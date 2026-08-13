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

import math
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Imports the shared ICD vocabulary, NOT simulator/protocol.py. This module used
# to path-hack into simulator/ for these enums, which meant the supposedly
# hardware-agnostic FDIR package could not be imported without the simulator
# present, formed an import cycle with simulator/environment.py, and produced two
# distinct FaultFlag classes at runtime. See icd/__init__.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import (  # noqa: E402,F401
    BUS_MEMBERS, Bus, Device, FaultFlag, HealthFlag, Mode, Rail, RawSample,
)

from . import config as cfg
from .diagnosis import Cause, Confidence, diagnose
from .ports import RecoveryAction, RecoveryIntent
from .degraded import LADDER, CapabilitySet, rails_to_shed, select_level, set_for_level
from .recovery import Campaign, CampaignState, VerifyCondition, comms_loss_ladder

# Faults allowed to autonomously force NOMINAL/TEST -> SAFE. Deliberately
# excludes ADAPTIVE_ANOMALY (a *statistical* detector, kept advisory-only since
# V0 -- preserved here, not revisited without new evidence) and ML_ANOMALY (a
# *learned* detector -- see module docstring). Only faults grounded in a fixed,
# physically-meaningful threshold get autonomous authority.
SAFE_MODE_TRIGGER_FLAGS = (
    FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY | FaultFlag.SENSOR_LOCKUP
)

# Flags backed by an ongoing physical condition. RESET_FAULTS may clear these
# only on positive evidence that the condition has gone away (see reset_faults).
#
# DATA_PATH_SUSPECT, UNKNOWN_ANOMALY and SENSOR_INVALID were added after the V0
# adversarial safety review (F1, F5). All three were in NEITHER this set nor
# EVENT_FLAGS, which meant reset_faults() never even considered them and nothing
# else cleared them either -- not start_boot(), not watchdog_reset(). They
# latched once and stayed set for the life of the vehicle.
#
# DATA_PATH_SUSPECT was the severe one. diagnose() checks it FIRST by design,
# because a suspect shared path explains away the per-device symptoms under it.
# Stuck permanently, that made every subsequent diagnosis DATA_PATH at
# Confidence.LIKELY -- with authorises_action True -- on a completely healthy
# vehicle, masking real faults underneath. A permanently confident wrong
# diagnosis is the exact Delfi-C3 failure mode fdir/diagnosis.py exists to
# prevent; it was being reintroduced through a latch that could not clear.
#
# The evidence machinery was already correct in every case: _observe() had been
# faithfully counting clean ticks that nothing ever read. This is the one-line
# consequence of wiring it up.
CONDITION_BACKED_FLAGS = (
    FaultFlag.SENSOR_TIMEOUT | FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY
    | FaultFlag.SENSOR_LOCKUP | FaultFlag.ADAPTIVE_ANOMALY | FaultFlag.ML_ANOMALY
    | FaultFlag.DATA_PATH_SUSPECT | FaultFlag.UNKNOWN_ANOMALY | FaultFlag.SENSOR_INVALID
    | FaultFlag.SENSOR_IMPLAUSIBLE | FaultFlag.RAIL_OVERCURRENT
    | FaultFlag.DRIFT_FROM_REFERENCE
)

# Flags recording that something happened, not that something is wrong.
# Acknowledging them always clears them.
#
# RECOVERY_FAILED belongs here rather than above (F5): it records a completed
# episode -- "autonomy tried everything and stood down" -- not a live condition
# that could be re-observed as clear. Left unclearable it survived a LATER
# successful campaign, so a vehicle whose autonomy subsequently worked still
# displayed permanent failure. Clearing it does not restart autonomy: the
# stand-down decision is held in the campaign state machine
# (_update_recovery_proposals reads campaign.finished, never this flag), so
# acknowledging the indication cannot re-arm a ladder that already exhausted.
EVENT_FLAGS = (
    FaultFlag.WATCHDOG_RESET | FaultFlag.CORRUPTED_PACKET | FaultFlag.RECOVERY_FAILED
)

RESETTABLE_FLAGS = CONDITION_BACKED_FLAGS | EVENT_FLAGS

# Flags permitted to AUTHORISE AN AUTONOMOUS RECOVERY ACTION.
#
# This is a separate gate from SAFE_MODE_TRIGGER_FLAGS and must stay separate.
# That one governs a mode transition -- changing a variable. This one governs
# commanding hardware, which is a strictly stronger permission, and the V0
# audit flagged that a mode gate does not generalise to an action gate just
# because it happens to be the only gate that exists today.
#
# ADAPTIVE_ANOMALY and ML_ANOMALY are excluded, as they are from SAFE authority.
# A statistical or learned detector may raise a flag that a human sees; it may
# not cause the spacecraft to switch a rail. That is the project's central
# principle applied to actions rather than only to modes, and it is enforced
# here in code with a test that fails if either bit is added.
# Flags that mean "something is wrong" but which no deterministic rule can
# turn into a cause. If one of these is set and diagnosis returns UNKNOWN, the
# spacecraft says so explicitly rather than inventing a label (R10).
_UNEXPLAINED_FLAGS = FaultFlag.ADAPTIVE_ANOMALY | FaultFlag.ML_ANOMALY

RECOVERY_AUTHORITY_FLAGS = (
    FaultFlag.COMMS_LOSS | FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY
    | FaultFlag.SENSOR_LOCKUP | FaultFlag.SENSOR_TIMEOUT
    # RAIL_OVERCURRENT earns action authority because the correct response is
    # specific, targeted and known: remove power from the offending rail. That
    # is precisely the action KySat-2 needed and never took. It is deliberately
    # NOT in SAFE_MODE_TRIGGER_FLAGS -- an overcurrent on one rail is a
    # subsystem problem with a subsystem-sized answer, and letting the PAYLOAD
    # rail safe the whole vehicle would be the wrong granularity of response.
    | FaultFlag.RAIL_OVERCURRENT
)

# SENSOR_IMPLAUSIBLE appears in NEITHER authority set, on purpose. A channel
# returning impossible values tells us the DATA is untrustworthy; it does not
# tell us the spacecraft is in danger, and it does not identify a cause -- the
# sensor, its wiring and its connector are all consistent with the evidence.
# Acting on it would be inventing a diagnosis, which is the failure R10 exists
# to prevent.


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
        # Counted, not silently swallowed. A detector that has stopped being fed
        # valid data must be distinguishable from one reporting "all clear".
        self.rejected = 0

    def update(self, x: float) -> None:
        # F4: one NaN used to poison mean and var IRREVERSIBLY -- every
        # subsequent arithmetic result is NaN, deviation_sigma() returns NaN,
        # and `NaN > ADAPTIVE_K` is False forever, so the detector silently
        # never fired again while looking exactly like a healthy one. Measured:
        # 500 clean samples after a single NaN did not recover it.
        if not math.isfinite(x):
            self.rejected += 1
            return
        if self.mean is None:
            self.mean = x
            return
        delta = x - self.mean
        self.mean += self.alpha * delta
        self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)

    def deviation_sigma(self, x: float) -> float:
        if self.mean is None or self.var <= 0 or not math.isfinite(x):
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
        self._suspect_now: set = set()
        self._invalid_now: set = set()
        self._implausible_count = 0
        self._overcurrent_since: Optional[float] = None
        # R7. Captured ONCE at commissioning and persisted; see
        # _update_reference_drift for why it must not be recaptured on boot.
        self.voltage_reference: Optional[float] = None
        self._reference_samples: List[float] = []
        self._drift_since: Optional[float] = None
        # R8. Which capability set the vehicle is CONFIRMED to be operating --
        # advanced only once the executor reports the sheds succeeded.
        self.capability: CapabilitySet = LADDER[0]
        self._capability_target: Optional[CapabilitySet] = None
        self._shed_pending: set = set()
        self._degrade_attempts = 0
        # Which rails are currently over their ceiling. Read by diagnose() to
        # name the offender rather than reporting a bare "something is hot".
        self.overcurrent_rails: set = set()
        self._ml_breach_count = 0

        # Consecutive non-breaching observations per fault flag. This is the
        # POSITIVE evidence reset_faults() requires (D2). Deliberately not
        # derived from the debounce timers, because start_boot() clears those --
        # a reset must not be able to manufacture evidence by erasing state.
        self._clean_ticks: Dict[int, int] = {}

        # Recovery requests awaiting an executor. The engine never actuates
        # anything itself -- see fdir/ports.py for why the seam is here.
        self.pending_intents: List[RecoveryIntent] = []
        self._comms_loss_since: Optional[float] = None
        self.campaign: Optional[Campaign] = None
        self.diagnosis = diagnose(FaultFlag.NONE, None)

    # ---- lifecycle -------------------------------------------------

    def start_boot(self, now: float) -> None:
        self.mode = Mode.BOOT
        self._boot_started_at = now
        self._sensor_timeout_since = None
        self._undervoltage_since = None
        self._thermal_since = None
        self._ml_breach_count = 0
        self._implausible_count = 0
        self._overcurrent_since = None
        self._imu_history.clear()
        # A reboot destroys what we knew about every condition. Evidence of
        # clearance must be re-earned by observation, not inherited.
        self._clean_ticks.clear()

    def _observe(self, flag: FaultFlag, breaching: bool) -> None:
        """Record whether a condition is currently breaching, for reset evidence."""
        if breaching:
            self._clean_ticks[int(flag)] = 0
        else:
            self._clean_ticks[int(flag)] = self._clean_ticks.get(int(flag), 0) + 1

    def _has_cleared(self, flag: FaultFlag) -> bool:
        return self._clean_ticks.get(int(flag), 0) >= cfg.RESET_EVIDENCE_SAMPLES

    def _emit(self, now: float, message: str) -> None:
        self.log.append((now, message))

    # ---- main entry point -------------------------------------------------

    def tick(self, sample: RawSample, now: float, ml_advisory: Optional[MLAdvisory] = None) -> None:
        if self._boot_started_at is None:
            self.start_boot(now)

        if self.mode == Mode.BOOT and now - self._boot_started_at >= cfg.BOOT_DURATION_S:
            # Gate on SAFE_MODE_TRIGGER_FLAGS, NOT on bare truthiness (D1).
            # Bare truthiness meant any latched bit safed the vehicle at end of
            # boot -- including WATCHDOG_RESET, which is informational (it records
            # why you booted, not that anything is wrong). A healthy spacecraft
            # that experienced a watchdog reset therefore landed in SAFE with no
            # autonomous way out, which is the CSSWE failure mode: if the reason
            # you reset was a comms problem, the operator command that could free
            # you cannot arrive. It also let advisory-only flags (ADAPTIVE_ANOMALY,
            # ML_ANOMALY) claim SAFE authority here that they are explicitly denied
            # in NOMINAL -- a hole in the architecture's central boundary.
            if self.fault_flags & SAFE_MODE_TRIGGER_FLAGS:
                self.mode = Mode.SAFE
                self._emit(now, f"BOOT -> SAFE: {FaultFlag(self.fault_flags & SAFE_MODE_TRIGGER_FLAGS)!r} at end of boot self-check")
            else:
                self.mode = Mode.NOMINAL
                self._emit(now, "BOOT -> NOMINAL: self-check clean")

        # Validity runs FIRST, ahead of even the data-path discriminator: a
        # channel returning NaN has not produced a reading at all, so nothing
        # downstream should be allowed to draw a conclusion from it -- in
        # either direction. See _update_sample_validity.
        self._update_sample_validity(sample, now)

        # Order matters: the data-path discriminator must run BEFORE the
        # per-channel detectors it gates, or they will have already latched on
        # a reading the path made up.
        self._update_data_path(sample, now)
        # Runs immediately after the path discriminator and depends on it: a
        # device only counts as individually faulty once the shared path has
        # been ruled out as the explanation.
        self._update_plausibility(sample, now)
        self._update_rail_overcurrent(sample, now)
        self._update_sensor_timeout(sample, now)
        self._update_undervoltage(sample, now)
        self._update_thermal(sample, now)
        self._update_lockup(sample, now)
        # AFTER the detectors whose verdicts it consults. Placed earlier, its
        # commissioning guard read fault_flags before _update_undervoltage had
        # written them this tick, so it evaluated the PREVIOUS tick's verdict --
        # and inverted itself exactly: a low sample was admitted because its
        # flag had not been set yet, and the good sample after it was rejected
        # because the flag was still set from the tick before.
        self._update_reference_drift(sample, now)
        self._update_adaptive_baseline(sample, now)
        self._update_ml_advisory(ml_advisory, now)

        if self.fault_flags & SAFE_MODE_TRIGGER_FLAGS and self.mode in (
                Mode.NOMINAL, Mode.TEST, Mode.DEGRADED):
            self.mode = Mode.SAFE
            self._emit(now, f"-> SAFE: {FaultFlag(self.fault_flags & SAFE_MODE_TRIGGER_FLAGS)!r}")

        # R8, AFTER the SAFE decision and never instead of it. Degradation is
        # for preserving a mission that is still viable; if something can
        # command SAFE then the mission is not currently viable and shedding
        # payload is not the right answer.
        self._update_degraded_mode(now)

        # Diagnosis runs AFTER every detector (so it sees the full picture) and
        # BEFORE recovery proposals (so an action can be justified by a cause
        # rather than by a raw flag). It is a pure function of the flags.
        self.diagnosis = diagnose(self.fault_flags, sample)
        if self.diagnosis.cause == Cause.UNKNOWN and self.fault_flags & _UNEXPLAINED_FLAGS:
            if not self.fault_flags & FaultFlag.UNKNOWN_ANOMALY:
                self._emit(now, "UNKNOWN_ANOMALY: something is flagged and no rule "
                                "identifies a cause -- holding, taking no autonomous action")
            self.fault_flags |= FaultFlag.UNKNOWN_ANOMALY
            self._observe(FaultFlag.UNKNOWN_ANOMALY, True)
        else:
            self._observe(FaultFlag.UNKNOWN_ANOMALY, False)

        self._update_recovery_proposals(now, sample)

    # ---- individual detectors -------------------------------------------------
    # Each latches its FaultFlag bit once its debounce window is satisfied, and
    # never clears it on its own -- only RESET_FAULTS (via apply_command) can,
    # and only if the underlying condition has actually cleared. See FDIR-005's
    # reasoning: false positives are handled by not tripping in the first place
    # (debounce), not by being quick to forgive after the fact.

    def _update_sample_validity(self, sample: RawSample, now: float) -> None:
        """
        Is anything in this sample not a representable number? (V0 review F3.)

        Named explicitly rather than folded into the individual detectors
        because a broken channel is its own condition with its own operator
        meaning. The alternative -- letting each detector decide what NaN means
        to it -- is what produced two detectors disagreeing about the same
        reading, one failing open and one failing closed.

        Latches like every other detector, but without a debounce window: a
        single non-finite reading is already conclusive. There is no such thing
        as a transient NaN that might have been a real measurement.
        """
        invalid = sample.invalid_devices()
        breaching = bool(invalid) or not sample.power_valid
        self._invalid_now = invalid
        self._observe(FaultFlag.SENSOR_INVALID, breaching)
        if not breaching:
            return
        if not self.fault_flags & FaultFlag.SENSOR_INVALID:
            channels = ", ".join(sorted(d.name for d in invalid)) or "-"
            self._emit(now, f"SENSOR_INVALID latched (non-finite readings: "
                            f"devices [{channels}]"
                            f"{', power' if not sample.power_valid else ''}) "
                            f"-- these channels are carrying no information")
        self.fault_flags |= FaultFlag.SENSOR_INVALID

    def _update_sensor_timeout(self, sample: RawSample, now: float) -> None:
        self._observe(FaultFlag.SENSOR_TIMEOUT, not sample.imu_responded)
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
        # F3, the most dangerous finding of the V0 safety review. Every
        # comparison with NaN is False, so `NaN < UNDERVOLTAGE_CRITICAL_V` took
        # the else branch below and called _observe(..., False) -- recording a
        # meaningless reading as POSITIVE EVIDENCE that a latched undervoltage
        # had cleared. Measured end to end: latched UNDERVOLTAGE_CRITICAL, then
        # 60 NaN ticks, then RESET_FAULTS cleared it and exit_safe_mode() was
        # accepted. A vehicle correctly held in SAFE was returned to service on
        # readings that carried no information.
        #
        # That defeated the entire point of the D2 fix, which was to require
        # positive evidence rather than absence of evidence.
        #
        # Record NO evidence in either direction, and deliberately do NOT reset
        # the debounce timer: a garbage reading is not grounds to forgive an
        # undervoltage that was already accumulating.
        if not sample.power_valid:
            self.health_flags &= ~HealthFlag.POWER_OK
            return
        self.health_flags |= HealthFlag.POWER_OK
        self._observe(FaultFlag.UNDERVOLTAGE_CRITICAL,
                      sample.bus_voltage_v < cfg.UNDERVOLTAGE_CRITICAL_V)
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
        # A non-finite temperature used to latch THERMAL_ANOMALY -- safely, but
        # by accident: `not (LOW <= NaN <= HIGH)` is `not False`, so this
        # predicate happened to fail closed while _update_undervoltage's
        # happened to fail open. Same language rule, opposite outcomes, decided
        # by nothing more than how each expression was phrased.
        #
        # Latching here would also be a WRONG diagnosis: the truth is "the
        # temperature sensor is broken", not "the spacecraft is too hot", and
        # THERMAL_ANOMALY carries SAFE authority and could authorise a
        # thermal-specific response to a fault that is not thermal. Naming the
        # real condition (SENSOR_INVALID) is the R10-consistent answer.
        if Device.TEMP in sample.invalid_devices():
            self._thermal_since = None
            self.health_flags &= ~HealthFlag.TEMP_OK
            return
        if not self._channel_trusted(Device.TEMP):
            self._thermal_since = None
            self._observe(FaultFlag.THERMAL_ANOMALY, False)
            return
        self.health_flags |= HealthFlag.TEMP_OK
        out_of_band = not (cfg.THERMAL_CRITICAL_LOW_C <= sample.temp_c <= cfg.THERMAL_CRITICAL_HIGH_C)
        self._observe(FaultFlag.THERMAL_ANOMALY, out_of_band)
        if out_of_band:
            if self._thermal_since is None:
                self._thermal_since = now
            elif now - self._thermal_since >= cfg.THERMAL_DEBOUNCE_S:
                if not self.fault_flags & FaultFlag.THERMAL_ANOMALY:
                    self._emit(now, f"THERMAL_ANOMALY latched ({sample.temp_c:.1f} C)")
                self.fault_flags |= FaultFlag.THERMAL_ANOMALY
        else:
            self._thermal_since = None

    def _suspect_devices(self, sample: RawSample) -> set:
        """
        Which devices are returning something that cannot be a real reading?

        Exact-zero across every axis of a multi-axis sensor is the signature
        Delfi-C3 documented ("insertion of zero's in the telemetry data"). Real
        sensor noise makes an exact 0.0 on all three magnetometer axes at once
        essentially impossible, so this is a cheap, ground-truth-free validity
        check rather than a simulation shortcut.
        """
        suspect = set()
        if sample.accel_x == 0.0 and sample.accel_y == 0.0 and sample.accel_z == 0.0 \
                and sample.gyro_x == 0.0 and sample.gyro_y == 0.0 and sample.gyro_z == 0.0:
            suspect.add(Device.IMU)
        if sample.mag_x == 0.0 and sample.mag_y == 0.0 and sample.mag_z == 0.0:
            suspect.add(Device.MAG)
        if sample.temp_c == 0.0:
            suspect.add(Device.TEMP)
        return suspect

    def _update_data_path(self, sample: RawSample, now: float) -> None:
        """
        R6, and the only genuine diagnostic ambiguity the failure research found.

        One symptom -- several channels reading nonsense -- has two very
        different causes: the devices failed, or the path they share did. Two or
        more devices on the SAME bus going invalid in the same sample is far
        better explained by one bus fault than by simultaneous independent
        device failures, so that is what gets latched.

        Shared membership is required, not mere simultaneity: two devices on
        DIFFERENT buses failing together is not evidence of a path fault, and
        treating it as one would trade a false sensor diagnosis for a false bus
        diagnosis.
        """
        if self.mode == Mode.BOOT:
            self._suspect_now = set()
            return
        suspect = self._suspect_devices(sample)
        self._suspect_now = suspect

        path_fault = False
        for bus, members in BUS_MEMBERS.items():
            if len(suspect.intersection(members)) >= 2:
                path_fault = True
                if not self.fault_flags & FaultFlag.DATA_PATH_SUSPECT:
                    self._emit(now, f"DATA_PATH_SUSPECT latched: "
                                    f"{len(suspect.intersection(members))} devices on {bus.name} "
                                    f"invalid together -- the shared path is the better explanation")
                self.fault_flags |= FaultFlag.DATA_PATH_SUSPECT
                break
        self._observe(FaultFlag.DATA_PATH_SUSPECT, path_fault)

    def _update_plausibility(self, sample: RawSample, now: float) -> None:
        """
        ONE device returning impossible values, with its bus healthy.

        The single-device partner to _update_data_path, and the half that was
        missing. `_suspect_devices()` already identified these channels -- it
        has to, in order to count them per bus -- but a lone suspect device
        latched nothing at all, so `sensor_corruption` came out of the scenario
        suite as **undetected** while its two-device twin was caught cleanly.

        The asymmetry was never intentional. Two devices failing together is
        better explained by the bus they share; one device failing alone
        genuinely is a device fault, and saying so is not the Delfi-C3 mistake,
        it is the correct other side of that discrimination.

        Deliberately silent while DATA_PATH_SUSPECT is up: a zeroed bus makes
        every device on it look individually broken, and reporting three device
        faults underneath a path fault would be counting one finding several
        times over -- the same double-count diagnose()'s rule order avoids.
        """
        if self.mode == Mode.BOOT:
            self._implausible_count = 0
            return
        suspect = getattr(self, "_suspect_now", set())
        # Non-finite readings are SENSOR_INVALID's business, not this one.
        suspect = suspect - sample.invalid_devices()

        if self.fault_flags & FaultFlag.DATA_PATH_SUSPECT:
            # Record NO evidence in either direction. Recording non-breach here
            # was the F3/D2 defect in new clothes: while a zeroed bus made every
            # device look broken, this counted each of those ticks as proof the
            # devices were fine, and RESET_FAULTS then cleared SENSOR_IMPLAUSIBLE
            # with the channels still suspect. _update_undervoltage already has
            # the right pattern for this fifteen lines below.
            self._implausible_count = 0
            return

        breaching = bool(suspect)
        self._observe(FaultFlag.SENSOR_IMPLAUSIBLE, breaching)
        if not breaching:
            self._implausible_count = 0
            # This detector OWNS MAG_OK -- no other detector sets it, so if it
            # does not restore the bit nothing ever will. IMU_OK and TEMP_OK
            # belong to the timeout and thermal detectors and are deliberately
            # not touched here; clearing them from this method achieved nothing
            # anyway, since both are unconditionally rewritten later in the
            # same tick.
            self.health_flags |= HealthFlag.MAG_OK
            return
        self._implausible_count += 1
        if self._implausible_count >= cfg.IMPLAUSIBLE_DEBOUNCE_SAMPLES:
            if Device.MAG in suspect:
                # Only after the debounce. Clearing on the first bad sample made
                # a one-tick glitch mark the magnetometer unhealthy permanently,
                # which is precisely what the debounce exists to prevent.
                self.health_flags &= ~HealthFlag.MAG_OK
            if not self.fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE:
                names = ", ".join(sorted(d.name for d in suspect))
                self._emit(now, f"SENSOR_IMPLAUSIBLE latched ({names}) -- device-level "
                                f"fault, its bus is not under suspicion")
            self.fault_flags |= FaultFlag.SENSOR_IMPLAUSIBLE

    def _update_rail_overcurrent(self, sample: RawSample, now: float) -> None:
        """
        FDIR-011. A rail drawing above its ceiling, which is where a latch-up is
        visible LONG before any fixed voltage threshold notices.

        This is the KySat-2 shape: a load ate the battery while
        UNDERVOLTAGE_CRITICAL stayed perfectly happy until it was far too late.
        The `rail_overcurrent` scenario exists to demonstrate exactly that gap
        and, until now, demonstrated it by going **undetected** -- the per-rail
        current was already in RawSample and nothing consumed it.

        No evidence when rail_current_a is absent, which is not an edge case
        but the entire point of the blinded scenario pairs: without per-rail
        sensing this detector cannot exist, and the measured difference between
        the sighted and blinded runs is the argument for buying the hardware.
        """
        if self.mode == Mode.BOOT or not sample.rail_current_a:
            self._overcurrent_since = None
            return
        over = {int(r): a for r, a in sample.rail_current_a.items()
                if a > cfg.RAIL_NOMINAL_CURRENT_CEILING_A}
        self._observe(FaultFlag.RAIL_OVERCURRENT, bool(over))
        self.overcurrent_rails = set(over)
        if not over:
            self._overcurrent_since = None
            return
        if self._overcurrent_since is None:
            self._overcurrent_since = now
        elif now - self._overcurrent_since >= cfg.RAIL_OVERCURRENT_DEBOUNCE_S:
            if not self.fault_flags & FaultFlag.RAIL_OVERCURRENT:
                detail = ", ".join(f"{Rail(r).name}={a:.3f} A" for r, a in sorted(over.items()))
                self._emit(now, f"RAIL_OVERCURRENT latched ({detail}; ceiling "
                                f"{cfg.RAIL_NOMINAL_CURRENT_CEILING_A} A)")
            self.fault_flags |= FaultFlag.RAIL_OVERCURRENT

    def _update_reference_drift(self, sample: RawSample, now: float) -> None:
        """
        R7. Drift measured against a FIXED reference, not a learned one.

        The adaptive baseline (FDIR-006) cannot do this and never could: it
        follows the signal, so a slow enough change is absorbed as the new
        normal and the detector reports healthy the whole way down. That is the
        QuakeSat shape, and this project measured the same blind spot in its own
        ML evaluation -- 0% recall on gradual_drift.

        A fixed reference cannot be talked into moving. The cost is that it has
        to be EARNED once, at commissioning, and then PERSISTED: recapturing on
        every boot would let a reboot part-way through a drift adopt the drifted
        value as normal, which is D2's defect wearing a different hat. So the
        reference is exported to NVM alongside campaign state and restored on
        boot, and start_boot() deliberately does not clear it.
        """
        if self.mode == Mode.BOOT or not sample.power_valid:
            return
        if self.voltage_reference is None:
            # Commissioning. Only clean samples contribute -- a reference
            # captured while something is already wrong is worse than none.
            # Judged from THIS sample, not from a latched flag. Flags are
            # written by other detectors during the same tick, so reading them
            # here made the guard depend on call order -- which is how it came
            # to admit exactly the samples it was written to exclude.
            if sample.bus_voltage_v < cfg.UNDERVOLTAGE_WARNING_V:
                return
            # ANY ongoing condition, not just the SAFE-commanding ones. A
            # RAIL_OVERCURRENT perturbs bus voltage through I*R without
            # tripping a voltage threshold or commanding SAFE, so the old guard
            # let a reference be captured at 5.0 V while a rail was drawing 30x
            # nominal -- baking the fault into the number every future
            # measurement is compared against. THERMAL was blocked only by
            # accident, because it happens to carry SAFE authority.
            if self.fault_flags & CONDITION_BACKED_FLAGS:
                return
            self._reference_samples.append(sample.bus_voltage_v)
            if len(self._reference_samples) >= cfg.REFERENCE_CAPTURE_SAMPLES:
                self.voltage_reference = sum(self._reference_samples) / len(self._reference_samples)
                self._emit(now, f"commissioning reference captured: bus voltage "
                                f"{self.voltage_reference:.3f} V over "
                                f"{len(self._reference_samples)} samples")
            return

        deviation = abs(sample.bus_voltage_v - self.voltage_reference)
        breaching = deviation > cfg.DRIFT_FROM_REFERENCE_V
        self._observe(FaultFlag.DRIFT_FROM_REFERENCE, breaching)
        if not breaching:
            self._drift_since = None
            return
        if self._drift_since is None:
            self._drift_since = now
        elif now - self._drift_since >= cfg.DRIFT_DEBOUNCE_S:
            if not self.fault_flags & FaultFlag.DRIFT_FROM_REFERENCE:
                self._emit(now, f"DRIFT_FROM_REFERENCE latched: bus voltage "
                                f"{sample.bus_voltage_v:.3f} V is {deviation:.3f} V "
                                f"from the {self.voltage_reference:.3f} V commissioning "
                                f"reference (limit {cfg.DRIFT_FROM_REFERENCE_V} V)")
            self.fault_flags |= FaultFlag.DRIFT_FROM_REFERENCE

    def export_capability_state(self) -> dict:
        """
        Which capability set is in force, for NVM.

        Found by probing a reboot: a fresh engine starts at FULL while the rails
        stay physically shed, so software and hardware disagree about what is
        powered. Exactly the class of defect the R7 reference persistence exists
        to prevent, in a second place.
        """
        return {"schema_version": 1, "level": self.capability.level}

    def import_capability_state(self, state: Optional[dict], now: float) -> None:
        if not state:
            return
        try:
            if state.get("schema_version") != 1:
                raise ValueError(f"unsupported capability schema {state.get('schema_version')!r}")
            level = int(state["level"])
            if not 0 <= level < len(LADDER):
                raise ValueError(f"level {level} outside the ladder")
        except (KeyError, TypeError, ValueError) as exc:
            self._emit(now, f"capability state discarded (unreadable: {exc})")
            return
        self.capability = set_for_level(level)
        # Deliberately does NOT set self.mode. Writing DEGRADED here overwrote
        # BOOT, skipping the boot self-check and every warm-up gate that hangs
        # off it -- COMMS_LOSS latched on the first post-reset link report.
        # _update_degraded_mode puts the vehicle in DEGRADED once boot ends,
        # which is the same answer arrived at without breaking the sequence.
        self._emit(now, f"capability restored after reset: {self.capability.name}")

    def recommission_reference(self, now: float) -> None:
        """
        Operator command: discard the commissioning reference and capture a new one.

        THE ESCAPE HATCH, and it was missing. A reference captured wrongly makes
        DRIFT_FROM_REFERENCE latch on perfectly nominal telemetry, which then
        selects a degraded capability set -- and there was no way back. The flag
        cannot clear, because the condition really is breaching against that bad
        reference; RESET_FAULTS therefore refuses forever, and
        restore_capability() refuses forever with it. The vehicle sheds its
        payload and stays that way for the rest of the mission.

        A latching detector whose reference can be wrong needs a way to correct
        the reference, or it is a trap rather than a detector.
        """
        self.voltage_reference = None
        self._reference_samples = []
        self._drift_since = None
        self.fault_flags &= ~FaultFlag.DRIFT_FROM_REFERENCE
        self._clean_ticks.pop(int(FaultFlag.DRIFT_FROM_REFERENCE), None)
        self._emit(now, "commissioning reference discarded by operator command; "
                        "recapturing from the next clean samples")

    def export_reference_state(self) -> Optional[dict]:
        """Commissioning reference, for NVM. Written once, then never again."""
        if self.voltage_reference is None:
            return None
        return {"schema_version": 1, "voltage_reference": self.voltage_reference}

    def import_reference_state(self, state: Optional[dict], now: float) -> None:
        """
        Restore the commissioning reference across a reset.

        Without this, R7 would be defeated by the very thing it guards against:
        reboot mid-drift, recapture at the drifted value, report healthy.
        """
        if not state:
            return
        try:
            if state.get("schema_version") != 1:
                raise ValueError(f"unsupported reference schema {state.get('schema_version')!r}")
            value = float(state["voltage_reference"])
            if not math.isfinite(value):
                raise ValueError(f"non-finite reference {value!r}")
        except (KeyError, TypeError, ValueError) as exc:
            self._emit(now, f"commissioning reference discarded (unreadable: {exc})")
            return
        self.voltage_reference = value
        self._reference_samples = []
        self._emit(now, f"commissioning reference restored: {value:.3f} V")

    def _update_degraded_mode(self, now: float) -> None:
        """
        R8. Select the most capable configuration the evidence still supports.

        Downgrades autonomously; does NOT upgrade autonomously. That asymmetry
        is deliberate and is the same reasoning as SAFE-mode exit (R9): the
        conditions that forced a downgrade are the ones the vehicle is worst
        placed to judge as resolved, and quietly restoring payload power
        because a flag happened to clear is how a spacecraft oscillates. An
        operator restores capability, via restore_capability().
        """
        if self.mode in (Mode.BOOT, Mode.SAFE):
            return
        if self._shed_pending:
            return              # a downgrade is already in flight
        if (self._capability_target is not None
                and self._degrade_attempts >= cfg.MAX_DEGRADE_ATTEMPTS):
            return              # bounded, like every other autonomous action
        # Mode must follow capability even when no flag currently argues for a
        # downgrade. After a reset the capability is restored from NVM while the
        # flags that caused it are gone, so the vehicle sat at REDUCED and
        # reported NOMINAL -- telling the ground it was fully capable while its
        # payload rail was physically off.
        if self.capability.level > 0 and self.mode == Mode.NOMINAL:
            self.mode = Mode.DEGRADED
            self._emit(now, f"-> DEGRADED: operating at {self.capability.name} "
                            f"(capability restored across a reset)")

        want = select_level(self.fault_flags)
        if want <= self.capability.level:
            return
        target = set_for_level(want)
        shed = rails_to_shed(self.capability, target)
        for rail in shed:
            self.pending_intents.append(RecoveryIntent(
                # POWER_OFF, not POWER_CYCLE. A power cycle always restores
                # power after its dwell -- using it here meant the mode changed,
                # the capability object changed, and the rail came straight back
                # on, so the degradation shed nothing. Measured, not theorised.
                action=RecoveryAction.POWER_OFF, target=Rail(rail),
                reason=f"degrade to {target.name}: shedding {Rail(rail).name}",
                requested_at=now,
            ))
        # Capability is NOT advanced here. The engine proposes; the executor
        # acts; and until the executor reports success the rail is still
        # powered. Committing optimistically made the engine believe PAYLOAD was
        # shed while a refusing port left it on -- software and hardware
        # disagreeing about the physical configuration, which is the same class
        # of defect as the capability that did not survive a reboot.
        #
        # It is also the KySat-2 principle applied consistently: recovery
        # campaigns already refuse to treat an issued command as an achieved
        # outcome. Degradation had been exempt from its own architecture's rule.
        self._capability_target = target
        self._shed_pending = set(shed)
        self._degrade_attempts += 1
        self._emit(now, f"degrading [{self.capability.name} -> {target.name}]: "
                        f"shedding {[Rail(r).name for r in shed]}; "
                        f"loses {target.loses}")

    def restore_capability(self, now: float) -> bool:
        """
        Operator command: return to full capability.

        Refused while the conditions that caused the downgrade are still
        present -- the same evidence discipline exit_safe_mode() applies, for
        the same reason. Returns True if accepted.
        """
        if self.capability.level == 0:
            return True
        if select_level(self.fault_flags) > 0:
            return False
        previous = self.capability
        for rail in rails_to_shed(LADDER[0], previous):
            self.pending_intents.append(RecoveryIntent(
                action=RecoveryAction.POWER_ON, target=Rail(rail),
                reason=f"restore {previous.name} -> FULL: re-powering {Rail(rail).name}",
                requested_at=now,
            ))
        self.capability = LADDER[0]
        if self.mode == Mode.DEGRADED:
            self.mode = Mode.NOMINAL
        self._emit(now, f"capability restored by operator command "
                        f"({previous.name} -> {self.capability.name})")
        return True

    def _channel_trusted(self, device: Device) -> bool:
        """
        Should a per-channel detector act on this device's reading?

        No, if the device is currently suspect AND its bus is under suspicion.
        This is the gate that stops Delfi-C3 from happening here: a zeroed bus
        used to latch SENSOR_LOCKUP -- because five identical zero readings look
        exactly like a frozen sensor -- and SENSOR_LOCKUP carries autonomous
        SAFE authority, so the spacecraft safed itself over a fault that was not
        in the IMU at all.
        """
        if not self.fault_flags & FaultFlag.DATA_PATH_SUSPECT:
            return True
        return device not in getattr(self, "_suspect_now", set())

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
        if Device.IMU in getattr(self, "_invalid_now", set()):
            # NaN comparisons make "is this window all identical?" meaningless
            # (NaN != NaN, but a repeated NaN *object* compares equal inside a
            # tuple, so the answer would depend on object identity). Refuse to
            # answer rather than answer by accident.
            self._imu_history.clear()
            self.health_flags &= ~HealthFlag.IMU_OK
            return
        if not self._channel_trusted(Device.IMU):
            # The path carrying this reading is under suspicion, so the reading
            # is not evidence about the device. Clearing the window prevents a
            # run of path-generated zeros from ever satisfying the detector.
            self._imu_history.clear()
            self._observe(FaultFlag.SENSOR_LOCKUP, False)
            return
        if sample.imu_responded:
            fingerprint = (sample.accel_x, sample.accel_y, sample.accel_z,
                           sample.gyro_x, sample.gyro_y, sample.gyro_z)
            self._imu_history.append(fingerprint)
            frozen = (len(self._imu_history) == cfg.LOCKUP_WINDOW_SAMPLES
                      and len(set(self._imu_history)) == 1)
            self._observe(FaultFlag.SENSOR_LOCKUP, frozen)
            if frozen:
                if not self.fault_flags & FaultFlag.SENSOR_LOCKUP:
                    self._emit(now, "SENSOR_LOCKUP latched (IMU reading frozen)")
                self.fault_flags |= FaultFlag.SENSOR_LOCKUP
        else:
            # Not responding is a different fault (SENSOR_TIMEOUT). We have no
            # evidence either way about freezing, so record none.
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
        breaching = warmed_up and self.voltage_baseline.deviation_sigma(voltage) > cfg.ADAPTIVE_K
        self._observe(FaultFlag.ADAPTIVE_ANOMALY, breaching)
        if breaching:
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
            # No advisory, or an advisory saying "normal", both count as evidence
            # of non-breach -- otherwise a latched ML_ANOMALY could never be
            # cleared once the model stopped reporting.
            if self.mode != Mode.BOOT:
                self._observe(FaultFlag.ML_ANOMALY, False)
            return
        self._observe(FaultFlag.ML_ANOMALY, True)
        self._ml_breach_count += 1
        if self._ml_breach_count >= cfg.ML_ANOMALY_DEBOUNCE_SAMPLES:
            if not self.fault_flags & FaultFlag.ML_ANOMALY:
                self._emit(now, f"ML_ANOMALY latched (score={ml_advisory.score:.3f}, advisory only)")
            self.fault_flags |= FaultFlag.ML_ANOMALY

    # ---- link-layer observations -------------------------------------------
    # The transport reports what it OBSERVED; the engine decides what it MEANS.
    # These exist because run_simulator.py used to reach in and write
    # engine.fault_flags directly for COMMS_LOSS and CORRUPTED_PACKET (D6) --
    # transport code mutating safety state, with the timeout hardcoded at the
    # call site while fdir/config.py's COMMS_LOSS_TIMEOUT_S sat unused. Two of
    # ten fault flags had no detector inside this engine, yet reset_faults()
    # cleared them: the engine was clearing flags it could not observe.

    def note_link_state(self, now: float, link_established: bool,
                        seconds_since_contact: Optional[float]) -> None:
        """
        Report ground-link EVIDENCE. The engine decides what it means.

        Unlike the latching detectors, COMMS_LOSS is a live indicator:
        reconnecting IS the recovery, so there is no operator acknowledgement to
        wait for and it is deliberately absent from RESETTABLE_FLAGS.

        J1/K1 -- WHY THIS TAKES EVIDENCE RATHER THAN A VERDICT.
        This used to accept `connected: bool` and short-circuit on it: if
        connected, COMMS_LOSS was cleared unconditionally and
        seconds_since_contact was never examined at all. The transport supplied
        that boolean as `self.conn is not None` -- a socket OBJECT EXISTING, not
        evidence that anything is on the other end. On a half-open TCP link
        (cable pulled, ground station killed, NAT idle timeout) recv blocks
        forever, the socket is never closed, and the spacecraft went on
        believing it had ground contact indefinitely. Measured: connected=True
        with seconds_since_contact=10000 against a 5 s timeout latched nothing.

        That mattered because COMMS_LOSS is the ONLY flag carrying authority to
        open the comms recovery ladder. R5 exists precisely because the ground
        cannot fix the radio it would have to talk through -- and the silent
        link failure, the case that ladder was built for, was the one case the
        spacecraft could not detect.

        The second half of the defect was that the scenario harness supplied a
        clean `link_healthy` boolean from the physics model, so the suite
        validated a decision path the real transport could not produce. Taking
        raw evidence puts the decision in ONE place that both callers reach,
        which is the only structural fix -- correcting the transport alone would
        have left the harness free to disagree with it again.

        THE DECIDING EVIDENCE IS THE HEARTBEAT, AND ONLY THE HEARTBEAT.
        `link_established` is recorded for diagnosis but deliberately does not
        gate the decision, because it is unreliable in BOTH directions:

          * established=True proves nothing. A half-open socket stays "up"
            indefinitely with nothing behind it -- that is J1.
          * established=False does not yet prove loss either. A TCP reconnect
            or a radio handover briefly drops the transport while contact is
            perfectly healthy, and latching on that would trade J1 for a
            false positive. COMMS_LOSS_TIMEOUT_S is the grace period.

        So: contact is lost exactly when nothing has been heard for longer than
        the timeout. That single rule covers both.
        """
        if self.mode == Mode.BOOT:
            # "No ground station has connected yet" is normal during boot, not a
            # fault. Same class of cold-start false positive as the adaptive
            # baseline and lockup warm-ups.
            return
        stale = (seconds_since_contact is None
                 or seconds_since_contact >= cfg.COMMS_LOSS_TIMEOUT_S)
        if not stale:
            if self.fault_flags & FaultFlag.COMMS_LOSS:
                self._emit(now, "COMMS_LOSS cleared (ground contact restored)")
            self.fault_flags &= ~FaultFlag.COMMS_LOSS
        else:
            if not self.fault_flags & FaultFlag.COMMS_LOSS:
                why = ("no transport link" if not link_established
                       else f"link open but nothing heard for >= {cfg.COMMS_LOSS_TIMEOUT_S} s")
                self._emit(now, f"COMMS_LOSS latched ({why})")
            self.fault_flags |= FaultFlag.COMMS_LOSS

    # ---- recovery proposals -------------------------------------------------

    def _propose(self, now: float, action: RecoveryAction, target: int,
                 authorising_flags: FaultFlag, reason: str) -> bool:
        """
        Queue a recovery request. Returns False if the proposal is refused.

        The authority check is the point of this method existing: a caller must
        name which latched flags justify the action, and at least one of them
        must carry recovery authority. A proposal justified only by an advisory
        detector (ADAPTIVE_ANOMALY, ML_ANOMALY) is refused here, in one place,
        rather than relying on every future producer to remember the rule.
        """
        if not (authorising_flags & RECOVERY_AUTHORITY_FLAGS):
            self._emit(now, f"recovery proposal REFUSED ({reason}): "
                            f"{FaultFlag(authorising_flags)!r} carries no recovery authority")
            return False
        self.pending_intents.append(
            RecoveryIntent(action=action, target=target, reason=reason, requested_at=now)
        )
        self._emit(now, f"recovery proposed: {action.name} on device {target} ({reason})")
        return True

    def _verification_met(self, condition: VerifyCondition, sample: RawSample) -> bool:
        """
        Did the action achieve anything? Answered from telemetry only.

        Note what is NOT consulted: whether the executor said the command
        succeeded. "The port accepted it" is not evidence the fault cleared --
        that conflation is the KySat-2 failure exactly.
        """
        if condition == VerifyCondition.RADIO_RESPONSIVE:
            return bool(sample.radio_responded)
        if condition == VerifyCondition.IMU_RESPONSIVE:
            return bool(sample.imu_responded)
        if condition == VerifyCondition.RAIL_CURRENT_NOMINAL:
            rung = self.campaign.current_rung if self.campaign else None
            if rung is None or not sample.rail_current_a:
                return False
            draw = sample.rail_current_a.get(int(rung.target))
            return draw is not None and draw < cfg.RAIL_NOMINAL_CURRENT_CEILING_A
        return False

    def _start_campaign(self, now: float, trigger: FaultFlag, rungs, reason: str) -> bool:
        if not (trigger & RECOVERY_AUTHORITY_FLAGS):
            self._emit(now, f"campaign REFUSED ({reason}): "
                            f"{FaultFlag(trigger)!r} carries no recovery authority")
            return False
        self.campaign = Campaign(trigger=int(trigger), rungs=list(rungs), started_at=now)
        self._emit(now, f"recovery campaign opened ({reason}), {len(rungs)} rungs")
        return True

    def _advance_campaign(self, now: float, sample: RawSample) -> None:
        """
        The bounded/verified/escalating loop. One transition per tick.

        IDLE     -> issue the current rung's intent, go ACTING
        ACTING   -> (executor reports completion) go VERIFYING with a deadline
        VERIFYING-> condition met      -> SUCCEEDED
                    deadline expired   -> count the attempt; retry the rung if
                                          it has attempts left, else escalate;
                                          if no rungs remain -> EXHAUSTED
        """
        c = self.campaign
        if c is None or c.finished:
            return

        if c.state == CampaignState.IDLE:
            rung = c.current_rung
            if rung is None:
                self._exhaust_campaign(now)
                return
            self.pending_intents.append(RecoveryIntent(
                action=rung.action, target=rung.target,
                reason=f"rung {c.rung_index}: {rung.description}",
                requested_at=now, attempt=c.attempts_on_rung + 1,
            ))
            c.state = CampaignState.ACTING
            self._emit(now, f"recovery rung {c.rung_index} attempt "
                            f"{c.attempts_on_rung + 1}/{rung.max_attempts}: {rung.description}")
            return

        if c.state == CampaignState.VERIFYING:
            rung = c.current_rung
            if rung is not None and self._verification_met(rung.verify, sample):
                c.state = CampaignState.SUCCEEDED
                self._emit(now, f"recovery VERIFIED at rung {c.rung_index} "
                                f"({rung.verify.name}) after {c.total_attempts} attempt(s)")
                return
            if c.verify_deadline is not None and now >= c.verify_deadline:
                self._on_verification_failed(now)

    def _on_verification_failed(self, now: float) -> None:
        c = self.campaign
        rung = c.current_rung
        self._emit(now, f"recovery rung {c.rung_index} attempt {c.attempts_on_rung} "
                        f"NOT VERIFIED ({rung.verify.name if rung else '?'})")
        if rung is not None and c.attempts_on_rung < rung.max_attempts:
            c.state = CampaignState.IDLE          # retry the same rung, still bounded
            return
        c.rung_index += 1
        c.attempts_on_rung = 0
        if c.current_rung is None:
            self._exhaust_campaign(now)
        else:
            c.state = CampaignState.IDLE
            self._emit(now, f"escalating to rung {c.rung_index}: {c.current_rung.description}")

    def _exhaust_campaign(self, now: float) -> None:
        c = self.campaign
        c.state = CampaignState.EXHAUSTED
        self.fault_flags |= FaultFlag.RECOVERY_FAILED
        self._emit(now, f"RECOVERY_FAILED: every rung exhausted after "
                        f"{c.total_attempts} attempt(s); autonomy standing down")

    def note_shed_completed(self, now: float, rail: int, accepted: bool) -> None:
        """
        The executor reporting whether a load-shed command was accepted.

        Capability advances here and nowhere else. A refused shed means the rail
        is still powered, so claiming the degraded configuration would be
        claiming a physical state that does not exist.
        """
        if not self._shed_pending or self._capability_target is None:
            return
        if not accepted:
            self._shed_pending.clear()
            self._emit(now, f"degrade to {self._capability_target.name} FAILED: "
                            f"port refused to shed {Rail(rail).name}; capability "
                            f"remains {self.capability.name} "
                            f"(attempt {self._degrade_attempts}/{cfg.MAX_DEGRADE_ATTEMPTS})")
            if self._degrade_attempts >= cfg.MAX_DEGRADE_ATTEMPTS:
                self._emit(now, "degradation standing down; the ground is better "
                                "placed to decide what to do with a rail that "
                                "will not switch")
            return
        self._shed_pending.discard(int(rail))
        if self._shed_pending:
            return
        previous = self.capability
        self.capability = self._capability_target
        self._capability_target = None
        self._degrade_attempts = 0
        if self.mode == Mode.NOMINAL:
            self.mode = Mode.DEGRADED
        self._emit(now, f"-> DEGRADED [{previous.name} -> {self.capability.name}]: "
                        f"sheds confirmed by the executor")

    def note_action_completed(self, now: float, accepted: bool) -> None:
        """
        Called by the executor when an action finishes. Starts the observation
        window; it does NOT decide success. Whether the fault actually cleared
        is re-observed from telemetry during VERIFYING.
        """
        c = self.campaign
        if c is None or c.state != CampaignState.ACTING:
            return
        c.attempts_on_rung += 1
        c.total_attempts += 1
        c.state = CampaignState.VERIFYING
        c.verify_deadline = now + cfg.RECOVERY_VERIFY_WINDOW_S
        self._emit(now, f"action complete (port accepted={accepted}); "
                        f"verifying for {cfg.RECOVERY_VERIFY_WINDOW_S} s")

    def _update_recovery_proposals(self, now: float, sample: RawSample) -> None:
        """
        The CSSWE rule: loss of ground contact is a fault condition with an
        autonomous response, because the one thing the ground cannot fix is the
        radio it would have to talk through.

        The trigger is COMMS_RECOVERY_TRIGGER_S, NOT the COMMS_LOSS_TIMEOUT_S
        heartbeat -- flagging the loss and acting on it are different
        timescales, and conflating them would power-cycle the radio every five
        seconds.
        """
        if self.campaign is not None and not self.campaign.finished:
            self._advance_campaign(now, sample)
            return

        if not self.fault_flags & FaultFlag.COMMS_LOSS:
            self._comms_loss_since = None
            # The finished campaign is deliberately KEPT rather than nulled.
            # Discarding it on recovery would erase the only structured record
            # that a recovery happened and was verified -- the log survives, but
            # the outcome, rung reached and attempt count would not. A new loss
            # episode is distinguished by its start time below, not by wiping it.
            return

        if self.campaign is not None and self.campaign.finished:
            started_this_episode = (
                self._comms_loss_since is not None
                and self.campaign.started_at >= self._comms_loss_since
            )
            if started_this_episode:
                # Already tried everything for THIS episode; stand down. Trying
                # again on the same unbroken loss would be the blind repetition
                # R3 forbids.
                return
            # Otherwise the campaign belongs to an earlier, resolved episode and
            # a genuinely new one may open below.

        if self._comms_loss_since is None:
            self._comms_loss_since = now
            return
        if now - self._comms_loss_since >= cfg.COMMS_RECOVERY_TRIGGER_S:
            # No arguments: the ladder now names the radio DEVICE and the radio
            # RAIL separately and defaults both correctly. Passing one id for
            # both was the F2 defect.
            self._start_campaign(
                now, FaultFlag.COMMS_LOSS, comms_loss_ladder(),
                f"no ground contact for >= {cfg.COMMS_RECOVERY_TRIGGER_S} s",
            )

    # ---- campaign persistence (state only -- the engine performs no I/O) ----

    def export_recovery_state(self) -> Optional[dict]:
        """Snapshot for non-volatile storage. Written BEFORE an action executes."""
        return None if self.campaign is None else self.campaign.to_dict()

    def import_recovery_state(self, state: Optional[dict], now: float) -> None:
        """
        Restore after a reset. This is what stops KySat-2's loop: without it a
        reboot mid-campaign restarts at rung 0 with the attempt counter at zero,
        forever.

        A campaign restored mid-action resumes at the NEXT rung, not the one it
        was on -- we cannot know whether the interrupted action completed, and
        re-running it would be the blind repetition R3 forbids.
        """
        if not state:
            return
        try:
            campaign = Campaign.from_dict(state)
        except (KeyError, TypeError, ValueError) as exc:
            # TypeError added with F6's range checks: comparing a string
            # attempt-count against 0 raises TypeError, not ValueError, and an
            # uncaught exception here would crash the flight software on boot
            # over a corrupted NVM record -- turning a recoverable data fault
            # into a boot loop.
            self._emit(now, f"recovery state discarded (unreadable: {exc})")
            return
        if campaign.finished:
            self.campaign = campaign
            self._emit(now, "restored a finished recovery campaign; not resuming")
            return
        campaign.rung_index += 1
        campaign.attempts_on_rung = 0
        campaign.verify_deadline = None
        if campaign.current_rung is None:
            self.campaign = campaign
            self._exhaust_campaign(now)
            return
        campaign.state = CampaignState.IDLE
        self.campaign = campaign
        self._emit(now, f"recovery campaign resumed after reset at rung "
                        f"{campaign.rung_index} ({campaign.current_rung.description}); "
                        f"{campaign.total_attempts} prior attempt(s) remembered")

    def take_pending_intents(self) -> List[RecoveryIntent]:
        """Drain the queue. The executor calls this; nothing else should."""
        intents, self.pending_intents = self.pending_intents, []
        return intents

    def note_corrupted_packet(self, now: float) -> None:
        """Report that the transport rejected a packet on integrity grounds (COM-004)."""
        if not self.fault_flags & FaultFlag.CORRUPTED_PACKET:
            self._emit(now, "CORRUPTED_PACKET latched (failed integrity check on receive)")
        self.fault_flags |= FaultFlag.CORRUPTED_PACKET

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
        # Land in DEGRADED rather than NOMINAL if capability is still shed --
        # leaving SAFE does not silently restore payload power.
        self.mode = Mode.DEGRADED if self.capability.level > 0 else Mode.NOMINAL
        self._emit(now, f"SAFE -> {self.mode.name} by operator command "
                        f"(triggering fault cleared)")
        return True

    def reset_faults(self, now: float) -> Tuple[FaultFlag, FaultFlag]:
        """
        Clears each latched condition-backed flag only on POSITIVE evidence that
        the condition has gone away: RESET_EVIDENCE_SAMPLES consecutive
        non-breaching observations, counted by _observe().

        Returns (cleared, still_latched) so the caller can report the real
        outcome instead of assuming success (D4). Event flags
        (WATCHDOG_RESET, CORRUPTED_PACKET) record something that already
        happened rather than an ongoing condition, so acknowledging them always
        clears them.

        This deliberately requires evidence rather than absence of evidence.
        The previous implementation inferred "cleared" from a debounce timer
        being None, which start_boot() also does -- so a reboot manufactured
        the evidence and a still-faulted vehicle could be returned to service.
        """
        before = self.fault_flags
        for flag in CONDITION_BACKED_FLAGS:
            if self.fault_flags & flag and self._has_cleared(flag):
                self.fault_flags &= ~flag
        self.fault_flags &= ~EVENT_FLAGS

        cleared = FaultFlag(before & ~self.fault_flags)
        still_latched = FaultFlag(self.fault_flags & RESETTABLE_FLAGS)
        if cleared:
            self._emit(now, f"RESET_FAULTS cleared {cleared!r}")
        if still_latched:
            self._emit(now, f"RESET_FAULTS refused {still_latched!r}: condition not confirmed clear")
        if not cleared and not still_latched:
            self._emit(now, "RESET_FAULTS applied (nothing latched)")
        return cleared, still_latched

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
