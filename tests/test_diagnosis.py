"""
Phase 4: data-path discrimination and deterministic diagnosis.

Two things are under test. First, R6 -- telling "this device is bad" from "the
path carrying this device's data is bad", which is the Delfi-C3 case and the
only genuine diagnostic ambiguity the failure research turned up. Second, R10 --
the spacecraft being able to say "I do not know" rather than inventing a cause,
which matters because 63% of the real failure record has no identifiable cause
at all.

Note the discrimination PAIRS. A scenario on its own proves detection; a pair
proves isolation, and four of five documented FDIR failures were isolation or
authority failures rather than missed detections.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))

from environment import SpacecraftEnvironment  # noqa: E402
from fdir.diagnosis import Cause, Confidence, diagnose  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402
from icd import FaultFlag, Rail, RawSample  # noqa: E402

DT = 0.1


def sample(**overrides) -> RawSample:
    base = dict(temp_c=25.0, accel_x=0.0, accel_y=0.0, accel_z=1.0,
                gyro_x=0.0, gyro_y=0.0, gyro_z=0.0,
                mag_x=25.0, mag_y=-8.0, mag_z=40.0,
                bus_voltage_v=5.0, bus_current_a=0.4)
    base.update(overrides)
    return RawSample(**base)


def drive(engine, env, n):
    for _ in range(n):
        s, _t = env.step(DT)
        engine.tick(s, env.t)
    return s


# ---------------------------------------------------------------------------
# R6: the data-path discriminator, end to end through the environment
# ---------------------------------------------------------------------------

def test_bus_failure_is_diagnosed_as_the_path_not_the_devices():
    env = SpacecraftEnvironment(seed=1)
    engine = FDIREngine()
    drive(engine, env, 30)

    env.inject("data_bus_failure")
    drive(engine, env, 20)

    assert engine.fault_flags & FaultFlag.DATA_PATH_SUSPECT
    assert engine.diagnosis.cause == Cause.DATA_PATH
    assert not (engine.fault_flags & FaultFlag.SENSOR_LOCKUP), (
        "the devices are fine -- only their shared path is not"
    )


def test_bus_failure_does_not_command_safe():
    """
    Delfi-C3's actual harm: protective responses fired against subsystems that
    were themselves fine. A path fault must not reach SAFE through a
    per-channel detector.
    """
    from icd import Mode

    env = SpacecraftEnvironment(seed=2)
    engine = FDIREngine()
    drive(engine, env, 30)
    env.inject("data_bus_failure")
    drive(engine, env, 40)

    assert engine.mode != Mode.SAFE


def test_diagnosis_prefers_the_path_over_the_symptoms_beneath_it():
    """
    A suspect shared bus EXPLAINS AWAY the per-device symptoms under it. If both
    are latched, reporting the frozen sensor would be counting one finding twice
    and would authorise the wrong action.
    """
    d = diagnose(FaultFlag.DATA_PATH_SUSPECT | FaultFlag.SENSOR_LOCKUP
                 | FaultFlag.SENSOR_TIMEOUT)
    assert d.cause == Cause.DATA_PATH


# ---------------------------------------------------------------------------
# The discrimination pair that quantifies the hardware argument
# ---------------------------------------------------------------------------

def test_per_rail_current_is_what_separates_latchup_from_a_quiet_link():
    """
    THE HARDWARE-PURCHASE MEASUREMENT, in miniature.

    Comms loss looks identical on the link whether the radio latched up or the
    ground station simply went quiet. Per-rail current is the only channel that
    separates them. With it, the diagnosis is LIKELY and may authorise action;
    without it, the system must honestly report POSSIBLE and refuse to act.

    That refusal is the argument for per-rail sensing stated as behaviour rather
    than as an opinion.
    """
    latched = diagnose(FaultFlag.COMMS_LOSS,
                       sample(rail_current_a={int(Rail.RADIO): 0.45}))
    quiet = diagnose(FaultFlag.COMMS_LOSS,
                     sample(rail_current_a={int(Rail.RADIO): 0.10}))
    blind = diagnose(FaultFlag.COMMS_LOSS, sample())   # no per-rail sensing

    assert latched.cause == Cause.RADIO_LATCHUP
    assert quiet.cause == Cause.GROUND_LINK_LOST
    assert latched.cause != quiet.cause, "the pair must reach different diagnoses"

    assert blind.confidence == Confidence.POSSIBLE
    assert not blind.authorises_action, (
        "without the sensing that distinguishes them, the system must not act "
        "on a guess"
    )
    assert latched.authorises_action and quiet.authorises_action


# ---------------------------------------------------------------------------
# R10: "I do not know" is a first-class answer
# ---------------------------------------------------------------------------

def test_advisory_only_evidence_yields_unknown_not_an_invented_cause():
    """
    A learned or statistical detector can say "this is unusual". It cannot say
    what is wrong. The correct output is UNKNOWN -- a confident wrong label is
    worse than no label, because it authorises the wrong action.
    """
    for flag in (FaultFlag.ML_ANOMALY, FaultFlag.ADAPTIVE_ANOMALY):
        d = diagnose(flag)
        assert d.cause == Cause.UNKNOWN
        assert d.confidence == Confidence.NONE
        assert not d.authorises_action


def test_unknown_anomaly_flag_is_raised_and_no_action_is_taken():
    """R10 end-to-end: the uncertainty is visible in telemetry, not swallowed."""
    env = SpacecraftEnvironment(seed=3)
    engine = FDIREngine()
    drive(engine, env, 30)

    # Force the advisory flag directly -- this test is about what the diagnosis
    # layer does with unexplained evidence, not about how it got there.
    engine.fault_flags |= FaultFlag.ML_ANOMALY
    drive(engine, env, 5)

    assert engine.fault_flags & FaultFlag.UNKNOWN_ANOMALY
    assert engine.diagnosis.cause == Cause.UNKNOWN
    assert engine.pending_intents == [], "an unknown cause must not authorise an action"
    assert any("UNKNOWN_ANOMALY" in msg for _, msg in engine.log)


def test_no_fault_yields_unknown_with_no_anomaly_flag():
    """A quiet spacecraft is not an unknown anomaly."""
    env = SpacecraftEnvironment(seed=4)
    engine = FDIREngine()
    drive(engine, env, 40)

    assert engine.diagnosis.cause == Cause.UNKNOWN
    assert engine.diagnosis.evidence == "no fault indication"
    assert not (engine.fault_flags & FaultFlag.UNKNOWN_ANOMALY)


@pytest.mark.parametrize("flags,expected", [
    (FaultFlag.UNDERVOLTAGE_CRITICAL, Cause.POWER_UNDERVOLTAGE),
    (FaultFlag.THERMAL_ANOMALY, Cause.THERMAL),
    (FaultFlag.SENSOR_LOCKUP, Cause.SENSOR_FROZEN),
    (FaultFlag.SENSOR_TIMEOUT, Cause.SENSOR_NOT_RESPONDING),
    (FaultFlag.RECOVERY_FAILED, Cause.RECOVERY_EXHAUSTED),
])
def test_deterministic_causes_map_as_documented(flags, expected):
    assert diagnose(flags).cause == expected


def test_diagnosis_is_a_pure_function():
    """No state, no I/O -- so it ports to C as a switch and is trivially testable."""
    flags = FaultFlag.THERMAL_ANOMALY
    first = diagnose(flags)
    second = diagnose(flags)
    assert (first.cause, first.confidence) == (second.cause, second.confidence)
