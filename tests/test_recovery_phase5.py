"""
Phase 5: verification, bounded retries, escalation, persistence.

This is the KySat-2 file. That spacecraft DID respond to its fault -- it reset
hourly, indefinitely, each reset re-entering the same latch-up-and-drain
condition, with nothing checking whether any of it helped, until the battery
was gone. It had an action, no verification, and no escalation.

Phase 3 deliberately reproduced that shape and pinned it as a test. These tests
replace it with the fixed behaviour.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))

from environment import SpacecraftEnvironment  # noqa: E402
from hardware_sim import SimulatedPowerPort, SimulatedResetPort  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402
from fdir.executor import RecoveryExecutor  # noqa: E402
from fdir.ports import RecoveryAction  # noqa: E402
from fdir.recovery import Campaign, CampaignState, comms_loss_ladder  # noqa: E402
from icd import FaultFlag, Rail  # noqa: E402

DT = 0.1


class Harness:
    def __init__(self, seed=42, latch_clears=True):
        self.env = SpacecraftEnvironment(seed=seed, latch_clears_on_power_cycle=latch_clears)
        self.engine = FDIREngine()
        self.executor = RecoveryExecutor(
            SimulatedPowerPort(self.env), SimulatedResetPort(self.env)
        )

    def tick(self):
        sample, truth = self.env.step(DT)
        now = self.env.t
        self.engine.tick(sample, now)
        self.engine.note_link_state(
            now, connected=self.env.link_healthy,
            seconds_since_contact=sample.seconds_since_ground_contact,
        )
        self.executor.step(self.engine, now)
        return sample, truth

    def run(self, n):
        for _ in range(n):
            sample, truth = self.tick()
        return sample, truth

    def boot(self):
        return self.run(30)


# ---------------------------------------------------------------------------
# Bounded escalation
# ---------------------------------------------------------------------------

def test_unrecoverable_fault_is_bounded_and_ends_in_recovery_failed():
    """
    The action executes correctly every time and the fault never clears
    (latch_clears_on_power_cycle=False -- physically a latch upstream of the
    switch). The system must try a bounded number of escalating things and
    then STOP.
    """
    h = Harness(seed=19, latch_clears=False)
    h.boot()
    h.env.inject("radio_latchup")
    h.run(900)

    assert h.engine.campaign is not None
    assert h.engine.campaign.state == CampaignState.EXHAUSTED
    assert h.engine.fault_flags & FaultFlag.RECOVERY_FAILED, (
        "exhausting every rung must be visible in telemetry, not silent"
    )

    actions_at_exhaustion = len(h.executor.history)
    assert actions_at_exhaustion <= 6, "attempts must be bounded, not open-ended"

    # And it really stops. KySat-2's loop ran roughly hourly, forever.
    h.run(600)
    assert len(h.executor.history) == actions_at_exhaustion, (
        "no further autonomous action may be taken once recovery is exhausted"
    )


def test_escalation_moves_through_distinct_rungs_not_blind_repetition():
    """R3: a failed action is not simply retried forever -- the ladder escalates."""
    h = Harness(seed=21, latch_clears=False)
    h.boot()
    h.env.inject("radio_latchup")
    h.run(900)

    rungs_used = {(r.intent.action, r.intent.target) for r in h.executor.history}
    assert len(rungs_used) >= 2, f"expected escalation across rungs, saw only {rungs_used}"

    non_radio = [r for r in h.executor.history if r.intent.target != int(Rail.RADIO)]
    assert non_radio, (
        "every rung targeted the radio -- R4 says a recovery path must not "
        "depend solely on the subsystem it is recovering"
    )


# ---------------------------------------------------------------------------
# Verification -- the heart of it
# ---------------------------------------------------------------------------

def test_verification_is_observed_not_assumed_from_command_acceptance():
    """
    In the unrecoverable case the port ACCEPTS every power cycle and reports
    success -- yet the campaign must still conclude failure, because the
    verification condition is checked against telemetry afterwards. Treating
    "the port accepted it" as success is the KySat-2 conflation.
    """
    h = Harness(seed=23, latch_clears=False)
    h.boot()
    h.env.inject("radio_latchup")
    h.run(900)

    accepted = [r for r in h.executor.history
                if r.intent.action == RecoveryAction.POWER_CYCLE and r.accepted]
    assert accepted, "the port should have accepted the power cycles"
    assert h.engine.campaign.state == CampaignState.EXHAUSTED, (
        "accepted commands must not be mistaken for a verified recovery"
    )
    assert any("NOT VERIFIED" in msg for _, msg in h.engine.log)


def test_successful_recovery_is_verified_and_stops_escalating():
    """Once verification passes, the ladder must stop."""
    h = Harness(seed=25, latch_clears=True)
    h.boot()
    h.env.inject("radio_latchup")
    h.run(900)

    assert h.engine.campaign.state == CampaignState.SUCCEEDED
    assert not (h.engine.fault_flags & FaultFlag.RECOVERY_FAILED)
    assert any("VERIFIED" in msg and "NOT VERIFIED" not in msg for _, msg in h.engine.log)

    actions = len(h.executor.history)
    h.run(400)
    assert len(h.executor.history) == actions, "must not keep acting after success"


# ---------------------------------------------------------------------------
# Persistence -- why KySat-2's loop was infinite
# ---------------------------------------------------------------------------

def test_campaign_state_round_trips_through_plain_dict():
    """
    Persistence has to survive a schema change and, later, become a fixed-size
    record in STM32 backup SRAM. Plain dicts, not pickle.
    """
    original = Campaign(trigger=int(FaultFlag.COMMS_LOSS),
                        rungs=comms_loss_ladder(int(Rail.RADIO)),
                        rung_index=1, attempts_on_rung=1, total_attempts=2)
    restored = Campaign.from_dict(original.to_dict())

    assert restored.rung_index == 1
    assert restored.total_attempts == 2
    assert [r.action for r in restored.rungs] == [r.action for r in original.rungs]


def test_reset_midcampaign_resumes_at_next_rung_and_remembers_attempts():
    """
    THE KySat-2 ASSERTION. A reboot mid-campaign must not restart at rung 0
    with the attempt counter at zero -- that is precisely what makes a reset
    loop infinite. Resuming at the NEXT rung is deliberate: we cannot know
    whether the interrupted action completed, and re-running it would be the
    blind repetition R3 forbids.
    """
    h = Harness(seed=27, latch_clears=False)
    h.boot()
    h.env.inject("radio_latchup")

    for _ in range(900):
        h.tick()
        if h.engine.campaign is not None and h.engine.campaign.total_attempts >= 1:
            break
    assert h.engine.campaign is not None

    saved = h.engine.export_recovery_state()
    assert saved is not None, "state must be exportable before an action executes"
    rung_before = saved["rung_index"]
    attempts_before = saved["total_attempts"]

    # The OBC reboots. Physics is untouched; software memory is not.
    h.env.obc_reset()
    fresh = FDIREngine()
    fresh.watchdog_reset(h.env.t)
    fresh.import_recovery_state(saved, h.env.t)

    assert fresh.campaign is not None, "the campaign must survive the reset"
    assert fresh.campaign.total_attempts == attempts_before, (
        "prior attempts must be remembered -- forgetting them is what made "
        "KySat-2's reset loop infinite"
    )
    assert fresh.campaign.rung_index == rung_before + 1, (
        "must resume at the NEXT rung, never re-run an interrupted action"
    )


def test_restored_campaign_past_its_last_rung_is_exhausted_not_restarted():
    ladder = comms_loss_ladder(int(Rail.RADIO))
    nearly_done = Campaign(trigger=int(FaultFlag.COMMS_LOSS), rungs=ladder,
                           rung_index=len(ladder) - 1, attempts_on_rung=1,
                           total_attempts=4)

    engine = FDIREngine()
    engine.import_recovery_state(nearly_done.to_dict(), now=0.0)

    assert engine.campaign.state == CampaignState.EXHAUSTED
    assert engine.fault_flags & FaultFlag.RECOVERY_FAILED


def test_unreadable_persisted_state_is_discarded_not_trusted():
    """Corrupt NVM must not be able to inject an arbitrary campaign."""
    engine = FDIREngine()
    engine.import_recovery_state({"schema_version": 999, "garbage": True}, now=0.0)

    assert engine.campaign is None
    assert any("discarded" in msg for _, msg in engine.log)
