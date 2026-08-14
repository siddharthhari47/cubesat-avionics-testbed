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
from fdir.degraded import FULL, LADDER, MINIMAL, REDUCED, capability_for  # noqa: E402
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


def test_capability_for_falls_to_the_last_rung_when_nothing_matches():
    assert capability_for(set()) is LADDER[-1]


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
