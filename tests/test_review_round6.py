"""
Regression tests for round 6.

Round 6's agent died on the spend limit before running, so these came from
probing the round-5 fixes directly against the questions that round's own
result implied. Three findings, all in round-5 code, all the same shape as
round 5's four: the fix was applied where I looked and absent where I did not.

THE STRUCTURAL ANSWER, and the reason this round is different from the five
before it: capability is no longer ASSERTED, it is DERIVED. `_rails_on` records
only confirmed port results and `capability_for()` returns the most capable set
whose rails are all actually powered. Rounds 5 and 6 each found the engine
claiming a configuration the hardware was not in -- once by advancing
optimistically, once by leaving capability untouched after a partial shed a
refused rollback could not undo. Deriving the answer makes both of those
unrepresentable instead of separately guarded, which is the first fix in this
sequence that closes a CLASS rather than an instance.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "simulator"))

from environment import SpacecraftEnvironment  # noqa: E402
from fdir.degraded import (  # noqa: E402
    BELOW_MINIMAL, FULL, LADDER, MINIMAL, REDUCED, capability_for,
)
from fdir.engine import FDIREngine  # noqa: E402
from fdir.executor import RecoveryExecutor  # noqa: E402
from fdir.ports import RecoveryAction  # noqa: E402
from hardware_sim import SimulatedPowerPort  # noqa: E402
from icd import FaultFlag, Mode, Rail  # noqa: E402

ALL_RAILS = (Rail.OBC, Rail.RADIO, Rail.SENSORS, Rail.ADCS, Rail.PAYLOAD)


def wired(seed=3, n=55):
    env = SpacecraftEnvironment(seed=seed)
    e = FDIREngine()
    for _ in range(n):
        smp, _t = env.step(0.1)
        e.tick(smp, env.t)
    return env, e


def spin(env, e, ex, n):
    for _ in range(n):
        smp, _t = env.step(0.1)
        e.tick(smp, env.t)
        ex.step(e, env.t)


def powered(env):
    return {r for r in ALL_RAILS if env.rail_powered[r]}


def assert_consistent(env, e):
    """The invariant that matters: never claim a rail that is not on."""
    claimed = {Rail(r) for r in e.capability.rails_powered}
    actually = powered(env)
    lying = sorted(r.name for r in claimed - actually)
    assert not lying, (
        f"capability {e.capability.name} claims {lying} but those rails are off"
    )


class ShedOKRestoreFails:
    """Sheds SENSORS/ADCS, refuses to shed PAYLOAD, and refuses every re-power."""

    def __init__(self, env):
        self.inner = SimulatedPowerPort(env)

    def set_enabled(self, dev, on):
        if int(dev) == int(Rail.PAYLOAD) and not on:
            return False
        if on:
            return False
        return self.inner.set_enabled(dev, on)

    def is_enabled(self, dev):
        return self.inner.is_enabled(dev)


class ShedPayloadStuck:
    """Sheds everything except PAYLOAD; re-powers fine."""

    def __init__(self, env):
        self.inner = SimulatedPowerPort(env)

    def set_enabled(self, dev, on):
        if int(dev) == int(Rail.PAYLOAD) and not on:
            return False
        return self.inner.set_enabled(dev, on)

    def is_enabled(self, dev):
        return self.inner.is_enabled(dev)


# --- capability is derived, not asserted -----------------------------------

def test_capability_for_returns_the_best_set_actually_supported():
    assert capability_for({r for r in ALL_RAILS}) is FULL
    assert capability_for({Rail.OBC, Rail.RADIO, Rail.SENSORS, Rail.ADCS}) is REDUCED
    assert capability_for({Rail.OBC, Rail.RADIO}) is MINIMAL


def test_capability_for_never_over_claims():
    """
    A configuration no set describes must fall to the most degraded rung, not
    to the closest-looking one. Over-claiming is the direction the ground acts
    on.
    """
    odd = {Rail.OBC, Rail.RADIO, Rail.PAYLOAD}      # SENSORS and ADCS off
    cs = capability_for(odd)
    assert all(Rail(r) in odd for r in cs.rails_powered), (
        f"{cs.name} claims rails that are not powered"
    )


def test_capability_for_claims_nothing_when_nothing_is_satisfied():
    """
    ROUND 7 CORRECTED THIS TEST. It used to assert LADDER[-1] (MINIMAL), which
    directly contradicted test_capability_for_never_over_claims sitting twelve
    lines above it: MINIMAL claims OBC and RADIO, so returning it for a state
    where neither is confirmed powered is an over-claim in exactly the situation
    the vehicle can least afford one.

    Two of my own tests were in tension and only the passing one was exercised,
    because no case reached the other. That is the same failure as a scenario
    suite whose expectations encode current behaviour rather than the
    requirement.
    """
    assert capability_for(set()) is BELOW_MINIMAL
    assert capability_for(set()).rails_powered == ()


def test_below_minimal_claims_nothing():
    assert BELOW_MINIMAL.rails_powered == ()
    assert BELOW_MINIMAL.level > LADDER[-1].level


@pytest.mark.parametrize("on", [
    set(),
    {Rail.OBC},
    {Rail.RADIO},
    {Rail.OBC, Rail.SENSORS},
    {Rail.SENSORS, Rail.ADCS, Rail.PAYLOAD},
])
def test_capability_for_never_claims_an_unpowered_rail(on):
    """The property, over every subset that fails to satisfy a rung."""
    cs = capability_for(on)
    claimed = {Rail(r) for r in cs.rails_powered}
    assert not (claimed - on), (
        f"{cs.name} claims {sorted(r.name for r in claimed - on)} which are off"
    )


# --- R6-1: a refused rollback was invisible --------------------------------

def test_a_refused_rollback_does_not_leave_a_false_claim():
    """
    note_shed_completed emitted rollback POWER_ONs and did not track them. They
    route to note_restore_completed, which returned early because
    _restore_pending was empty -- so a REFUSED rollback was silent and the
    engine went on claiming FULL with two rails dead.

    Same shape as R5-3: one path made two-phase, the other left optimistic.
    """
    env, e = wired(seed=8)
    ex = RecoveryExecutor(ShedOKRestoreFails(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    spin(env, e, ex, 80)

    assert_consistent(env, e)
    assert any("REFUSED" in m for _, m in e.log), (
        "a refused re-power must be recorded, not silently dropped"
    )


def test_an_accepted_rollback_returns_the_vehicle_to_full():
    """The other half. A failed degrade should leave the vehicle where it was,
    not in a half-shed configuration it did not choose."""
    env, e = wired(seed=3)
    ex = RecoveryExecutor(ShedPayloadStuck(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    spin(env, e, ex, 80)

    assert powered(env) == set(ALL_RAILS), "the rollback should have re-powered everything"
    assert e.capability is FULL
    assert_consistent(env, e)


def test_the_rollback_remembers_what_it_shed_rather_than_recomputing_it():
    """
    Found while fixing the above, and worth its own test. The rollback computed
    which rails to restore as rails_to_shed(self.capability, target) -- which
    broke the instant capability began being derived from confirmed state,
    because capability has already moved by the time a refusal arrives and the
    diff then finds nothing to roll back. Remember the intent; do not
    re-derive it from state the intent itself changed.
    """
    env, e = wired(seed=3)
    ex = RecoveryExecutor(ShedPayloadStuck(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    spin(env, e, ex, 80)

    restored = [r for r in ex.history if r.intent.action == RecoveryAction.POWER_ON]
    assert restored, "the rollback never issued a single POWER_ON"


# --- R6-2: two two-phase machines sharing one intent queue -----------------

def test_a_downgrade_is_not_proposed_while_a_restore_is_in_flight():
    """
    Proposing a downgrade while rollback or restore POWER_ONs are still queued
    interleaves the two, and whichever the executor drains last decides the
    physical outcome.
    """
    env, e = wired(seed=9)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    e.restore_capability(env.t)                      # POWER_ON queued, not executed
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    smp, _t = env.step(0.1)
    e.tick(smp, env.t)

    actions = {i.action for i in e.pending_intents}
    assert not (RecoveryAction.POWER_ON in actions
                and RecoveryAction.POWER_OFF in actions), (
        "both directions queued at once; execution order decides the outcome"
    )


def test_the_vehicle_settles_consistently_after_a_reversal():
    env, e = wired(seed=9)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)
    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    e.restore_capability(env.t)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    spin(env, e, ex, 80)
    assert_consistent(env, e)


# --- R6-3: a reset dropped the bookkeeping but not the intents -------------

def test_a_reset_clears_in_flight_rail_operations():
    """The intents that would have completed them are gone, so the bookkeeping
    must go too -- consistent with every other pending state start_boot clears."""
    env, e = wired(seed=11)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)
    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    e.restore_capability(env.t)

    e.watchdog_reset(env.t)
    assert not e._restore_pending
    assert not e._shed_pending
    assert e._capability_target is None


def test_a_confirmed_rail_change_updates_the_belief_even_when_untracked():
    """
    The belief about physical state must follow any CONFIRMED port result, not
    only tracked ones. Gating it on the bookkeeping made the engine UNDER-claim
    after a reset: capability stuck at REDUCED with every rail powered, because
    the queued POWER_ON executed while _restore_pending had been cleared.
    """
    env, e = wired(seed=11)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)
    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    e.restore_capability(env.t)
    e.watchdog_reset(env.t)
    spin(env, e, ex, 40)

    assert powered(env) == set(ALL_RAILS)
    assert e.capability is FULL, (
        "the rail came back and the engine did not notice -- under-claiming is "
        "still a disagreement between software and hardware"
    )
    assert_consistent(env, e)


# --- the general invariant, across every port behaviour --------------------

@pytest.mark.parametrize("port_cls,seed", [
    (SimulatedPowerPort, 20),
    (ShedPayloadStuck, 21),
    (ShedOKRestoreFails, 22),
])
def test_capability_never_over_claims_under_any_port_behaviour(port_cls, seed):
    """
    The property that subsumes R5-2, R5-3, R6-1 and R6-3. Whatever the hardware
    does -- accepts everything, refuses a shed, refuses a re-power -- the engine
    must never report a capability set whose rails are not all powered.
    """
    env, e = wired(seed=seed)
    ex = RecoveryExecutor(port_cls(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    spin(env, e, ex, 100)
    assert_consistent(env, e)


# ---------------------------------------------------------------------------
# Round 7: the live simulator had no executor at all
# ---------------------------------------------------------------------------

def test_the_live_simulator_wires_an_executor():
    """
    ROUND 7'S HEADLINE. run_simulator.py had never had a RecoveryExecutor. The
    engine proposed RecoveryIntents into pending_intents and nothing drained
    them, so no autonomous recovery action had ever executed on the live path.
    Measured before the fix: 70 s of injected radio latch-up left the rail still
    latched, no campaign, no action.

    scenarios/runner.py wires one, which is why every measured recovery result
    in the results doc is real -- but the scenario harness is not the flight
    path, and the capability being demonstrated was absent from the thing being
    shipped. Same class as escape hatches with no telecommand and persistence
    methods nothing called: built, tested in one harness, never wired into the
    real one.
    """
    import run_simulator

    src = (REPO_ROOT / "simulator" / "run_simulator.py").read_text(encoding="utf-8")
    assert "RecoveryExecutor(" in src, "the live simulator constructs no executor"
    assert "executor.step(" in src, "the executor is constructed but never stepped"

    sim = run_simulator.Simulator(telemetry_rate_hz=10, seed=5)
    assert hasattr(sim, "executor"), "Simulator has no executor attribute"


def test_the_engine_and_environment_share_a_time_base():
    """
    Found while probing the above. The engine takes `now` from the wall clock
    while the environment advances simulated time, so any faster-than-real-time
    run silently diverges -- 700 tight-loop ticks were under a second of engine
    time while the environment believed 70 s had passed. The real telemetry
    loop sleeps, so the two stay aligned in normal operation, and this test
    pins the assumption rather than the behaviour.
    """
    src = (REPO_ROOT / "simulator" / "run_simulator.py").read_text(encoding="utf-8")
    assert "time.sleep(1.0 / sim.telemetry_rate_hz)" in src, (
        "the telemetry loop's sleep is what keeps engine time and environment "
        "time in step; without it every duration-based detector is wrong"
    )


# ---------------------------------------------------------------------------
# Round 8: loss of contact is the NORMAL state, and it masked everything
# ---------------------------------------------------------------------------

ACUTE = [
    (FaultFlag.UNDERVOLTAGE_CRITICAL, "POWER_UNDERVOLTAGE"),
    (FaultFlag.THERMAL_ANOMALY, "THERMAL"),
    (FaultFlag.SENSOR_LOCKUP, "SENSOR_FROZEN"),
    (FaultFlag.SENSOR_TIMEOUT, "SENSOR_NOT_RESPONDING"),
    (FaultFlag.SENSOR_IMPLAUSIBLE, "SENSOR_CORRUPT"),
    (FaultFlag.DRIFT_FROM_REFERENCE, "DEGRADATION"),
    (FaultFlag.RECOVERY_FAILED, "RECOVERY_EXHAUSTED"),
]


def _sample(**kw):
    from icd import RawSample
    rails = {int(Rail.OBC): 0.12, int(Rail.RADIO): 0.10, int(Rail.SENSORS): 0.06,
             int(Rail.ADCS): 0.08, int(Rail.PAYLOAD): 0.04}
    d = dict(temp_c=25.0, accel_x=0.1, accel_y=0.2, accel_z=9.8, gyro_x=0.0,
             gyro_y=0.0, gyro_z=0.0, mag_x=20.0, mag_y=5.0, mag_z=-40.0,
             bus_voltage_v=5.0, bus_current_a=0.40, rail_current_a=rails)
    d.update(kw)
    return RawSample(**d)


@pytest.mark.parametrize("flag,expected", ACUTE)
def test_loss_of_contact_does_not_mask_an_acute_fault(flag, expected):
    """
    ROUND 8'S HEADLINE, and the worst-placed instance of the F1 defect class in
    the whole review.

    The COMMS_LOSS rule sat third and returned on every path, so every acute
    fault beneath it reported as "ground link lost". That would be bad anywhere.
    Here it is worse, because COMMS_LOSS is not an anomaly on a CubeSat -- it is
    the NORMAL state for most of every orbit. An undervoltage, a thermal
    excursion, a frozen sensor or an exhausted recovery ladder would all have
    been masked for most of the mission.

    Measured on the live flight path before the fix: eleven injected faults,
    NINE of them diagnosed GROUND_LINK_LOST.

    Being out of contact is only the best explanation when nothing else is
    wrong.
    """
    from fdir.diagnosis import diagnose

    assert diagnose(flag | FaultFlag.COMMS_LOSS, _sample()).cause.name == expected


def test_the_compound_radio_diagnosis_still_outranks_the_acute_faults():
    """
    The split has to keep the COMPOUND case high. No contact AND a hot radio
    rail together mean something neither says alone, and that pairing is the
    project's headline discrimination measurement.
    """
    from fdir.diagnosis import Cause, diagnose

    hot = {int(Rail.OBC): 0.12, int(Rail.RADIO): 1.0, int(Rail.SENSORS): 0.06,
           int(Rail.ADCS): 0.08, int(Rail.PAYLOAD): 0.04}
    d = diagnose(FaultFlag.COMMS_LOSS | FaultFlag.UNDERVOLTAGE_CRITICAL,
                 _sample(rail_current_a=hot))
    assert d.cause == Cause.RADIO_LATCHUP


def test_the_discrimination_pair_survives_the_split():
    """The measurement that justified the per-rail current hardware."""
    from fdir.diagnosis import Cause, diagnose

    hot = {int(Rail.OBC): 0.12, int(Rail.RADIO): 1.0, int(Rail.SENSORS): 0.06,
           int(Rail.ADCS): 0.08, int(Rail.PAYLOAD): 0.04}
    assert diagnose(FaultFlag.COMMS_LOSS, _sample(rail_current_a=hot)).cause \
        == Cause.RADIO_LATCHUP
    assert diagnose(FaultFlag.COMMS_LOSS, _sample(rail_current_a=None)).cause \
        == Cause.GROUND_LINK_LOST
    assert diagnose(FaultFlag.COMMS_LOSS, _sample()).cause == Cause.GROUND_LINK_LOST


def test_loss_of_contact_alone_is_still_diagnosed():
    """Demoting it must not silence it -- with nothing else wrong, it IS the
    finding."""
    from fdir.diagnosis import Cause, diagnose

    assert diagnose(FaultFlag.COMMS_LOSS, _sample()).cause == Cause.GROUND_LINK_LOST
