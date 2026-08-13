"""
Regression tests for round 4 (F1-F6).

THREE OF THESE SIX WERE IN CODE WRITTEN HOURS EARLIER AS FIXES. F2 and F3 were
introduced by the round-4 self-probed repair of the degrade path; F5 was
introduced by the round-4 repair of the commissioning guard. The bound I added
to stop an unbounded retry became a permanent block; the two-phase commit I
added to stop a false capability claim opened a window where an operator restore
was accepted and then silently reversed; and widening a guard to catch more
faults handed an advisory detector a veto over a deterministic one.

That is the finding behind the findings, and it is why these tests exist rather
than a note in a document: fixes in this system have a high defect rate, and the
only thing that has reliably caught them is running the code.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "simulator"))

from fdir import config as cfg  # noqa: E402
from fdir.degraded import select_level  # noqa: E402
from fdir.engine import DETERMINISTIC_CONDITION_FLAGS, FDIREngine  # noqa: E402
from fdir.executor import RecoveryExecutor  # noqa: E402
from fdir.ports import RecoveryAction  # noqa: E402
from icd import FaultFlag, Mode, Rail, RawSample  # noqa: E402

RAILS = {int(Rail.OBC): 0.12, int(Rail.RADIO): 0.10, int(Rail.SENSORS): 0.06,
         int(Rail.ADCS): 0.08, int(Rail.PAYLOAD): 0.04}


def sample(i=0, rails=None, **kw):
    d = dict(temp_c=25.0, accel_x=0.1 + i * 1e-6, accel_y=0.2, accel_z=9.8,
             gyro_x=0.01 + i * 1e-7, gyro_y=0.02, gyro_z=0.03,
             mag_x=20.0, mag_y=5.0, mag_z=-40.0, bus_voltage_v=5.0,
             bus_current_a=0.40,
             rail_current_a=dict(RAILS if rails is None else rails))
    d.update(kw)
    return RawSample(**d)


class Port:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def set_enabled(self, dev, on):
        self.calls.append((int(dev), on))
        return self.ok

    def is_enabled(self, dev):
        return True


def commissioned(v=5.0, n=50):
    e = FDIREngine()
    t = 0.0
    for i in range(n):
        e.tick(sample(i, bus_voltage_v=v), t)
        t += 0.1
    return e, t


def spin(e, ex, t, n, **kw):
    for i in range(n):
        e.tick(sample(i, **kw), t)
        ex.step(e, t)
        t += 0.1
    return t


# --- F1: the guard was calibrated to the wrong number, and one-sided --------

@pytest.mark.parametrize("v", [4.55, 4.70, 5.30, 5.60])
def test_commissioning_refuses_voltages_outside_the_drift_band(v):
    """
    The old guard admitted anything the undervoltage detector would not already
    have flagged -- [4.5 V, +infinity). That window is wider than the +/-0.25 V
    tolerance the reference is later held to, so a vehicle commissioned on a
    partly-discharged battery at 4.55 V (the normal post-deployment state,
    before the panels produce) captured 4.55 V and then latched DRIFT the moment
    charging brought it to a healthy 5.00 V. There is no overvoltage detector at
    all, so the upper side was unbounded.
    """
    e, _t = commissioned(v=v)
    assert e.voltage_reference is None, (
        f"{v} V is {abs(v - cfg.NOMINAL_VOLTAGE_V):.2f} V from nominal, outside "
        f"the +/-{cfg.DRIFT_FROM_REFERENCE_V} V band the reference is judged by"
    )


def test_commissioning_still_succeeds_at_nominal():
    e, _t = commissioned(v=5.0)
    assert e.voltage_reference == pytest.approx(5.0, abs=0.05)


def test_a_healthy_charge_does_not_latch_drift():
    """The end-to-end version of F1: commission low, charge to nominal."""
    e, t = commissioned(v=4.55)
    for i in range(60):
        e.tick(sample(i, bus_voltage_v=5.0), t)
        t += 0.1
    assert not (e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE)


# --- F2: a bounded retry became a permanent block ---------------------------

def test_refused_sheds_are_bounded_for_that_condition():
    e, t = commissioned()
    port = Port(ok=False)
    ex = RecoveryExecutor(port, None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    spin(e, ex, t, 20)
    assert len(port.calls) <= cfg.MAX_DEGRADE_ATTEMPTS


def test_a_refused_downgrade_does_not_kill_degradation_forever():
    """
    F2. _capability_target stayed set and _degrade_attempts stayed at the bound,
    so the early return fired on every later tick for the rest of the mission --
    including for rungs never attempted, and for evidence that had since
    escalated. Nothing cleared it: not restore_capability, not
    recommission_reference, not reset_faults.
    """
    e, t = commissioned()
    port = Port(ok=False)
    ex = RecoveryExecutor(port, None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    t = spin(e, ex, t, 20)
    assert port.calls, "the first attempts happened"

    port.calls.clear()
    hot = {**RAILS, int(Rail.PAYLOAD): 1.4}
    t = spin(e, ex, t, 40, rails=hot)
    assert select_level(e.fault_flags) == 2, "evidence corroborated to level 2"
    assert port.calls, (
        "new, stronger evidence must produce a new attempt -- the bound is per "
        "condition, not a permanent stand-down"
    )


def test_an_operator_restore_clears_the_degrade_block():
    e, t = commissioned()
    port = Port(ok=False)
    ex = RecoveryExecutor(port, None)
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    t = spin(e, ex, t, 20)

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    assert e.restore_capability(t) is True
    assert e._degrade_attempts == 0
    assert e._blocked_at_level is None


# --- F3: restore accepted while a shed was in flight ------------------------

def test_restore_is_refused_while_the_cause_is_present_even_mid_shed():
    """
    Once capability stopped advancing at proposal time, a window opened where
    capability.level was still 0 while a POWER_OFF was already queued.
    restore_capability() returned True on that first check without ever
    consulting the evidence -- and the shed landed anyway, degrading the vehicle
    immediately after an accepted command to do the opposite.
    """
    e, t = commissioned()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    e.tick(sample(0), t)
    assert e.capability.level == 0, "the downgrade has not completed yet"
    assert e._shed_pending, "but a shed is queued"
    assert select_level(e.fault_flags) > 0, "and the cause is present"
    assert e.restore_capability(t) is False


def test_an_accepted_restore_withdraws_a_queued_shed():
    e, t = commissioned()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    e.tick(sample(0), t)
    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE

    assert e.restore_capability(t) is True
    assert not e._shed_pending
    assert not any(i.action == RecoveryAction.POWER_OFF for i in e.pending_intents), (
        "an accepted restore must not be followed by the shed it prevented"
    )


# --- F4: escapes that the ground could not reach ----------------------------

def test_the_operator_escapes_are_reachable_as_telecommands():
    """
    Round 3 declared recommission_reference() 'the escape hatch that was
    missing'. Round 4 found it reachable from no telecommand at all -- an escape
    the ground cannot reach is not an escape.
    """
    import protocol
    assert hasattr(protocol.CommandId, "RECOMMISSION_REFERENCE")
    assert hasattr(protocol.CommandId, "RESTORE_CAPABILITY")

    src = (REPO_ROOT / "simulator" / "run_simulator.py").read_text(encoding="utf-8")
    assert "RECOMMISSION_REFERENCE" in src
    assert "RESTORE_CAPABILITY" in src


def test_nvm_persistence_is_actually_called_on_the_reboot_path():
    """
    export/import_reference_state and export/import_capability_state were
    written, tested, and called by nothing outside tests -- so the R7 reference
    and R8 capability survived a reboot only in the test suite. On the real
    path both were silently lost, which is precisely what each was written to
    prevent.
    """
    src = (REPO_ROOT / "simulator" / "run_simulator.py").read_text(encoding="utf-8")
    for method in ("export_reference_state", "import_reference_state",
                   "export_capability_state", "import_capability_state",
                   "export_recovery_state", "import_recovery_state"):
        assert method in src, f"{method} is never called outside the tests"


# --- F5: an advisory flag vetoing a deterministic detector ------------------

def test_advisory_flags_cannot_veto_the_commissioning_capture():
    """
    Widening the guard to CONDITION_BACKED_FLAGS swept in ADAPTIVE_ANOMALY,
    ML_ANOMALY and UNKNOWN_ANOMALY, so a latched advisory bit blocked the
    capture indefinitely and R7 silently never came into existence. A learned
    detector deciding whether a deterministic one may exist is the exact inverse
    of this project's central boundary -- reached while fixing something else.
    """
    for flag in (FaultFlag.ADAPTIVE_ANOMALY, FaultFlag.ML_ANOMALY,
                 FaultFlag.UNKNOWN_ANOMALY):
        assert not (DETERMINISTIC_CONDITION_FLAGS & flag), (
            f"{flag.name} can gate a deterministic decision"
        )

    e = FDIREngine()
    t = 0.0
    for i in range(25):
        e.tick(sample(i), t)
        t += 0.1
    e.voltage_reference = None
    e._reference_samples = []
    e.fault_flags |= FaultFlag.ML_ANOMALY
    for i in range(60):
        e.tick(sample(i), t)
        t += 0.1
    assert e.voltage_reference is not None, "an advisory bit switched off R7"


def test_a_real_condition_still_blocks_the_capture():
    """The fix must not have blunted the guard it narrowed."""
    e = FDIREngine()
    t = 0.0
    for i in range(25):
        e.tick(sample(i), t)
        t += 0.1
    e.voltage_reference = None
    e._reference_samples = []
    e.fault_flags |= FaultFlag.THERMAL_ANOMALY
    for i in range(60):
        e.tick(sample(i), t)
        t += 0.1
    assert e.voltage_reference is None


# --- F6: a reset left the drift debounce and a partial capture behind -------

def test_a_reset_clears_the_drift_debounce():
    """
    Every other debounce timer is nulled in start_boot(); _drift_since was
    missed. The first post-boot out-of-band sample was therefore compared
    against a timestamp from before the reset, so a single transient -- exactly
    what the 2 s debounce exists to forgive -- latched immediately.
    """
    e, t = commissioned()
    for i in range(19):                       # 1.9 s, just under the debounce
        e.tick(sample(i, bus_voltage_v=4.60), t)
        t += 0.1
    e.watchdog_reset(t)
    for i in range(21):                       # clear BOOT
        e.tick(sample(i), t)
        t += 0.1
    e.tick(sample(0, bus_voltage_v=4.60), t)
    assert not (e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE)


def test_a_reset_discards_a_partial_capture():
    """A capture interrupted by a reboot must not resume with samples from a
    different epoch mixed in."""
    e = FDIREngine()
    t = 0.0
    for i in range(25):
        e.tick(sample(i), t)
        t += 0.1
    e.voltage_reference = None
    e._reference_samples = []
    for i in range(10):
        e.tick(sample(i), t)
        t += 0.1
    assert e._reference_samples, "a partial capture is under way"
    e.watchdog_reset(t)
    assert e._reference_samples == []
