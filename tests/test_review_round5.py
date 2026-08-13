"""
Regression tests for round 5 (R5-1 to R5-4).

ALL FOUR WERE IN CODE THAT WAS ITSELF A FIX. Round 4's lesson was that fixes in
this system have a high defect rate; round 5 is that lesson holding at 4/4.

The shape repeats and is worth naming, because naming it is the only defence
that generalises:

  * R5-1  a fix applied in ONE branch of a function and not the other
  * R5-2  a two-phase commit that handles total failure and not PARTIAL failure
  * R5-3  a rule applied to one DIRECTION of a symmetric operation
  * R5-4  a guard added to one entry point and not to the one beside it

Every one is "the fix was correct where I looked and absent where I did not".
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "simulator"))

from environment import SpacecraftEnvironment  # noqa: E402
from fdir.degraded import FULL, REDUCED  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402
from fdir.executor import RecoveryExecutor  # noqa: E402
from fdir.ports import RecoveryAction, RecoveryIntent  # noqa: E402
from hardware_sim import SimulatedPowerPort  # noqa: E402
from icd import FaultFlag, Mode, Rail, RawSample  # noqa: E402

RAILS = {int(Rail.OBC): 0.12, int(Rail.RADIO): 0.10, int(Rail.SENSORS): 0.06,
         int(Rail.ADCS): 0.08, int(Rail.PAYLOAD): 0.04}


def sample(i=0, **kw):
    d = dict(temp_c=25.0, accel_x=0.1 + i * 1e-6, accel_y=0.2, accel_z=9.8,
             gyro_x=0.01 + i * 1e-7, gyro_y=0.02, gyro_z=0.03,
             mag_x=20.0, mag_y=5.0, mag_z=-40.0, bus_voltage_v=5.0,
             bus_current_a=0.40, rail_current_a=dict(RAILS))
    d.update(kw)
    return RawSample(**d)


class AcceptAll:
    def __init__(self):
        self.calls = []

    def set_enabled(self, dev, on):
        self.calls.append((int(dev), on))
        return True

    def is_enabled(self, dev):
        return True


def wired(seed=3, n=55):
    """Engine + executor + the real environment, past commissioning."""
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


# --- R5-1: a fix applied in one branch and not the other -------------------

def test_a_shed_refused_while_busy_reaches_the_capability_machine():
    """
    Round 4 split shed reporting from campaign reporting -- inside _begin()
    only. The busy branch of step() still sent EVERY refused intent to
    note_action_completed(), so a POWER_OFF proposed while a comms-recovery
    power cycle was in its off-dwell reported to the recovery campaign.
    _shed_pending was never cleared, _update_degraded_mode returned on its first
    line for the rest of the mission, and R8 autonomy died with no attempt
    counted and no stand-down logged.
    """
    e = FDIREngine()
    t = 0.0
    for i in range(55):
        e.tick(sample(i), t)
        t += 0.1
    ex = RecoveryExecutor(AcceptAll(), None)

    e.pending_intents.append(RecoveryIntent(
        action=RecoveryAction.POWER_CYCLE, target=Rail.RADIO,
        reason="in-flight cycle", requested_at=t))
    ex.step(e, t)
    t += 0.01

    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    e.tick(sample(0), t)
    ex.step(e, t)
    for i in range(60):
        t += 0.1
        e.tick(sample(i), t)
        ex.step(e, t)

    assert not e._shed_pending, (
        "the shed was reported to the wrong state machine and never cleared"
    )
    assert e.capability.level > 0 or e._degrade_attempts > 0, (
        "R8 autonomy must still be alive after the collision"
    )


def test_every_completion_is_routed_through_one_place():
    """
    The general form. Two branches deciding independently which state machine
    hears about a completion is what allowed the split to be applied to one and
    not the other.
    """
    import inspect

    src = inspect.getsource(RecoveryExecutor)
    direct = src.count("engine.note_action_completed") + \
        src.count("engine.note_shed_completed") + \
        src.count("engine.note_restore_completed")
    assert direct == 3, (
        f"{direct} direct completion calls; they must appear only inside "
        f"_report(), or a future branch will route one of them wrongly again"
    )


# --- R5-2: total failure handled, partial failure not ----------------------

def test_a_partially_refused_shed_rolls_back():
    """
    note_shed_completed advanced capability all-or-nothing, so a port whose
    PAYLOAD switch is stuck still took SENSORS and ADCS off -- and the engine
    then reported FULL/NOMINAL with two rails dead, permanently. It is the
    engine's claim, not the truth, that reaches the ground.
    """
    class Picky:
        def __init__(self, env):
            self.inner = SimulatedPowerPort(env)

        def set_enabled(self, dev, on):
            if int(dev) == int(Rail.PAYLOAD) and not on:
                return False
            return self.inner.set_enabled(dev, on)

        def is_enabled(self, dev):
            return self.inner.is_enabled(dev)

    env, e = wired()
    ex = RecoveryExecutor(Picky(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    spin(env, e, ex, 80)

    off = [Rail(r).name for r in RAILS if not env.rail_powered[Rail(r)]]
    assert not off, (
        f"rails {off} are physically off while the engine claims "
        f"{e.capability.name} -- a configuration no capability set describes"
    )


# --- R5-3: the rule applied to one direction only --------------------------

def test_restore_does_not_claim_full_until_the_rails_are_back():
    """
    Round 4 made the DOWNGRADE two-phase and left the restore committing
    immediately. A load switch that fails open left the rail dead while the
    engine reported FULL -- and once this became telecommand 0x0B, the operator
    was told ACCEPTED for a restore that never happened.
    """
    class NoRepower:
        def __init__(self, env):
            self.inner = SimulatedPowerPort(env)

        def set_enabled(self, dev, on):
            if on:
                return False
            return self.inner.set_enabled(dev, on)

        def is_enabled(self, dev):
            return self.inner.is_enabled(dev)

    env, e = wired(seed=4)
    ex = RecoveryExecutor(NoRepower(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)
    assert e.capability is REDUCED

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    assert e.restore_capability(env.t) is True, "the command is accepted"
    spin(env, e, ex, 10)

    assert env.rail_powered[Rail.PAYLOAD] is False, "the port refused"
    assert e.capability is REDUCED, (
        "the engine must not claim FULL for a restore the hardware refused"
    )


def test_a_confirmed_restore_does_advance():
    env, e = wired(seed=5)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)
    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE

    assert e.restore_capability(env.t) is True
    spin(env, e, ex, 10)
    assert e.capability is FULL
    assert env.rail_powered[Rail.PAYLOAD] is True


# --- R5-4: a guard on one entry point and not the one beside it ------------

def test_restore_is_refused_in_safe_mode():
    """
    _update_degraded_mode refuses to touch capability in SAFE; restore had no
    such guard. Its only gate was select_level(), computed from
    DEGRADE_TRIGGERS -- a set DISJOINT from the SAFE triggers -- so it read 0 in
    exactly the state where the vehicle is least able to carry load. The ground
    could add payload draw during an undervoltage-critical SAFE and be told
    ACCEPTED.
    """
    env, e = wired(seed=6)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)
    assert env.rail_powered[Rail.PAYLOAD] is False

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    e.fault_flags |= FaultFlag.UNDERVOLTAGE_CRITICAL
    e.mode = Mode.SAFE

    assert e.restore_capability(env.t) is False
    spin(env, e, ex, 10)
    assert env.rail_powered[Rail.PAYLOAD] is False, (
        "adding payload load during a critical undervoltage is the opposite of "
        "what SAFE is for"
    )


def test_restore_is_refused_while_a_safe_trigger_is_latched_even_outside_safe():
    """The flag, not just the mode -- a vehicle about to enter SAFE is in no
    better position to carry load than one already there."""
    env, e = wired(seed=7)
    ex = RecoveryExecutor(SimulatedPowerPort(env), None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(env, e, ex, 10)

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    e.fault_flags |= FaultFlag.THERMAL_ANOMALY
    e.mode = Mode.DEGRADED

    assert e.restore_capability(env.t) is False
