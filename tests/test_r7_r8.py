"""
R7 (fixed-reference drift) and R8 (degraded modes).

The two requirements the traceability doc carried as NOT MET. Both are derived
from the failure record rather than invented: QuakeSat for R7, BIRD/Odin for R8.

R8 carries a limitation these tests pin explicitly: "pre-validated" means
measured, and nothing here has been measured. The capability sets are DECLARED,
and `test_capability_sets_are_declared_not_measured` exists so that stays
visible rather than quietly becoming a claim.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fdir import config as cfg  # noqa: E402
from fdir.degraded import (  # noqa: E402
    FULL, LADDER, MINIMAL, REDUCED, DEGRADE_TRIGGERS, rails_to_shed, select_level,
)
from fdir.diagnosis import Cause, diagnose  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402
from icd import FaultFlag, Mode, Rail, RawSample  # noqa: E402

RAILS = {int(Rail.OBC): 0.12, int(Rail.RADIO): 0.10, int(Rail.SENSORS): 0.06,
         int(Rail.ADCS): 0.08, int(Rail.PAYLOAD): 0.04}


def sample(i=0, **kw):
    d = dict(temp_c=25.0, accel_x=0.1 + i * 1e-6, accel_y=0.2, accel_z=9.8,
             gyro_x=0.01 + i * 1e-7, gyro_y=0.02, gyro_z=0.03,
             mag_x=20.0, mag_y=5.0, mag_z=-40.0,
             bus_voltage_v=5.0, bus_current_a=0.40, rail_current_a=dict(RAILS))
    d.update(kw)
    return RawSample(**d)


def commissioned():
    """An engine past BOOT with its commissioning reference captured."""
    e = FDIREngine()
    t = 0.0
    for i in range(30 + cfg.REFERENCE_CAPTURE_SAMPLES):
        e.tick(sample(i), t)
        t += 0.1
    assert e.mode == Mode.NOMINAL
    assert e.voltage_reference is not None
    return e, t


def drive(e, t, n, **kw):
    for i in range(n):
        e.tick(sample(i, **kw), t)
        t += 0.1
    return t


# ---------------------------------------------------------------------------
# R7 -- drift against a reference that cannot be learned away
# ---------------------------------------------------------------------------

def test_the_reference_is_captured_from_clean_samples():
    e, _t = commissioned()
    assert e.voltage_reference == pytest.approx(5.0, abs=0.05)


def test_a_drift_the_adaptive_baseline_absorbs_is_still_caught():
    """
    The requirement, in one test. The EWMA follows the signal, so a slow enough
    decline becomes the new normal -- measured at 0% recall in the ML
    evaluation, and the flight analogue is QuakeSat. A fixed reference cannot
    be talked into moving.
    """
    e, t = commissioned()
    v = e.voltage_reference
    for step in range(60):                      # ease down over 6 s, gently
        v -= 0.008
        e.tick(sample(step, bus_voltage_v=v), t)
        t += 0.1
    assert e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE
    assert diagnose(e.fault_flags, sample()).cause == Cause.DEGRADATION


def test_the_drift_is_caught_while_fixed_thresholds_still_read_healthy():
    """
    The modelled drift ends at 4.30 V -- under the 4.5 V warning but never
    reaching the 4.0 V critical, so an undervoltage detector alone would never
    fire at all.
    """
    e, t = commissioned()
    drive(e, t, 60, bus_voltage_v=4.60)         # 0.40 V off reference, above 4.5
    assert e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE
    assert not (e.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL)


def test_small_deviations_inside_the_band_do_not_latch():
    e, t = commissioned()
    drive(e, t, 60, bus_voltage_v=e.voltage_reference - 0.10)
    assert not (e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE)


def test_the_reference_survives_a_reboot():
    """
    THE ASSERTION THAT MATTERS. Recapturing on boot would let a reboot
    part-way through a drift adopt the drifted value as normal, and the
    detector would go quiet exactly when it mattered -- D2's defect wearing a
    different hat.
    """
    e, t = commissioned()
    saved = e.export_reference_state()
    reference = e.voltage_reference

    fresh = FDIREngine()
    fresh.import_reference_state(saved, t)
    assert fresh.voltage_reference == reference

    # Boot the fresh engine on ALREADY-DRIFTED telemetry. Without persistence it
    # would commission itself at 4.60 V and never report anything again.
    # Long enough to clear BOOT (2 s) and then satisfy the drift debounce (2 s).
    t2 = 0.0
    for i in range(70):
        fresh.tick(sample(i, bus_voltage_v=4.60), t2)
        t2 += 0.1
    assert fresh.voltage_reference == reference
    assert fresh.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE


def test_a_reference_is_not_captured_while_something_is_wrong():
    """A reference learned during a fault is worse than no reference."""
    e = FDIREngine()
    t = 0.0
    for i in range(30):
        e.tick(sample(i), t)
        t += 0.1
    e.voltage_reference = None
    e._reference_samples = []
    e.fault_flags |= FaultFlag.THERMAL_ANOMALY
    drive(e, t, 60, bus_voltage_v=4.2)
    assert e.voltage_reference is None


@pytest.mark.parametrize("bad", [
    {}, {"schema_version": 2, "voltage_reference": 5.0},
    {"schema_version": 1}, {"schema_version": 1, "voltage_reference": "five"},
    {"schema_version": 1, "voltage_reference": float("nan")},
])
def test_corrupt_reference_state_is_discarded(bad):
    e = FDIREngine()
    e.import_reference_state(bad, 0.0)
    assert e.voltage_reference is None


def test_drift_is_deterministic_evidence_not_advisory():
    """
    The distinction R7 turns on. ADAPTIVE_ANOMALY is a statistical hint and may
    not drive anything; DRIFT_FROM_REFERENCE is measured against a fixed
    physical number, which is what lets it select a degraded mode.
    """
    assert int(FaultFlag.DRIFT_FROM_REFERENCE) in DEGRADE_TRIGGERS
    assert int(FaultFlag.ADAPTIVE_ANOMALY) not in DEGRADE_TRIGGERS


# ---------------------------------------------------------------------------
# R8 -- degraded modes
# ---------------------------------------------------------------------------

def test_capability_sets_are_declared_not_measured():
    """
    R8 says PRE-VALIDATED, and pre-validated means measured. Nothing here has
    been. This test exists so that limitation cannot quietly disappear and be
    read later as a measured result.
    """
    for cs in LADDER:
        assert cs.declared_only is True, (
            f"{cs.name} claims to be validated. If a real measurement now backs "
            f"it, update docs/requirements/case-study-traceability.md in the "
            f"same commit -- do not just flip this flag."
        )


def test_the_flight_computer_is_never_shed():
    """The hardware-safety constraint. A configuration without the OBC is not
    a degraded mode, it is an ending."""
    for cs in LADDER:
        assert cs.powers(Rail.OBC), f"{cs.name} would remove OBC power"


def test_the_radio_survives_to_the_last_rung():
    """
    CSSWE. The one asset that must survive a degradation is the one the ground
    needs in order to intervene -- shedding it would be optimising the energy
    balance of a spacecraft nobody can reach.
    """
    assert MINIMAL.powers(Rail.RADIO)


def test_the_ladder_is_ordered_and_each_rung_costs_less():
    assert [cs.level for cs in LADDER] == [0, 1, 2]
    budgets = [cs.budget_w for cs in LADDER]
    assert budgets == sorted(budgets, reverse=True), (
        "a more degraded configuration that costs MORE is not a degradation"
    )


def test_no_advisory_flag_can_cause_a_downgrade():
    """
    The project's central boundary, applied to a third kind of authority.
    Shedding a subsystem changes what the spacecraft can do, so it goes through
    a named gate like everything else.
    """
    for flag in (FaultFlag.ADAPTIVE_ANOMALY, FaultFlag.ML_ANOMALY,
                 FaultFlag.UNKNOWN_ANOMALY, FaultFlag.SENSOR_INVALID):
        assert select_level(flag) == 0, f"{flag.name} must not select a degraded set"


def test_a_measured_degradation_selects_a_reduced_configuration():
    assert select_level(FaultFlag.DRIFT_FROM_REFERENCE) == 1
    assert select_level(FaultFlag.RAIL_OVERCURRENT) == 1


def test_corroborating_evidence_degrades_further():
    both = FaultFlag.DRIFT_FROM_REFERENCE | FaultFlag.RAIL_OVERCURRENT
    assert select_level(both) == 2


def test_the_engine_enters_degraded_and_sheds_the_right_rails():
    e, t = commissioned()
    e.take_pending_intents()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    e.tick(sample(0), t)

    assert e.mode == Mode.DEGRADED
    assert e.capability is REDUCED
    shed = [i.target for i in e.take_pending_intents()]
    assert Rail.PAYLOAD in shed
    assert Rail.OBC not in shed and Rail.RADIO not in shed


def test_degrading_does_not_upgrade_itself_when_the_flag_clears():
    """
    Asymmetric on purpose, and for R9's reason: the conditions that forced a
    downgrade are the ones the vehicle is worst placed to judge resolved, and
    silently restoring payload power is how a spacecraft oscillates.
    """
    e, t = commissioned()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    t = drive(e, t, 2)
    assert e.capability is REDUCED

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    t = drive(e, t, 20)
    assert e.capability is REDUCED, "capability must not return on its own"


def test_an_operator_can_restore_capability_once_the_condition_clears():
    e, t = commissioned()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    t = drive(e, t, 2)
    assert e.restore_capability(t) is False, "refused while the cause is present"

    e.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
    assert e.restore_capability(t) is True
    assert e.capability is FULL
    assert e.mode == Mode.NOMINAL


def test_safe_mode_outranks_degraded():
    """
    Degradation preserves a mission that is still viable. If something can
    command SAFE the mission is not currently viable, and shedding payload is
    not the answer to it.
    """
    e, t = commissioned()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    t = drive(e, t, 2)
    assert e.mode == Mode.DEGRADED

    e.fault_flags |= FaultFlag.THERMAL_ANOMALY
    t = drive(e, t, 2)
    assert e.mode == Mode.SAFE


def test_leaving_safe_does_not_silently_restore_capability():
    e, t = commissioned()
    e.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE
    t = drive(e, t, 2)
    e.enter_safe_mode(t)   # operator command; no SAFE-triggering flag is set

    assert e.exit_safe_mode(t) is True
    assert e.mode == Mode.DEGRADED, "must land in DEGRADED, not NOMINAL"
    assert e.capability is REDUCED


def test_rails_to_shed_names_only_the_difference():
    assert set(rails_to_shed(FULL, REDUCED)) == {int(Rail.PAYLOAD)}
    assert set(rails_to_shed(FULL, MINIMAL)) == {int(Rail.SENSORS), int(Rail.ADCS),
                                                 int(Rail.PAYLOAD)}
    assert rails_to_shed(FULL, FULL) == []
