"""
Deterministic Fault Detection, Isolation, and Recovery engine.

This module is the authority. It is a (near-)pure function of
(telemetry sample, elapsed time, optional ML advisory) -> (mode, fault_flags,
health_flags) plus a log of what changed and why. No sockets, no threads, and
critically, no simulation-only "ground truth" shortcuts: everything here reads
only what real STM32 firmware could actually observe -- raw sensor values,
whether a sensor produced a fresh reading this tick, and (once trained) a score
from the onboard anomaly-detection model. It never reads "is a fault currently
injected" -- that would make the detector untestable against ground truth,
since it would just be echoing the test harness's own answer key back.

Extracted from simulator/run_simulator.py's original Simulator class, which
coupled this decision logic directly to TCP/threading concerns and to the
simulator's own injection flags. That coupling was a reasonable simplification
for a single-script V0 demo; it stopped being reasonable once this logic needed
to (a) be evaluated against an ML pipeline on the same footing, and (b) port to
firmware later without dragging socket code with it. See
docs/architecture/phase0-1-engineering-decisions.md for the full reasoning.

Architectural principle, enforced in code, not just described here: ML output
can only ever set FaultFlag.ML_ANOMALY through the same debounced latch every
deterministic detector uses, and ML_ANOMALY is deliberately excluded from
SAFE_MODE_TRIGGER_FLAGS below. The model can flag something as worth attention;
it can never, by itself, force a state transition. That boundary is the one
line of code two paragraphs down, not a policy note someone could forget to
enforce elsewhere.
"""

import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))
from protocol import FaultFlag, HealthFlag, Mode  # noqa: E402

from . import config as cfg

# Faults allowed to autonomously force NOMINAL/TEST -> SAFE. Deliberately
# excludes ADAPTIVE_ANOMALY (a *statistical* detector, kept advisory-only since
# V0 -- preserved here, not revisited without new evidence) and ML_ANOMALY (a
# *learned* detector -- see module docstring). Only faults grounded in a fixed,
# physically-meaningful threshold get autonomous authority.
SAFE_MODE_TRIGGER_FLAGS = (
    FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY | FaultFlag.SENSOR_LOCKUP
)


@dataclass
class RawSample:
    """
    What FDIR is allowed to see. Physical observables only.

    `imu_responded`/`temp_responded` model whether the sensor actually produced
    a fresh reading this tick -- a real, physically observable I2C/SPI outcome
    (an ACK or the lack of one), not a simulation ground-truth label.
    """

    temp_c: float
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    mag_x: float
    mag_y: float
    mag_z: float
    bus_voltage_v: float
    bus_current_a: float
    imu_responded: bool = True
    temp_responded: bool = True


@dataclass
class MLAdvisory:
    """What the anomaly-detection model contributes each tick. Advisory only.

    `score` is a raw model output (e.g. an Isolation Forest path-length-based
    score) and is explicitly NOT a probability -- see ml/evaluate.py and
    docs/architecture/phase0-1-engineering-decisions.md for why that distinction
    is being called out this deliberately.
    """

    score: float
    is_anomalous: bool


class EwmaStat:
    """Exponentially-weighted mean/variance -- the adaptive baseline behind FDIR-006."""

    def __init__(self, alpha: float):
        self.alpha = alpha
        self.mean: Optional[float] = None
        self.var = 0.0

    def update(self, x: float) -> None:
        if self.mean is None:
            self.mean = x
            return
        delta = x - self.mean
        self.mean += self.alpha * delta
        self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)

    def deviation_sigma(self, x: float) -> float:
        if self.mean is None or self.var <= 0:
            return 0.0
        return abs(x - self.mean) / (self.var ** 0.5)


class FDIREngine:
    def __init__(self):
        self.mode = Mode.BOOT
        self.fault_flags = FaultFlag.NONE
        self.health_flags = HealthFlag.ALL_OK
        self.log: List[Tuple[float, str]] = []

        self._boot_started_at: Optional[float] = None
        self._sensor_timeout_since: Optional[float] = None
        self._undervoltage_since: Optional[float] = None
        self._thermal_since: Optional[float] = None

        self._adaptive_breach_count = 0
        self._adaptive_sample_count = 0
        self.voltage_baseline = EwmaStat(cfg.EWMA_ALPHA)

        self._imu_history: deque = deque(maxlen=cfg.LOCKUP_WINDOW_SAMPLES)
        self._ml_breach_count = 0

    # ---- lifecycle -------------------------------------------------

    def start_boot(self, now: float) -> None:
        self.mode = Mode.BOOT
        self._boot_started_at = now
        self._sensor_timeout_since = None
        self._undervoltage_since = None
        self._thermal_since = None
        self._ml_breach_count = 0
        self._imu_history.clear()

    def _emit(self, now: float, message: str) -> None:
        self.log.append((now, message))

    # ---- main entry point -------------------------------------------------

    def tick(self, sample: RawSample, now: float, ml_advisory: Optional[MLAdvisory] = None) -> None:
        if self._boot_started_at is None:
            self.start_boot(now)

        if self.mode == Mode.BOOT and now - self._boot_started_at >= cfg.BOOT_DURATION_S:
            if self.fault_flags:
                self.mode = Mode.SAFE
                self._emit(now, "BOOT -> SAFE: fault present at end of boot self-check")
            else:
                self.mode = Mode.NOMINAL
                self._emit(now, "BOOT -> NOMINAL: self-check clean")

        self._update_sensor_timeout(sample, now)
        self._update_undervoltage(sample, now)
        self._update_thermal(sample, now)
        self._update_lockup(sample, now)
        self._update_adaptive_baseline(sample, now)
        self._update_ml_advisory(ml_advisory, now)

        if self.fault_flags & SAFE_MODE_TRIGGER_FLAGS and self.mode in (Mode.NOMINAL, Mode.TEST):
            self.mode = Mode.SAFE
            self._emit(now, f"-> SAFE: {FaultFlag(self.fault_flags & SAFE_MODE_TRIGGER_FLAGS)!r}")

    # ---- individual detectors -------------------------------------------------
    # Each latches its FaultFlag bit once its debounce window is satisfied, and
    # never clears it on its own -- only RESET_FAULTS (via apply_command) can,
    # and only if the underlying condition has actually cleared. See FDIR-005's
    # reasoning: false positives are handled by not tripping in the first place
    # (debounce), not by being quick to forgive after the fact.

    def _update_sensor_timeout(self, sample: RawSample, now: float) -> None:
        if not sample.imu_responded:
            self.health_flags &= ~HealthFlag.IMU_OK
            if self._sensor_timeout_since is None:
                self._sensor_timeout_since = now
            elif now - self._sensor_timeout_since >= cfg.SENSOR_TIMEOUT_DEBOUNCE_S:
                if not self.fault_flags & FaultFlag.SENSOR_TIMEOUT:
                    self._emit(now, "SENSOR_TIMEOUT latched (IMU not responding)")
                self.fault_flags |= FaultFlag.SENSOR_TIMEOUT
        else:
            self.health_flags |= HealthFlag.IMU_OK
            self._sensor_timeout_since = None

    def _update_undervoltage(self, sample: RawSample, now: float) -> None:
        if sample.bus_voltage_v < cfg.UNDERVOLTAGE_CRITICAL_V:
            if self._undervoltage_since is None:
                self._undervoltage_since = now
            elif now - self._undervoltage_since >= cfg.UNDERVOLTAGE_DEBOUNCE_S:
                if not self.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
                    self._emit(now, f"UNDERVOLTAGE_CRITICAL latched ({sample.bus_voltage_v:.2f} V)")
                self.fault_flags |= FaultFlag.UNDERVOLTAGE_CRITICAL
        else:
            self._undervoltage_since = None
        if sample.bus_voltage_v < cfg.UNDERVOLTAGE_WARNING_V:
            self.fault_flags |= FaultFlag.UNDERVOLTAGE_WARNING
        else:
            self.fault_flags &= ~FaultFlag.UNDERVOLTAGE_WARNING

    def _update_thermal(self, sample: RawSample, now: float) -> None:
        out_of_band = not (cfg.THERMAL_CRITICAL_LOW_C <= sample.temp_c <= cfg.THERMAL_CRITICAL_HIGH_C)
        if out_of_band:
            if self._thermal_since is None:
                self._thermal_since = now
            elif now - self._thermal_since >= cfg.THERMAL_DEBOUNCE_S:
                if not self.fault_flags & FaultFlag.THERMAL_ANOMALY:
                    self._emit(now, f"THERMAL_ANOMALY latched ({sample.temp_c:.1f} C)")
                self.fault_flags |= FaultFlag.THERMAL_ANOMALY
        else:
            self._thermal_since = None

    def _update_lockup(self, sample: RawSample, now: float) -> None:
        # A frozen IMU still ACKs (imu_responded True) but stops changing.
        # Exact-repeat-for-N-samples is a cheap, ground-truth-free detector --
        # ordinary sensor noise makes a real exact repeat vanishingly unlikely.
        # Like the adaptive baseline, this needs a settled window before it
        # means anything -- gate it on BOOT the same way, rather than trust
        # that real sensor jitter always saves it (that assumption is exactly
        # what caused the adaptive-baseline cold-start bug found in V0).
        if self.mode == Mode.BOOT:
            self._imu_history.clear()
            return
        if sample.imu_responded:
            fingerprint = (sample.accel_x, sample.accel_y, sample.accel_z,
                           sample.gyro_x, sample.gyro_y, sample.gyro_z)
            self._imu_history.append(fingerprint)
            if (len(self._imu_history) == cfg.LOCKUP_WINDOW_SAMPLES
                    and len(set(self._imu_history)) == 1):
                if not self.fault_flags & FaultFlag.SENSOR_LOCKUP:
                    self._emit(now, "SENSOR_LOCKUP latched (IMU reading frozen)")
                self.fault_flags |= FaultFlag.SENSOR_LOCKUP
        else:
            self._imu_history.clear()

    def _update_adaptive_baseline(self, sample: RawSample, now: float) -> None:
        # FDIR isn't fully active until NOMINAL, and a variance estimate built
        # from only a handful of samples is nearly zero, which makes ordinary
        # noise look like a huge number of standard deviations away. Require a
        # warm-up period before the baseline is trusted to judge anything --
        # this is what stops the detector from flagging itself on cold start
        # (a real bug caught and fixed during V0 testing; preserved here).
        if self.mode == Mode.BOOT:
            return
        if self.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
            return  # don't let an active fault get learned as the new normal

        voltage = sample.bus_voltage_v
        warmed_up = self._adaptive_sample_count >= cfg.MIN_ADAPTIVE_SAMPLES
        if warmed_up and self.voltage_baseline.deviation_sigma(voltage) > cfg.ADAPTIVE_K:
            self._adaptive_breach_count += 1
            if self._adaptive_breach_count >= cfg.ADAPTIVE_DEBOUNCE_SAMPLES:
                if not self.fault_flags & FaultFlag.ADAPTIVE_ANOMALY:
                    self._emit(now, f"ADAPTIVE_ANOMALY latched (voltage {voltage:.2f} V deviates from learned baseline)")
                self.fault_flags |= FaultFlag.ADAPTIVE_ANOMALY
        else:
            self._adaptive_breach_count = 0
            self.voltage_baseline.update(voltage)
            self._adaptive_sample_count += 1

    def _update_ml_advisory(self, ml_advisory: Optional[MLAdvisory], now: float) -> None:
        # Same debounce pattern as every deterministic detector -- a single
        # anomalous sample from the model doesn't latch anything. What's
        # different, deliberately, is what latching THIS flag is allowed to do:
        # see SAFE_MODE_TRIGGER_FLAGS at module scope. This method can only ever
        # set FaultFlag.ML_ANOMALY; it cannot reach `self.mode`.
        if ml_advisory is None or self.mode == Mode.BOOT or not ml_advisory.is_anomalous:
            self._ml_breach_count = 0
            return
        self._ml_breach_count += 1
        if self._ml_breach_count >= cfg.ML_ANOMALY_DEBOUNCE_SAMPLES:
            if not self.fault_flags & FaultFlag.ML_ANOMALY:
                self._emit(now, f"ML_ANOMALY latched (score={ml_advisory.score:.3f}, advisory only)")
            self.fault_flags |= FaultFlag.ML_ANOMALY

    # ---- commands (operator-driven FDIR state transitions) -----------------
    # PING / GET_STATUS / SET_TELEMETRY_RATE / REQUEST_LOG don't touch FDIR
    # state and are intentionally NOT here -- they stay in whatever's serving
    # the link (simulator/run_simulator.py), since they're not fault-management
    # decisions.

    def enter_safe_mode(self, now: float) -> None:
        self.mode = Mode.SAFE
        self._emit(now, "SAFE mode entered by operator command")

    def exit_safe_mode(self, now: float) -> bool:
        """Returns True if accepted. False means: a triggering fault is still active (FDIR-005)."""
        if self.mode != Mode.SAFE:
            return True
        if self.fault_flags & SAFE_MODE_TRIGGER_FLAGS:
            return False
        self.mode = Mode.NOMINAL
        self._emit(now, "SAFE -> NOMINAL by operator command (triggering fault cleared)")
        return True

    def reset_faults(self, now: float) -> None:
        """
        Clears each latched flag IF its own debounce/window state shows the
        condition isn't currently breaching -- derived entirely from this
        engine's own tracked state, not from simulation ground truth. This is
        deliberately the same information real firmware would have: whether
        the last several samples have been back inside tolerance.
        """
        if self._sensor_timeout_since is None:
            self.fault_flags &= ~FaultFlag.SENSOR_TIMEOUT
        if self._undervoltage_since is None:
            self.fault_flags &= ~FaultFlag.UNDERVOLTAGE_CRITICAL
        if self._thermal_since is None:
            self.fault_flags &= ~FaultFlag.THERMAL_ANOMALY
        if not (len(self._imu_history) == cfg.LOCKUP_WINDOW_SAMPLES and len(set(self._imu_history)) == 1):
            self.fault_flags &= ~FaultFlag.SENSOR_LOCKUP
        if self._adaptive_breach_count == 0:
            self.fault_flags &= ~FaultFlag.ADAPTIVE_ANOMALY
        if self._ml_breach_count == 0:
            self.fault_flags &= ~FaultFlag.ML_ANOMALY
        self.fault_flags &= ~(FaultFlag.WATCHDOG_RESET | FaultFlag.CORRUPTED_PACKET)
        self._emit(now, "RESET_FAULTS applied")

    def enter_test_mode(self) -> bool:
        if self.mode not in (Mode.NOMINAL, Mode.TEST):
            return False
        self.mode = Mode.TEST
        return True

    def exit_test_mode(self) -> bool:
        if self.mode != Mode.TEST:
            return False
        self.mode = Mode.NOMINAL
        return True

    def watchdog_reset(self, now: float) -> None:
        self.fault_flags |= FaultFlag.WATCHDOG_RESET
        self.start_boot(now)
        self._emit(now, "watchdog reset -> BOOT")
