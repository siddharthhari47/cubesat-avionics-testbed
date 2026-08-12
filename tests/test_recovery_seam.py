"""
Verification for the Phase 3 actuation seam: FDIREngine proposes recovery
intents, RecoveryExecutor carries them out against hardware ports.

The single most important assertions in this file are the negative ones. The
project's central principle is that a statistical or learned detector may
advise but never command, and Phase 3 is where that principle first has to
survive contact with an *action* rather than only a mode change. A mode gate
is not an action gate -- commanding a rail is a strictly stronger permission
than setting a variable -- so RECOVERY_AUTHORITY_FLAGS exists separately from
SAFE_MODE_TRIGGER_FLAGS and is tested separately here.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))

from environment import SpacecraftEnvironment  # noqa: E402
from hardware_sim import SimulatedPowerPort, SimulatedResetPort  # noqa: E402
from fdir import config as cfg  # noqa: E402
from fdir.engine import (  # noqa: E402
    FDIREngine, MLAdvisory, RECOVERY_AUTHORITY_FLAGS, SAFE_MODE_TRIGGER_FLAGS,
)
from fdir.executor import RecoveryExecutor  # noqa: E402
from fdir.ports import RecoveryAction  # noqa: E402
from icd import FaultFlag, Mode, Rail  # noqa: E402

DT = 0.1


class Harness:
    """environment -> engine -> executor, wired the way flight software would."""

    def __init__(self, seed=42, latch_clears=True):
        self.env = SpacecraftEnvironment(seed=seed, latch_clears_on_power_cycle=latch_clears)
        self.engine = FDIREngine()
        self.executor = RecoveryExecutor(
            SimulatedPowerPort(self.env), SimulatedResetPort(self.env)
        )

    def tick(self, ml_advisory=None):
        sample, truth = self.env.step(DT)
        now = self.env.t
        self.engine.tick(sample, now, ml_advisory=ml_advisory)
        self.engine.note_link_state(
            now, link_established=self.env.link_healthy,
            seconds_since_contact=sample.seconds_since_ground_contact,
        )
        self.executor.step(self.engine, now)
        return sample, truth

    def run(self, n, ml_advisory=None):
        for _ in range(n):
            sample, truth = self.tick(ml_advisory)
        return sample, truth

    def boot(self):
        return self.run(30)


# ---------------------------------------------------------------------------
# The capability the failure research ranked first
# ---------------------------------------------------------------------------

def test_csswe_radio_latchup_recovers_autonomously():
    """
    CSSWE lost communications to a radio latch-up and sat dead for three
    months, then recovered BY ACCIDENT when an unrelated battery drain
    power-cycled the spacecraft. The corrective action existed, was within the
    spacecraft's capability, and was proven to work -- it was simply never
    commanded, because the only asset that could have commanded it was the
    failed radio.

    This asserts the architecture provides a recovery pathway for that
    documented failure mechanism. It is NOT a claim that this would have saved
    CSSWE.
    """
    h = Harness(seed=42)
    h.boot()

    h.env.inject("radio_latchup")
    sample, truth = h.tick()
    assert truth.rail_latched[int(Rail.RADIO)] is True
    assert sample.radio_responded is False

    # Long enough for the COMMS_LOSS debounce, THEN the recovery trigger, plus
    # dwell and settle.
    #
    # The debounce term is new and it is not padding. Before K1, the
    # environment never advanced last_ground_contact_t, so
    # seconds_since_ground_contact was really "seconds since boot" -- already
    # past the 5 s timeout by the time any fault was injected. COMMS_LOSS
    # therefore latched the instant the link dropped and
    # COMMS_LOSS_TIMEOUT_S was never actually exercised by this test or any
    # other. Now that the timeout genuinely applies, the ladder starts 5 s
    # later and the old budget expired mid-campaign.
    ticks = int((cfg.COMMS_LOSS_TIMEOUT_S + cfg.COMMS_RECOVERY_TRIGGER_S) / DT) + 120
    sample, truth = h.run(ticks)

    assert h.executor.attempts_for(RecoveryAction.POWER_CYCLE, int(Rail.RADIO)) == 1, (
        "exactly one power cycle should have been attempted"
    )
    assert truth.rail_latched[int(Rail.RADIO)] is False, "the latch should have cleared"
    assert sample.radio_responded is True
    assert not (h.engine.fault_flags & FaultFlag.COMMS_LOSS)


def test_recovery_uses_its_own_trigger_not_the_link_heartbeat():
    """
    COMMS_LOSS_TIMEOUT_S (5 s) decides when to FLAG loss of contact.
    COMMS_RECOVERY_TRIGGER_S decides when to ACT on it. Conflating them would
    power-cycle the radio every five seconds.
    """
    assert cfg.COMMS_RECOVERY_TRIGGER_S > cfg.COMMS_LOSS_TIMEOUT_S

    h = Harness(seed=7)
    h.boot()
    h.env.inject("communication_loss")

    # Past the heartbeat, well short of the action trigger.
    h.run(int((cfg.COMMS_LOSS_TIMEOUT_S + 1.0) / DT))
    assert h.engine.fault_flags & FaultFlag.COMMS_LOSS, "loss should be flagged by now"
    assert h.executor.history == [], "but no action should have been taken yet"


# ---------------------------------------------------------------------------
# Action authority -- the negative assertions that matter most
# ---------------------------------------------------------------------------

def test_advisory_flags_are_excluded_from_recovery_authority():
    """Structural. A learned or statistical detector may not command hardware."""
    assert not (FaultFlag.ML_ANOMALY & RECOVERY_AUTHORITY_FLAGS)
    assert not (FaultFlag.ADAPTIVE_ANOMALY & RECOVERY_AUTHORITY_FLAGS)
    # And the two gates are genuinely separate concepts, not aliases.
    assert RECOVERY_AUTHORITY_FLAGS != SAFE_MODE_TRIGGER_FLAGS


@pytest.mark.parametrize("advisory_flag", [FaultFlag.ML_ANOMALY, FaultFlag.ADAPTIVE_ANOMALY])
def test_a_proposal_justified_only_by_an_advisory_flag_is_refused(advisory_flag):
    """
    Behavioural counterpart to the structural test: even if a future producer
    tries to justify an action with an advisory detector, the gate refuses it
    in one place rather than relying on every producer to remember the rule.
    """
    h = Harness(seed=3)
    h.boot()

    issued = h.engine._propose(
        h.env.t, RecoveryAction.POWER_CYCLE, int(Rail.RADIO),
        advisory_flag, "test: advisory-only justification",
    )

    assert issued is False
    assert h.engine.pending_intents == []
    assert any("REFUSED" in msg for _, msg in h.engine.log)


def test_sustained_ml_anomaly_never_produces_a_recovery_action():
    """
    End-to-end version: 200 consecutive anomalous advisories, and the hardware
    is never touched. Mirrors the existing mode-level test that ML cannot force
    SAFE, extended to actions.
    """
    h = Harness(seed=5)
    h.boot()

    h.run(200, ml_advisory=MLAdvisory(score=0.99, is_anomalous=True))

    assert h.engine.fault_flags & FaultFlag.ML_ANOMALY, "the flag should latch -- advising is allowed"
    assert h.executor.history == [], "but nothing may be commanded on its authority"
    assert h.engine.mode != Mode.SAFE


def test_engine_holds_no_reference_to_any_hardware_port():
    """
    The engine must stay a pure function of (sample, time, advisory). If it ever
    grows a port reference, every test that constructs a bare FDIREngine()
    starts needing a fake, and "no GPIO/I2C in FDIR" stops being true.
    """
    engine = FDIREngine()
    for value in vars(engine).values():
        assert not hasattr(value, "set_enabled"), "FDIREngine must not hold a PowerPort"
        assert not hasattr(value, "reset_system"), "FDIREngine must not hold a ResetPort"


# ---------------------------------------------------------------------------
# Executor behaviour
# ---------------------------------------------------------------------------

def test_power_cycle_respects_the_dwell_time():
    """
    Off-time is load-bearing: a latch clears only if power was genuinely
    removed long enough. The executor must not collapse the cycle into one tick.
    """
    h = Harness(seed=11)
    h.boot()
    h.env.inject("radio_latchup")

    # The flag must latch first (COMMS_LOSS_TIMEOUT_S), and only then does the
    # recovery trigger start counting -- so run until the action completes
    # rather than guessing a tick count.
    budget = int((cfg.COMMS_LOSS_TIMEOUT_S + cfg.COMMS_RECOVERY_TRIGGER_S) / DT) + 400
    for _ in range(budget):
        h.tick()
        if any(r.completed_at is not None and r.intent.action == RecoveryAction.POWER_CYCLE
               for r in h.executor.history):
            break

    completed = [r for r in h.executor.history
                 if r.completed_at is not None
                 and r.intent.action == RecoveryAction.POWER_CYCLE]
    assert completed, "a power cycle should have completed"
    record = completed[0]
    off_duration = record.completed_at - record.started_at
    assert off_duration >= cfg.POWER_CYCLE_OFF_TIME_S


def test_executor_refuses_to_start_a_second_action_while_one_is_in_flight():
    """A rail left off because two recoveries overlapped is worse than a delay."""
    h = Harness(seed=13)
    h.boot()

    h.engine._propose(h.env.t, RecoveryAction.POWER_CYCLE, int(Rail.RADIO),
                      FaultFlag.COMMS_LOSS, "first")
    h.tick()
    assert h.executor.busy

    h.engine._propose(h.env.t, RecoveryAction.POWER_CYCLE, int(Rail.PAYLOAD),
                      FaultFlag.COMMS_LOSS, "second, should be refused")
    h.tick()

    refused = [r for r in h.executor.history if not r.accepted]
    assert any("already in progress" in r.detail for r in refused)


def test_unavailable_port_reports_failure_rather_than_pretending(monkeypatch):
    """
    A stub that silently returns success would teach the recovery logic a lie.
    Device reset is not modelled, so it must report that honestly.
    """
    h = Harness(seed=17)
    h.boot()

    h.engine._propose(h.env.t, RecoveryAction.RESET_DEVICE, int(Rail.RADIO),
                      FaultFlag.SENSOR_TIMEOUT, "test")
    h.tick()

    assert h.executor.history[-1].accepted is False
    assert "unavailable" in h.executor.history[-1].detail


