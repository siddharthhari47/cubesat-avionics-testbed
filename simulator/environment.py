"""
Seedable spacecraft environment: sensor physics + fault injection.

This is deliberately separate from fdir/engine.py. FDIR decides system state
from what it can observe; this module decides what a (possibly faulty)
spacecraft would physically produce. Neither should know about the other --
FDIR must never read this module's ground truth, and this module has no
concept of BOOT/NOMINAL/SAFE/TEST at all, only sensor values.

It's also separate from run_simulator.py's TCP-serving concerns so it can run
in two modes without duplicating the fault-generation logic: real-time (paced
with time.sleep, feeding the live dashboard) and fast/headless (no pacing, used
by dataset_gen.py to generate hours of labeled telemetry in seconds).

Reproducibility: every source of randomness goes through a single
random.Random(seed) instance owned by the environment, never the `random`
module's global state. Same seed + same fault schedule = byte-identical
telemetry -- required for evaluating detectors against ground truth (V0's
original simulator used the global `random` module, which made runs
irreproducible; that was fine for a live demo and wrong for anything that
needs to be reproduced or scored, so it's fixed here rather than carried
forward).
"""

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fdir.engine import RawSample  # noqa: E402

NOMINAL_VOLTAGE_V = 5.0
NOMINAL_CURRENT_A = 0.4
NOMINAL_TEMP_C = 25.0

UNDERVOLTAGE_INJECTED_V = 3.8
THERMAL_INJECTED_C = 70.0

# Gradual drift: linear ramp on bus_voltage_v from nominal down to this floor
# over DRIFT_RAMP_DURATION_S. Deliberately stays above the FDIR-003 critical
# threshold (4.0V, see fdir/config.py) throughout -- the point of this fault is
# to be invisible to a fixed threshold while still being a real deviation from
# learned-normal, which is what FDIR-006's adaptive baseline exists to catch.
DRIFT_FLOOR_V = 4.3
DRIFT_RAMP_DURATION_S = 30.0

FAULT_TYPES = (
    "sensor_timeout",   # IMU stops ACKing entirely
    "sensor_lockup",    # IMU ACKs but returns a frozen value
    "undervoltage",     # step drop below the critical threshold
    "gradual_drift",    # linear ramp, stays above the critical threshold
    "thermal",          # step spike outside the thermal band
)


@dataclass
class GroundTruth:
    """
    The answer key. Available to the test harness and dataset generator, never
    to fdir/engine.py or to ml/ inference at deployment time -- only at
    training/evaluation time, where knowing what actually happened is the
    entire point.
    """

    t: float
    active_faults: List[str]


class SpacecraftEnvironment:
    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.t = 0.0
        self._fault_start: Dict[str, float] = {}
        self._frozen_imu_values: Optional[Dict[str, float]] = None

    # ---- fault injection (ground truth) -------------------------------------------------

    def inject(self, fault_name: str) -> None:
        if fault_name not in FAULT_TYPES:
            raise ValueError(f"unknown fault type {fault_name!r}, expected one of {FAULT_TYPES}")
        if fault_name in self._fault_start:
            return
        self._fault_start[fault_name] = self.t
        if fault_name == "sensor_lockup":
            self._frozen_imu_values = self._raw_imu_sample()

    def clear(self, fault_name: str) -> None:
        self._fault_start.pop(fault_name, None)
        if fault_name == "sensor_lockup":
            self._frozen_imu_values = None

    def clear_all(self) -> None:
        self._fault_start.clear()
        self._frozen_imu_values = None

    def active_faults(self) -> List[str]:
        return sorted(self._fault_start)

    # ---- physics -------------------------------------------------

    def _raw_imu_sample(self) -> Dict[str, float]:
        return {
            "accel_x": self.rng.gauss(0, 0.01),
            "accel_y": self.rng.gauss(0, 0.01),
            "accel_z": 1.0 + self.rng.gauss(0, 0.01),
            "gyro_x": self.rng.gauss(0, 0.5),
            "gyro_y": self.rng.gauss(0, 0.5),
            "gyro_z": self.rng.gauss(0, 0.5),
        }

    def step(self, dt: float) -> "tuple[RawSample, GroundTruth]":
        self.t += dt

        imu_responded = "sensor_timeout" not in self._fault_start
        if "sensor_lockup" in self._fault_start:
            imu = dict(self._frozen_imu_values)
        elif imu_responded:
            imu = self._raw_imu_sample()
        else:
            # Not responding: last-known-shape values are irrelevant since
            # FDIR is told imu_responded=False and must not trust them: this
            # mirrors "no ACK", not "obviously wrong data".
            imu = self._raw_imu_sample()

        temp_c = NOMINAL_TEMP_C + self.rng.gauss(0, 0.3)
        if "thermal" in self._fault_start:
            temp_c = THERMAL_INJECTED_C + self.rng.gauss(0, 0.3)

        voltage = NOMINAL_VOLTAGE_V + self.rng.gauss(0, 0.02)
        if "undervoltage" in self._fault_start:
            voltage = UNDERVOLTAGE_INJECTED_V + self.rng.gauss(0, 0.02)
        elif "gradual_drift" in self._fault_start:
            elapsed = self.t - self._fault_start["gradual_drift"]
            progress = min(1.0, elapsed / DRIFT_RAMP_DURATION_S)
            ramp_target = NOMINAL_VOLTAGE_V + progress * (DRIFT_FLOOR_V - NOMINAL_VOLTAGE_V)
            voltage = ramp_target + self.rng.gauss(0, 0.02)

        sample = RawSample(
            temp_c=temp_c,
            accel_x=imu["accel_x"], accel_y=imu["accel_y"], accel_z=imu["accel_z"],
            gyro_x=imu["gyro_x"], gyro_y=imu["gyro_y"], gyro_z=imu["gyro_z"],
            mag_x=25.0 + self.rng.gauss(0, 1.0),
            mag_y=-8.0 + self.rng.gauss(0, 1.0),
            mag_z=40.0 + self.rng.gauss(0, 1.0),
            bus_voltage_v=voltage,
            bus_current_a=NOMINAL_CURRENT_A + self.rng.gauss(0, 0.02),
            imu_responded=imu_responded,
            temp_responded=True,
        )
        truth = GroundTruth(t=self.t, active_faults=self.active_faults())
        return sample, truth
