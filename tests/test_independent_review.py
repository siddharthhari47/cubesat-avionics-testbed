"""
Regression tests for the independent review (N1-N6).

These came from an adversarial pass over code written the same day -- code I had
just tested, documented, and convinced myself about. Every one is real, and every
one was reproduced by probe before being fixed.

Worth recording why they matter beyond the individual bugs: four of the six are
the same two mistakes this project keeps making.

  * A latched flag read as if it were live state (N5, and the outcome
    classification alongside it). That is the F1 defect class, now found for the
    fifth and sixth time.
  * State read before the thing that writes it has run this tick (N1).

The review that found them was itself cut short -- 13 of 15 agents died on a
spend limit, including every refuter -- so "0 confirmed" in its output meant the
verifiers never ran, not that the findings were wrong.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fdir import config as cfg  # noqa: E402
from fdir.diagnosis import Cause, diagnose  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402
from fdir.executor import RecoveryExecutor  # noqa: E402
from icd import FaultFlag, HealthFlag, Mode, Rail, RawSample  # noqa: E402

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


def nominal(n=30):
    e = FDIREngine()
    t = 0.0
    for i in range(n):
        e.tick(sample(i), t)
        t += 0.1
    return e, t


def drive(e, t, n, **kw):
    for i in range(n):
        e.tick(sample(i, **kw), t)
        t += 0.1
    return t


# --- N1: stale flag read, and the trap it created --------------------------

def test_commissioning_rejects_low_samples_regardless_of_detector_order():
    """
    The guard read UNDERVOLTAGE_WARNING, which _update_undervoltage writes LATER
    in the same tick -- so it evaluated the previous tick's verdict and inverted
    itself exactly: a low sample was admitted because its flag was not set yet,
    and the good sample after it was rejected because the flag was still set
    from before. On an alternating supply the reference captured 4.24 V instead
    of 5.00 V.
    """
    e, t = nominal(25)
    e.voltage_reference = None
    e._reference_samples = []
    for i in range(60):
        e.tick(sample(i, bus_voltage_v=4.2 if i % 2 else 5.0), t)
        t += 0.1
    assert e.voltage_reference == pytest.approx(5.0, abs=0.05), (
        "only clean samples may contribute to the commissioning reference"
    )


def test_the_drift_detector_runs_after_the_flags_it_reads():
    import inspect
    order = [ln.strip() for ln in inspect.getsource(FDIREngine.tick).splitlines()
             if "self._update_" in ln]
    drift = next(i for i, ln in enumerate(order) if "reference_drift" in ln)
    under = next(i for i, ln in enumerate(order) if "undervoltage" in ln)
    assert drift > under, (
        "the drift detector consults verdicts the undervoltage detector writes; "
        "running first makes it read the previous tick"
    )


def test_a_bad_reference_is_escapable():
    """
    THE TRAP. A wrong reference makes DRIFT_FROM_REFERENCE latch on perfectly
    nominal telemetry, which selects a degraded capability set -- and there was
    no way back. The flag cannot clear, because the condition really is
    breaching against that reference, so RESET_FAULTS refuses forever and
    restore_capability() refuses with it. The vehicle sheds its payload for the
    rest of the mission.

    A latching detector whose reference can be wrong needs a way to correct the
    reference, or it is a trap rather than a detector.
    """
    class AcceptingPort:
        def set_enabled(self, dev, on):
            return True

        def is_enabled(self, dev):
            return True

    e, t = nominal(25)
    ex = RecoveryExecutor(AcceptingPort(), None)
    e.voltage_reference = 4.24                      # as if captured badly
    for i in range(80):                             # perfectly nominal telemetry
        e.tick(sample(i), t)
        ex.step(e, t)
        t += 0.1

    assert e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE
    assert e.capability.level > 0, "the vehicle degraded over a measurement error"
    assert e.reset_faults(t)[0] == FaultFlag.NONE, (
        "the flag cannot clear -- the condition really IS breaching against a "
        "reference that is itself wrong"
    )
    assert e.restore_capability(t) is False, "and so capability cannot be restored"

    # Without recommission_reference() the two assertions above are the end of
    # the story, for the rest of the mission.

    e.recommission_reference(t)
    for i in range(40):
        e.tick(sample(i), t)
        ex.step(e, t)
        t += 0.1
    assert not (e.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE)
    assert e.voltage_reference == pytest.approx(5.0, abs=0.05)
    assert e.restore_capability(t) is True


# --- N2: suppressed plausibility recorded clean evidence -------------------

def test_a_suppressed_device_fault_records_no_evidence_either_way():
    """
    While DATA_PATH_SUSPECT is up, every device on the bus looks broken. Counting
    those ticks as proof the devices are FINE is the F3/D2 defect class: it lets
    RESET_FAULTS clear SENSOR_IMPLAUSIBLE with the channels still suspect.
    """
    e, t = nominal(25)
    before = e._clean_ticks.get(int(FaultFlag.SENSOR_IMPLAUSIBLE), 0)
    zeroed = dict(accel_x=0.0, accel_y=0.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0,
                  gyro_z=0.0, mag_x=0.0, mag_y=0.0, mag_z=0.0, temp_c=0.0)
    drive(e, t, 15, **zeroed)
    assert e.fault_flags & FaultFlag.DATA_PATH_SUSPECT
    assert e._suspect_now, "devices are still suspect"
    assert e._clean_ticks.get(int(FaultFlag.SENSOR_IMPLAUSIBLE), 0) <= before, (
        "suppression must not manufacture evidence that the devices are healthy"
    )


# --- N3: MAG_OK cleared too early and never restored -----------------------

def test_a_one_tick_glitch_does_not_mark_the_magnetometer_unhealthy():
    e, t = nominal(25)
    e.tick(sample(0, mag_x=0.0, mag_y=0.0, mag_z=0.0), t)
    assert e.health_flags & HealthFlag.MAG_OK, (
        "the health bit was cleared before the debounce that exists to forgive "
        "exactly this"
    )


def test_mag_health_is_restored_when_the_device_recovers():
    """
    Nothing else in the system sets MAG_OK, so if this detector does not restore
    it, nothing ever will -- it survived 300 clean ticks, RESET_FAULTS and a
    watchdog reset.
    """
    e, t = nominal(25)
    t = drive(e, t, cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES + 3,
              mag_x=0.0, mag_y=0.0, mag_z=0.0)
    assert not (e.health_flags & HealthFlag.MAG_OK)
    t = drive(e, t, 20)
    assert e.health_flags & HealthFlag.MAG_OK


# --- N4: capability restore overwrote BOOT ---------------------------------

def test_restoring_capability_does_not_skip_the_boot_sequence():
    """
    Writing DEGRADED over BOOT erased the boot self-check and every warm-up gate
    hanging off it, latching COMMS_LOSS on the first post-reset link report.
    """
    e = FDIREngine()
    e.watchdog_reset(0.0)
    e.import_capability_state(
        # schema 2 persists the RAIL BELIEF, not a level derived from it --
        # storing the level let capability and _rails_on disagree from the
        # moment of restore. REDUCED = everything but PAYLOAD.
        {"schema_version": 2, "rails_on": [0, 1, 2, 3]}, 0.0)
    assert e.mode == Mode.BOOT
    assert e.capability.level == 1


def test_mode_follows_capability_once_boot_completes():
    """
    The other half: after a reset the flags that caused the downgrade are gone,
    so nothing argued for DEGRADED -- the vehicle sat at REDUCED capability
    while reporting NOMINAL, telling the ground it was fully capable with a rail
    physically off.
    """
    e = FDIREngine()
    e.watchdog_reset(0.0)
    e.import_capability_state(
        # schema 2 persists the RAIL BELIEF, not a level derived from it --
        # storing the level let capability and _rails_on disagree from the
        # moment of restore. REDUCED = everything but PAYLOAD.
        {"schema_version": 2, "rails_on": [0, 1, 2, 3]}, 0.0)
    drive(e, 0.0, 40)
    assert e.mode == Mode.DEGRADED
    assert e.capability.level == 1


# --- N5: a latched overcurrent asserted with no live evidence --------------

def test_a_cleared_overcurrent_does_not_mask_a_live_undervoltage():
    """
    RAIL_OVERCURRENT latches, so the bit outlives the condition. The rule fired
    on the stale bit alone and asserted "any voltage sag is downstream of it"
    with every rail at nominal -- masking a live undervoltage and stating the
    exact inverse of the truth, at LIKELY confidence.
    """
    d = diagnose(FaultFlag.RAIL_OVERCURRENT | FaultFlag.UNDERVOLTAGE_CRITICAL,
                 sample(bus_voltage_v=3.8))
    assert d.cause == Cause.POWER_UNDERVOLTAGE


def test_a_live_overcurrent_still_outranks_the_undervoltage_it_causes():
    """The KySat-2 ordering has to survive the fix above."""
    hot = {**RAILS, int(Rail.PAYLOAD): 1.2}
    d = diagnose(FaultFlag.RAIL_OVERCURRENT | FaultFlag.UNDERVOLTAGE_CRITICAL,
                 sample(rails=hot, bus_voltage_v=3.8))
    assert d.cause == Cause.RAIL_OVERCURRENT


# --- N6: comms loss masked every non-radio overcurrent ---------------------

@pytest.mark.parametrize("rail", [Rail.ADCS, Rail.SENSORS, Rail.PAYLOAD])
def test_comms_loss_does_not_mask_an_overcurrent_on_another_rail(rail):
    """
    The COMMS_LOSS rule returns on every path and inspects only the RADIO rail,
    so a live overcurrent elsewhere went undiagnosed whenever ground contact
    happened to be stale -- while the evidence string actively exonerated the
    power system. COMMS_LOSS is set by any 5 s gap in contact, so this is not an
    exotic combination.
    """
    hot = {**RAILS, int(rail): 1.4}
    d = diagnose(FaultFlag.COMMS_LOSS | FaultFlag.RAIL_OVERCURRENT, sample(rails=hot))
    assert d.cause == Cause.RAIL_OVERCURRENT
    assert rail.name in d.evidence


def test_a_radio_overcurrent_with_comms_loss_is_still_a_latch_up():
    """Moving the rule above COMMS_LOSS must not cost the more specific answer."""
    hot = {**RAILS, int(Rail.RADIO): 1.0}
    d = diagnose(FaultFlag.COMMS_LOSS | FaultFlag.RAIL_OVERCURRENT, sample(rails=hot))
    assert d.cause == Cause.RADIO_LATCHUP


def test_comms_loss_alone_is_unaffected():
    assert diagnose(FaultFlag.COMMS_LOSS, sample()).cause == Cause.GROUND_LINK_LOST
