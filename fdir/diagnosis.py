"""
Deterministic diagnosis: symptom -> candidate cause, or an explicit "unknown".

WHY DETERMINISTIC AND NOT LEARNED. The failure research (case study section 19)
looked at whether an ML diagnosis layer is justified and concluded: not on this
evidence. Only one of eight studied cases (Delfi-C3) presented a genuine
diagnostic ambiguity; the rest were failures of authority, isolation,
verification or action repertoire. Building a learned layer to address the
rarest observed bottleneck is poor allocation. A small auditable fault tree over
already-computed detector outputs is inspectable, needs no training data, runs
in microseconds, and directly encodes R6. This is that tree.

WHY "UNKNOWN" IS A FIRST-CLASS ANSWER. 63% of the NASA failure record has no
identifiable technical cause. A diagnosis layer that always produces a label
would be producing fiction most of the time. R10 requires the spacecraft to be
able to say it does not know and to act conservatively when it does -- and the
Delfi-C3 lesson generalises: a confident wrong diagnosis is worse than no
diagnosis, because it authorises the wrong action.

ORDERING MATTERS. Rules are evaluated most-explanatory first. DATA_PATH_SUSPECT
comes first precisely because it EXPLAINS AWAY the per-device symptoms sitting
underneath it -- if the shared bus is suspect, "the IMU is frozen" is not an
independent finding, it is the same finding counted twice.
"""

import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import FaultFlag, Rail, RawSample  # noqa: E402

from . import config as cfg  # noqa: E402


class Cause(IntEnum):
    """Enumerated causes. Deliberately coarse -- these authorise actions, and a
    finer taxonomy than the evidence supports would be false precision."""

    UNKNOWN = 0
    DATA_PATH = 1
    RADIO_LATCHUP = 2
    GROUND_LINK_LOST = 3
    POWER_UNDERVOLTAGE = 4
    THERMAL = 5
    SENSOR_FROZEN = 6
    SENSOR_NOT_RESPONDING = 7
    RECOVERY_EXHAUSTED = 8
    RAIL_OVERCURRENT = 9      # a rail is drawing above its ceiling (FDIR-011)
    SENSOR_CORRUPT = 10       # one device, its bus fine, returning nonsense
    DEGRADATION = 11          # drifted from the commissioning reference (R7)


class Confidence(IntEnum):
    NONE = 0        # no diagnosis at all
    POSSIBLE = 1    # consistent with the evidence, but so are other things
    LIKELY = 2      # the best explanation of the evidence available


@dataclass
class Diagnosis:
    cause: Cause
    confidence: Confidence
    evidence: str

    @property
    def is_known(self) -> bool:
        return self.cause != Cause.UNKNOWN

    @property
    def authorises_action(self) -> bool:
        """
        Only a LIKELY diagnosis of a known cause may justify a recovery action.

        POSSIBLE is deliberately not enough. An action taken on a merely
        plausible diagnosis is the Delfi-C3 failure -- protective responses
        fired against subsystems that were themselves fine.
        """
        return self.is_known and self.confidence >= Confidence.LIKELY


# Flags that indicate "something is wrong" without, on their own, saying what.
_ADVISORY_ONLY = FaultFlag.ADAPTIVE_ANOMALY | FaultFlag.ML_ANOMALY


def diagnose(fault_flags: FaultFlag, sample: Optional[RawSample] = None) -> Diagnosis:
    """
    Map the current latched flags (plus, where it discriminates, the raw
    sample) onto a cause. Pure function: no state, no I/O, no side effects.
    """
    # 1. A suspect shared path explains every per-device symptom under it.
    if fault_flags & FaultFlag.DATA_PATH_SUSPECT:
        return Diagnosis(
            Cause.DATA_PATH, Confidence.LIKELY,
            "two or more devices on one bus invalid together; the shared path "
            "is a better explanation than simultaneous independent failures",
        )

    # 2. A rail over its ceiling, checked BEFORE the comms rule.
    #
    #    It used to sit below, and that masked real faults: the COMMS_LOSS rule
    #    returns on every path and inspects only the RADIO rail, so a live
    #    overcurrent on ADCS, SENSORS or PAYLOAD went undiagnosed whenever
    #    ground contact happened to be stale -- while the evidence string
    #    actively exonerated the power system. Moving this up costs nothing,
    #    because the radio branch below reaches RADIO_LATCHUP for the one case
    #    the old ordering was protecting.
    #
    #    Ordering against UNDERVOLTAGE_CRITICAL is the KySat-2 lesson: the drain
    #    and the sag are one fault, and diagnosing the sag treats a symptom
    #    while the cause keeps eating the battery.
    if fault_flags & FaultFlag.RAIL_OVERCURRENT:
        over = []
        if sample is not None and sample.rail_current_a:
            over = [(r, a) for r, a in sorted(sample.rail_current_a.items())
                    if a > cfg.RAIL_NOMINAL_CURRENT_CEILING_A]
        # REQUIRE LIVE EVIDENCE. RAIL_OVERCURRENT latches, so the bit outlives
        # the condition; this rule used to fire on the stale bit alone and
        # assert "any voltage sag is downstream of it" with every rail sitting
        # at nominal -- masking a live UNDERVOLTAGE_CRITICAL and stating the
        # exact inverse of the truth. That is the F1 defect class: a latched
        # flag read as live state. If nothing is over its ceiling now, this
        # rule has nothing to say and the acute rules below get their turn.
        if over:
            radio = [(r, a) for r, a in over if r == int(Rail.RADIO)]
            if radio:
                return Diagnosis(
                    Cause.RADIO_LATCHUP, Confidence.LIKELY,
                    f"the radio rail is drawing {radio[0][1]:.3f} A, well above "
                    f"its nominal band -- a latch-up, caught on current before "
                    f"the comms symptom has finished debouncing",
                )
            detail = ", ".join(f"{Rail(r).name} at {a:.3f} A" for r, a in over)
            return Diagnosis(
                Cause.RAIL_OVERCURRENT, Confidence.LIKELY,
                f"{detail} against a {cfg.RAIL_NOMINAL_CURRENT_CEILING_A} A "
                f"nominal ceiling; the draw is the cause, any voltage sag is "
                f"downstream of it",
            )

    # 3. Comms loss: latch-up and a merely-quiet link look identical on the
    #    link itself. Per-rail current is what separates them -- which is the
    #    measurement the hardware shortlist exists to justify.
    if fault_flags & FaultFlag.COMMS_LOSS:
        rail_current = None
        if sample is not None and sample.rail_current_a:
            rail_current = sample.rail_current_a.get(int(Rail.RADIO))
        if rail_current is not None and rail_current > cfg.RAIL_NOMINAL_CURRENT_CEILING_A:
            return Diagnosis(
                Cause.RADIO_LATCHUP, Confidence.LIKELY,
                f"no ground contact and the radio rail is drawing "
                f"{rail_current:.3f} A, well above its nominal band",
            )
        if rail_current is None:
            # Without per-rail current we genuinely cannot tell these apart.
            # Saying so is the honest answer, and it is also the argument for
            # the sensing hardware.
            return Diagnosis(
                Cause.GROUND_LINK_LOST, Confidence.POSSIBLE,
                "no ground contact; without per-rail current a radio fault "
                "cannot be distinguished from a quiet link",
            )
        return Diagnosis(
            Cause.GROUND_LINK_LOST, Confidence.LIKELY,
            f"no ground contact and the radio rail is nominal "
            f"({rail_current:.3f} A), so the radio is probably not the fault",
        )

    if fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
        return Diagnosis(Cause.POWER_UNDERVOLTAGE, Confidence.LIKELY,
                         "bus voltage below the critical threshold past its debounce")

    if fault_flags & FaultFlag.THERMAL_ANOMALY:
        return Diagnosis(Cause.THERMAL, Confidence.LIKELY,
                         "temperature outside the critical band past its debounce")

    if fault_flags & FaultFlag.SENSOR_LOCKUP:
        return Diagnosis(Cause.SENSOR_FROZEN, Confidence.LIKELY,
                         "IMU reading identical across the lockup window while still ACKing")

    if fault_flags & FaultFlag.SENSOR_TIMEOUT:
        return Diagnosis(Cause.SENSOR_NOT_RESPONDING, Confidence.LIKELY,
                         "sensor produced no fresh reading past its debounce")

    # The single-device partner to rule 1. Reached only when DATA_PATH_SUSPECT
    # is absent, which is what makes it a claim about the DEVICE: the bus it
    # sits on is carrying its neighbours' traffic perfectly well.
    if fault_flags & FaultFlag.SENSOR_IMPLAUSIBLE:
        return Diagnosis(
            Cause.SENSOR_CORRUPT, Confidence.LIKELY,
            "one device is returning physically impossible values while the bus "
            "it shares is healthy, so the device is the better explanation",
        )

    # R7. Ranked below the acute faults and above UNKNOWN: a drift is real,
    # deterministic and named, but it is a degradation rather than an emergency,
    # and anything acute happening at the same time is the better explanation.
    if fault_flags & FaultFlag.DRIFT_FROM_REFERENCE:
        return Diagnosis(
            Cause.DEGRADATION, Confidence.LIKELY,
            "a channel has moved away from its commissioning reference by more "
            "than the allowed band; slow enough that an adaptive baseline would "
            "have absorbed it as the new normal",
        )

    if fault_flags & FaultFlag.RECOVERY_FAILED:
        return Diagnosis(Cause.RECOVERY_EXHAUSTED, Confidence.LIKELY,
                         "every rung of the recovery ladder ran without verification succeeding")

    # 3. Something is flagged, but only by a detector that cannot say what.
    #    This is the honest UNKNOWN, and it is the common case in the real
    #    failure record rather than an edge case.
    if fault_flags & _ADVISORY_ONLY:
        return Diagnosis(
            Cause.UNKNOWN, Confidence.NONE,
            "a statistical or learned detector flagged the behaviour as unusual, "
            "but no deterministic rule identifies a cause",
        )

    return Diagnosis(Cause.UNKNOWN, Confidence.NONE, "no fault indication")
