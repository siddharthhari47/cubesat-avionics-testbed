"""
FDIR-011 (rail overcurrent) and FDIR-012 (per-channel plausibility).

Both close gaps the scenario suite had been reporting honestly as **undetected**
for several phases. In each case the data the detector needs was already in
RawSample and nothing consumed it.

See docs/architecture/v0-scenario-results.md for the measured before/after.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fdir import config as cfg  # noqa: E402
from fdir.diagnosis import Cause, diagnose  # noqa: E402
from fdir.engine import (  # noqa: E402
    RECOVERY_AUTHORITY_FLAGS, RESETTABLE_FLAGS, SAFE_MODE_TRIGGER_FLAGS, FDIREngine,
)
from icd import FaultFlag, HealthFlag, Mode, Rail, RawSample  # noqa: E402

NOMINAL_RAILS = {int(Rail.OBC): 0.12, int(Rail.RADIO): 0.10,
                 int(Rail.SENSORS): 0.06, int(Rail.ADCS): 0.08,
                 int(Rail.PAYLOAD): 0.04}


def sample(i=0, rails=None, **kw):
    d = dict(temp_c=25.0, accel_x=0.1 + i * 1e-6, accel_y=0.2, accel_z=9.8,
             gyro_x=0.01 + i * 1e-7, gyro_y=0.02, gyro_z=0.03,
             mag_x=20.0, mag_y=5.0, mag_z=-40.0,
             bus_voltage_v=5.0, bus_current_a=0.40,
             rail_current_a=dict(NOMINAL_RAILS if rails is None else rails))
    d.update(kw)
    return RawSample(**d)


def nominal(n=30):
    e = FDIREngine()
    t = 0.0
    for i in range(n):
        e.tick(sample(i), t)
        t += 0.1
    assert e.mode == Mode.NOMINAL
    return e, t


def drive(e, t, n, **kw):
    for i in range(n):
        e.tick(sample(i, **kw), t)
        t += 0.1
    return t


HOT_PAYLOAD = {**NOMINAL_RAILS, int(Rail.PAYLOAD): 1.2}
HOT_RADIO = {**NOMINAL_RAILS, int(Rail.RADIO): 1.0}
ZERO_MAG = dict(mag_x=0.0, mag_y=0.0, mag_z=0.0)
ZERO_BUS = dict(accel_x=0.0, accel_y=0.0, accel_z=0.0, gyro_x=0.0, gyro_y=0.0,
                gyro_z=0.0, mag_x=0.0, mag_y=0.0, mag_z=0.0, temp_c=0.0)


# ---------------------------------------------------------------------------
# FDIR-011 -- rail overcurrent
# ---------------------------------------------------------------------------

def test_a_rail_over_its_ceiling_latches():
    """
    The KySat-2 gap. A rail eating the battery used to be entirely invisible:
    per-rail current sat in RawSample and no detector consumed it.
    """
    e, t = nominal()
    drive(e, t, 20, rails=HOT_PAYLOAD)
    assert e.fault_flags & FaultFlag.RAIL_OVERCURRENT


def test_it_is_caught_on_current_before_any_voltage_threshold_moves():
    """
    The whole point of the requirement. Catching this as an undervoltage means
    catching it after the battery is already drained, which is the failure
    rather than the fix.
    """
    e, t = nominal()
    drive(e, t, 20, rails=HOT_PAYLOAD, bus_voltage_v=5.0)   # voltage still nominal
    assert e.fault_flags & FaultFlag.RAIL_OVERCURRENT
    assert not (e.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL)


def test_nominal_draws_do_not_trip_it():
    e, t = nominal()
    drive(e, t, 60)
    assert not (e.fault_flags & FaultFlag.RAIL_OVERCURRENT)
    assert max(NOMINAL_RAILS.values()) < cfg.RAIL_NOMINAL_CURRENT_CEILING_A, (
        "the ceiling must sit above every nominal draw, or this detector is a "
        "permanent false positive"
    )


def test_a_single_spike_does_not_latch():
    """Debounced like every other detector."""
    e, t = nominal()
    e.tick(sample(0, rails=HOT_PAYLOAD), t)
    assert not (e.fault_flags & FaultFlag.RAIL_OVERCURRENT)


def test_without_per_rail_sensing_the_detector_cannot_exist():
    """
    Not a limitation to work around. The measured difference between this and
    the sighted case is the argument for buying the hardware, and inventing a
    fault from the bus total would be guessing which rail.
    """
    e = FDIREngine()
    t = 0.0
    for i in range(30):
        e.tick(sample(i), t)
        t += 0.1
    for i in range(30):
        e.tick(sample(i, bus_current_a=1.6, rail_current_a=None), t)
        t += 0.1
    assert not (e.fault_flags & FaultFlag.RAIL_OVERCURRENT)


def test_overcurrent_authorises_action_but_not_safe_mode():
    """
    Recovery authority YES: the correct response is specific and known -- remove
    power from that rail -- and it is exactly what KySat-2 needed and never got.
    SAFE authority NO: a payload rail must not be able to safe the whole vehicle.
    """
    assert FaultFlag.RAIL_OVERCURRENT & RECOVERY_AUTHORITY_FLAGS
    assert not (FaultFlag.RAIL_OVERCURRENT & SAFE_MODE_TRIGGER_FLAGS)


def test_overcurrent_outranks_the_undervoltage_it_causes():
    """
    Rule ordering IS the KySat-2 lesson: the drain and the sag are one fault,
    and diagnosing the sag treats a symptom while the cause keeps draining.
    """
    d = diagnose(FaultFlag.RAIL_OVERCURRENT | FaultFlag.UNDERVOLTAGE_CRITICAL,
                 sample(rails=HOT_PAYLOAD))
    assert d.cause == Cause.RAIL_OVERCURRENT


def test_a_radio_rail_overcurrent_is_named_as_a_latch_up():
    """
    More specific than a generic overcurrent, and reachable about 5 s earlier
    than the comms symptom the diagnosis would otherwise have to wait for.
    """
    d = diagnose(FaultFlag.RAIL_OVERCURRENT, sample(rails=HOT_RADIO))
    assert d.cause == Cause.RADIO_LATCHUP


def test_overcurrent_clears_once_the_draw_returns_to_normal():
    e, t = nominal()
    t = drive(e, t, 20, rails=HOT_PAYLOAD)
    assert e.fault_flags & FaultFlag.RAIL_OVERCURRENT
    t = drive(e, t, 20)
    cleared, _still = e.reset_faults(t)
    assert cleared & FaultFlag.RAIL_OVERCURRENT


# ---------------------------------------------------------------------------
# FDIR-012 -- one device, its bus healthy
# ---------------------------------------------------------------------------

def test_one_corrupt_device_is_detected():
    """
    `sensor_corruption` was reported UNDETECTED by the scenario suite for
    several phases. `_suspect_devices()` had already identified the channel --
    it has to, in order to count devices per bus -- but a lone suspect device
    latched nothing at all. The asymmetry was never intentional.
    """
    e, t = nominal()
    drive(e, t, cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES + 3, **ZERO_MAG)
    assert e.fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE
    assert not (e.health_flags & HealthFlag.MAG_OK)


def test_one_corrupt_device_is_not_a_path_fault():
    """The Delfi-C3 discrimination, asserted from the other side."""
    e, t = nominal()
    drive(e, t, cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES + 3, **ZERO_MAG)
    assert not (e.fault_flags & FaultFlag.DATA_PATH_SUSPECT)
    assert diagnose(e.fault_flags, sample(**ZERO_MAG)).cause == Cause.SENSOR_CORRUPT


def test_a_whole_zeroed_bus_is_a_path_fault_not_three_device_faults():
    """
    Reporting three device faults underneath a path fault would count one
    finding several times over -- the double-count diagnose()'s rule order
    exists to avoid.
    """
    e, t = nominal()
    drive(e, t, cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES + 5, **ZERO_BUS)
    assert e.fault_flags & FaultFlag.DATA_PATH_SUSPECT
    assert not (e.fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE)
    assert diagnose(e.fault_flags, sample(**ZERO_BUS)).cause == Cause.DATA_PATH


def test_a_brief_glitch_does_not_latch():
    e, t = nominal()
    e.tick(sample(0, **ZERO_MAG), t)
    assert not (e.fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE)


def test_implausible_carries_no_authority_at_all():
    """
    A channel returning impossible values says the DATA is untrustworthy, not
    that the vehicle is in danger, and it does not identify a cause -- the
    sensor, its wiring and its connector all fit the evidence equally well.
    Acting on it would be inventing a diagnosis, which R10 exists to prevent.
    """
    assert not (FaultFlag.SENSOR_IMPLAUSIBLE & SAFE_MODE_TRIGGER_FLAGS)
    assert not (FaultFlag.SENSOR_IMPLAUSIBLE & RECOVERY_AUTHORITY_FLAGS)


def test_implausible_clears_once_the_device_recovers():
    e, t = nominal()
    t = drive(e, t, cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES + 3, **ZERO_MAG)
    assert e.fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE
    t = drive(e, t, 20)
    cleared, _still = e.reset_faults(t)
    assert cleared & FaultFlag.SENSOR_IMPLAUSIBLE


def test_non_finite_is_sensor_invalid_not_implausible():
    """
    Two distinct conditions. NaN is "not a number"; an exact zero across every
    axis is "a number that cannot be true". Conflating them would lose the
    distinction and mislabel a dead ADC as a corrupt one.
    """
    nan = float("nan")
    e, t = nominal()
    drive(e, t, cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES + 3,
          mag_x=nan, mag_y=nan, mag_z=nan)
    assert e.fault_flags & FaultFlag.SENSOR_INVALID
    assert not (e.fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE)


@pytest.mark.parametrize("flag", [FaultFlag.RAIL_OVERCURRENT,
                                  FaultFlag.SENSOR_IMPLAUSIBLE])
def test_new_flags_are_clearable(flag):
    """Neither may repeat the F1/F5 defect."""
    assert flag & RESETTABLE_FLAGS
