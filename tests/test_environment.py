"""
Tests for simulator/environment.py.

Scope: the environment's own contract -- reproducibility given a seed, and
that each injected fault produces the observable signature FDIR is supposed
to detect from raw sensor values alone. These tests read GroundTruth (the
answer key) freely, same as a dataset generator would; fdir/engine.py itself
must never do that, but this test file is allowed to.

No debounce logic lives in environment.py (that's fdir/engine.py's job), so
a fault's signature is expected to show up on the very first .step() after
injection, not after some settling window.
"""

import dataclasses
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))                  # for `fdir` (a real package)
sys.path.insert(0, str(_ROOT / "simulator"))     # for `environment` (not a package, see protocol.py's own pattern)

from environment import (  # noqa: E402
    DRIFT_FLOOR_V,
    DRIFT_RAMP_DURATION_S,
    FAULT_TYPES,
    NOMINAL_VOLTAGE_V,
    SpacecraftEnvironment,
)
from fdir import config as cfg  # noqa: E402

IMU_FIELDS = ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")


def _imu_tuple(sample) -> tuple:
    return tuple(getattr(sample, f) for f in IMU_FIELDS)


# ---- reproducibility -------------------------------------------------


def test_same_seed_produces_byte_identical_samples_nominal():
    """Two independent instances, same seed, no faults: every RawSample field
    must match exactly (not approximately) at every step."""
    env_a = SpacecraftEnvironment(seed=1234)
    env_b = SpacecraftEnvironment(seed=1234)

    for _ in range(200):
        sample_a, truth_a = env_a.step(0.1)
        sample_b, truth_b = env_b.step(0.1)
        assert dataclasses.astuple(sample_a) == dataclasses.astuple(sample_b)
        assert truth_a.t == truth_b.t
        assert truth_a.active_faults == truth_b.active_faults == []


def test_same_seed_produces_byte_identical_samples_with_faults():
    """Same seed + same fault schedule (inject/clear called at identical
    points in the deterministic step sequence) must still replay identically
    -- fault injection consumes rng draws too (e.g. the frozen lockup
    snapshot), so this is the case most likely to break if the RNG usage
    inside inject()/step() were ever accidentally made order-dependent on
    something other than the seed."""
    env_a = SpacecraftEnvironment(seed=99)
    env_b = SpacecraftEnvironment(seed=99)

    for i in range(60):
        if i == 10:
            env_a.inject("thermal")
            env_b.inject("thermal")
        if i == 20:
            env_a.inject("sensor_lockup")
            env_b.inject("sensor_lockup")
        if i == 40:
            env_a.clear("thermal")
            env_b.clear("thermal")
        sample_a, truth_a = env_a.step(0.05)
        sample_b, truth_b = env_b.step(0.05)
        assert dataclasses.astuple(sample_a) == dataclasses.astuple(sample_b)
        assert truth_a.active_faults == truth_b.active_faults


def test_different_seeds_diverge():
    env_a = SpacecraftEnvironment(seed=1)
    env_b = SpacecraftEnvironment(seed=2)

    samples_a = [dataclasses.astuple(env_a.step(0.1)[0]) for _ in range(20)]
    samples_b = [dataclasses.astuple(env_b.step(0.1)[0]) for _ in range(20)]

    assert samples_a != samples_b
    # every step should differ, not just "the sequences aren't equal overall"
    assert all(a != b for a, b in zip(samples_a, samples_b))


# ---- per-fault observable signatures -------------------------------------------------


def test_undervoltage_drops_below_critical_threshold():
    env = SpacecraftEnvironment(seed=7)
    env.inject("undervoltage")
    for _ in range(20):
        sample, truth = env.step(0.1)
        assert sample.bus_voltage_v < cfg.UNDERVOLTAGE_CRITICAL_V
        assert truth.active_faults == ["undervoltage"]


def test_thermal_leaves_critical_band():
    env = SpacecraftEnvironment(seed=7)
    env.inject("thermal")
    for _ in range(20):
        sample, truth = env.step(0.1)
        assert not (cfg.THERMAL_CRITICAL_LOW_C <= sample.temp_c <= cfg.THERMAL_CRITICAL_HIGH_C)
        assert truth.active_faults == ["thermal"]


def test_sensor_timeout_sets_imu_not_responded():
    env = SpacecraftEnvironment(seed=7)
    env.inject("sensor_timeout")
    for _ in range(10):
        sample, truth = env.step(0.1)
        assert sample.imu_responded is False
        assert truth.active_faults == ["sensor_timeout"]


def test_sensor_lockup_freezes_imu_values_while_still_responding():
    env = SpacecraftEnvironment(seed=7)
    env.inject("sensor_lockup")

    samples = [env.step(0.1)[0] for _ in range(10)]

    assert all(s.imu_responded is True for s in samples)  # unlike timeout: it still ACKs
    imu_tuples = {_imu_tuple(s) for s in samples}
    assert len(imu_tuples) == 1  # every consecutive reading is exactly the same frozen value


def test_gradual_drift_decreases_and_never_breaches_critical():
    env = SpacecraftEnvironment(seed=7)
    env.inject("gradual_drift")

    dt = 1.0
    n_steps = int(DRIFT_RAMP_DURATION_S) + 10  # run past the end of the ramp onto the floor plateau
    voltages = []
    for _ in range(n_steps):
        sample, truth = env.step(dt)
        voltages.append(sample.bus_voltage_v)
        assert truth.active_faults == ["gradual_drift"]
        # the entire point of this fault: never trips the fixed critical threshold
        assert sample.bus_voltage_v >= cfg.UNDERVOLTAGE_CRITICAL_V

    # "monotonically-ish": individual samples are noisy (gauss jitter on top of
    # the ramp), so assert the overall downward trend via correlation with time
    # rather than requiring every single step-to-step delta to be non-positive.
    voltages = np.array(voltages)
    correlation = np.corrcoef(np.arange(len(voltages)), voltages)[0, 1]
    assert correlation < -0.8

    # settles near (not below) the drift floor once the ramp has finished
    tail_avg = voltages[-5:].mean()
    assert tail_avg == pytest.approx(DRIFT_FLOOR_V, abs=0.15)
    assert tail_avg < NOMINAL_VOLTAGE_V


# ---- clear() / clear_all() -------------------------------------------------


@pytest.mark.parametrize("fault_name", FAULT_TYPES)
def test_clear_removes_fault_from_ground_truth(fault_name):
    env = SpacecraftEnvironment(seed=42)
    env.inject(fault_name)
    _, truth = env.step(0.1)
    assert truth.active_faults == [fault_name]

    env.clear(fault_name)
    _, truth = env.step(0.1)
    assert truth.active_faults == []


def test_clear_undervoltage_restores_nominal_voltage():
    env = SpacecraftEnvironment(seed=42)
    env.inject("undervoltage")
    sample, _ = env.step(0.1)
    assert sample.bus_voltage_v < cfg.UNDERVOLTAGE_CRITICAL_V

    env.clear("undervoltage")
    sample, _ = env.step(0.1)
    assert sample.bus_voltage_v > cfg.UNDERVOLTAGE_WARNING_V


def test_clear_thermal_restores_nominal_temperature():
    env = SpacecraftEnvironment(seed=42)
    env.inject("thermal")
    sample, _ = env.step(0.1)
    assert not (cfg.THERMAL_CRITICAL_LOW_C <= sample.temp_c <= cfg.THERMAL_CRITICAL_HIGH_C)

    env.clear("thermal")
    sample, _ = env.step(0.1)
    assert cfg.THERMAL_CRITICAL_LOW_C <= sample.temp_c <= cfg.THERMAL_CRITICAL_HIGH_C


def test_clear_sensor_timeout_restores_imu_responded():
    env = SpacecraftEnvironment(seed=42)
    env.inject("sensor_timeout")
    sample, _ = env.step(0.1)
    assert sample.imu_responded is False

    env.clear("sensor_timeout")
    sample, _ = env.step(0.1)
    assert sample.imu_responded is True


def test_clear_sensor_lockup_unfreezes_imu_values():
    env = SpacecraftEnvironment(seed=42)
    env.inject("sensor_lockup")
    frozen = [env.step(0.1)[0] for _ in range(5)]
    assert len({_imu_tuple(s) for s in frozen}) == 1

    env.clear("sensor_lockup")
    unfrozen = [env.step(0.1)[0] for _ in range(5)]
    unfrozen_tuples = [_imu_tuple(s) for s in unfrozen]
    assert len(set(unfrozen_tuples)) == len(unfrozen_tuples)  # back to varying every step


def test_clear_gradual_drift_stops_the_ramp():
    env = SpacecraftEnvironment(seed=42)
    env.inject("gradual_drift")
    for _ in range(15):  # partway into the ramp, well below nominal
        sample, _ = env.step(1.0)
    assert sample.bus_voltage_v < NOMINAL_VOLTAGE_V - 0.1

    env.clear("gradual_drift")
    sample, _ = env.step(0.1)
    assert sample.bus_voltage_v == pytest.approx(NOMINAL_VOLTAGE_V, abs=0.15)


def test_clear_all_stops_every_active_fault():
    env = SpacecraftEnvironment(seed=42)
    env.inject("thermal")
    env.inject("undervoltage")
    _, truth = env.step(0.1)
    assert set(truth.active_faults) == {"thermal", "undervoltage"}

    env.clear_all()
    sample, truth = env.step(0.1)
    assert truth.active_faults == []
    assert cfg.THERMAL_CRITICAL_LOW_C <= sample.temp_c <= cfg.THERMAL_CRITICAL_HIGH_C
    assert sample.bus_voltage_v > cfg.UNDERVOLTAGE_WARNING_V
