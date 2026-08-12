"""
Bounded, verified, escalating recovery campaigns.

This module exists because of KySat-2. That spacecraft *did* respond to its
fault -- it reset, hourly, indefinitely -- and each reset re-entered the same
latch-up-and-drain condition until the battery was gone. It had an action, no
verification that the action achieved anything, and no escalation when it
didn't. Phase 3 deliberately reproduced that shape (an executor that records
success on a power cycle which fixed nothing) and pinned it as a test. This is
the phase that fixes it.

Four rules, each traced to the failure research:

  R2  every recovery action carries an explicit verification condition,
      evaluated from telemetry after an observation window -- never assumed
      from "the command was accepted"
  R3  a failed action is not blindly repeated; attempts are bounded per rung
      and exhausting a rung escalates to a different, stronger one
  R4  the ladder does not depend solely on the subsystem being recovered
  --  campaign state persists across a reset, so a reboot mid-campaign resumes
      at the next rung rather than restarting at the first. Erasing the attempt
      counter is precisely how KySat-2's loop became infinite.

DESIGN NOTE -- where this runs. Campaign state lives in FDIREngine because
verification requires observing telemetry, which only the engine does. But the
engine still performs no I/O and holds no ports: it exports/imports its
campaign state as a plain dict, and something outside (the run loop) is
responsible for actually writing that to storage. That keeps the engine a pure
function and keeps persistence testable without a filesystem.
"""

import sys
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import Device, Rail  # noqa: E402

from .ports import RecoveryAction  # noqa: E402

_DEVICE_IDS = {int(d) for d in Device}
_RAIL_IDS = {int(r) for r in Rail}


class VerifyCondition(IntEnum):
    """
    What "it worked" means for a given action, checkable from telemetry alone.

    Deliberately an enum rather than a callable: it has to survive being
    serialised into persistent state and, later, being ported to C.
    """

    NONE = 0
    RADIO_RESPONSIVE = 1        # the radio ACKs again
    RAIL_CURRENT_NOMINAL = 2    # the rail's draw returned to its expected band
    IMU_RESPONSIVE = 3


class CampaignState(IntEnum):
    IDLE = 0
    ACTING = 1        # an intent has been issued, executor is working on it
    VERIFYING = 2     # action complete, observing whether it achieved anything
    SUCCEEDED = 3
    EXHAUSTED = 4     # every rung tried, verification never satisfied


SYSTEM_TARGET = -1      # "the whole spacecraft", the only valid negative target


def _kind_of(target) -> str:
    """Tag a target with which vocabulary it belongs to, for persistence."""
    if target == SYSTEM_TARGET:
        return "system"
    if isinstance(target, Device):
        return "device"
    if isinstance(target, Rail):
        return "rail"
    raise ValueError(f"untyped recovery target {target!r}; expected Device, Rail "
                     f"or SYSTEM_TARGET")


def _target_from(value: int, kind: Optional[str]):
    """Rebuild a typed target from persisted state. Refuses to guess."""
    if kind == "system":
        return SYSTEM_TARGET
    if kind == "device":
        return Device(value)
    if kind == "rail":
        return Rail(value)
    raise ValueError(f"persisted rung target has no usable kind ({kind!r}); "
                     f"a bare id cannot be resolved because Rail and Device "
                     f"ids overlap")


@dataclass
class Rung:
    """
    One step of an escalation ladder.

    `target` is an id whose MEANING DEPENDS ON `action`, which is exactly the
    trap the V0 safety review found (F2). RESET_DEVICE targets are indexed
    against `Device`; POWER_CYCLE targets against `Rail`. The two enums overlap
    numerically -- Rail.RADIO is 1 and Device.MAG is also 1 -- so passing the
    wrong one is silent, type-correct, and wrong.

    That is what happened: comms_loss_ladder() took a single integer and used it
    for both, so "soft reset the radio" issued a reset of Device(1) = MAG. V0
    never noticed because SimulatedResetPort honestly returns False for device
    reset, so the rung reported unavailable and the ladder escalated past it.

    __post_init__ makes the mistake unrepresentable rather than merely fixed.
    """

    action: RecoveryAction
    target: int
    max_attempts: int
    verify: VerifyCondition
    description: str = ""

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")

        # A RANGE check cannot catch this bug, and it is worth being precise
        # about why: Rail ids are 0-4 and Device ids are 0-3, so every Rail
        # except PAYLOAD is simultaneously a valid Device id. Rail.RADIO is 1;
        # so is Device.MAG. Any check that accepts a bare int has already lost.
        #
        # So the target must arrive as the ENUM MEMBER, and the type is what is
        # checked. IntEnum members still behave as ints everywhere downstream
        # (the executor and the ports are unchanged), but Rail.RADIO is not an
        # instance of Device, which is the distinction that actually matters.
        if self.action == RecoveryAction.RESET_DEVICE:
            if self.target == SYSTEM_TARGET:
                return
            if not isinstance(self.target, Device):
                raise ValueError(
                    f"RESET_DEVICE target must be a Device member or "
                    f"SYSTEM_TARGET, got {self.target!r} "
                    f"({type(self.target).__name__}). Passing a Rail here is "
                    f"the F2 defect and is not detectable by value."
                )
        elif self.action == RecoveryAction.POWER_CYCLE:
            if not isinstance(self.target, Rail):
                raise ValueError(
                    f"POWER_CYCLE target must be a Rail member, got "
                    f"{self.target!r} ({type(self.target).__name__}). Passing a "
                    f"Device here is the F2 defect and is not detectable by value."
                )


@dataclass
class Campaign:
    """
    A bounded response to one fault condition.

    `rung_index` and `attempts_on_rung` are the two numbers KySat-2 needed and
    did not have. They are what persistence must preserve across a reset.
    """

    trigger: int                     # FaultFlag value that authorised this
    rungs: List[Rung]
    rung_index: int = 0
    attempts_on_rung: int = 0
    total_attempts: int = 0
    state: CampaignState = CampaignState.IDLE
    verify_deadline: Optional[float] = None
    started_at: float = 0.0

    @property
    def current_rung(self) -> Optional[Rung]:
        if 0 <= self.rung_index < len(self.rungs):
            return self.rungs[self.rung_index]
        return None

    @property
    def finished(self) -> bool:
        return self.state in (CampaignState.SUCCEEDED, CampaignState.EXHAUSTED)

    # --- persistence ------------------------------------------------------
    # Plain dicts, not pickle: this has to survive a schema change and, later,
    # become a fixed-size record in STM32 backup SRAM.

    def to_dict(self) -> dict:
        # schema_version 2 adds target_kind. A bare integer target loses the one
        # piece of information that distinguishes Rail.RADIO from Device.MAG
        # (both are 1), so a v1 record cannot be restored into a type-checked
        # Rung. v1 records are therefore rejected on restore rather than
        # guessed at -- and every v1 record was written by the code that had
        # the F2 defect, so discarding them is correct on its own merits.
        return {
            "schema_version": 2,
            "trigger": int(self.trigger),
            "rung_index": self.rung_index,
            "attempts_on_rung": self.attempts_on_rung,
            "total_attempts": self.total_attempts,
            "state": int(self.state),
            "started_at": self.started_at,
            "rungs": [
                {"action": int(r.action), "target": int(r.target),
                 "target_kind": _kind_of(r.target),
                 "max_attempts": r.max_attempts, "verify": int(r.verify),
                 "description": r.description}
                for r in self.rungs
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Campaign":
        """
        Rebuild from persisted state, validating VALUES and not merely types.

        F6: this used to check the schema version and the field types and stop
        there, so a corrupted NVM record could supply a negative rung index, a
        negative attempt count, an index past the end of the ladder, or an empty
        ladder, and all of them were accepted. Every case happened to fail safe
        -- I could not construct an unbounded retry from poisoned state -- but
        that was a property of downstream bounds checks, not of this function,
        and safety resting on luck is worth converting into safety resting on a
        check. Everything raises ValueError, which import_recovery_state()
        already handles by discarding the record.
        """
        if d.get("schema_version") != 2:
            raise ValueError(f"unsupported campaign schema {d.get('schema_version')!r}")

        rungs = [Rung(action=RecoveryAction(r["action"]),
                      target=_target_from(r["target"], r.get("target_kind")),
                      max_attempts=r["max_attempts"], verify=VerifyCondition(r["verify"]),
                      description=r.get("description", ""))
                 for r in d["rungs"]]
        if not rungs:
            raise ValueError("campaign has no rungs")

        rung_index = d["rung_index"]
        attempts_on_rung = d["attempts_on_rung"]
        total_attempts = d["total_attempts"]
        if not isinstance(rung_index, int) or isinstance(rung_index, bool):
            raise ValueError(f"rung_index must be an int, got {rung_index!r}")
        # len(rungs) is a legal value: it is what an exhausted campaign exports.
        if not 0 <= rung_index <= len(rungs):
            raise ValueError(
                f"rung_index {rung_index} outside [0, {len(rungs)}]")
        if attempts_on_rung < 0 or total_attempts < 0:
            raise ValueError(
                f"negative attempt counters ({attempts_on_rung}, {total_attempts})")
        if attempts_on_rung > total_attempts:
            raise ValueError(
                f"attempts_on_rung {attempts_on_rung} exceeds total {total_attempts}")

        return cls(
            trigger=d["trigger"],
            rungs=rungs,
            rung_index=rung_index,
            attempts_on_rung=attempts_on_rung,
            total_attempts=total_attempts,
            state=CampaignState(d["state"]),
            started_at=d.get("started_at", 0.0),
        )


def comms_loss_ladder(radio_device: Device = Device.RADIO,
                      radio_rail: Rail = Rail.RADIO) -> List[Rung]:
    """
    The CSSWE ladder, ordered least- to most-disruptive.

    TWO parameters, not one. The previous single-integer signature was the F2
    defect: callers passed Rail.RADIO and it was used as both the device id for
    rung 0 and the rail id for rung 1, so the "soft reset the radio" rung
    actually reset the magnetometer. Defaulted so callers cannot get the order
    wrong by accident, and validated in Rung.__post_init__ regardless.

    Rung 0 is a device-level reset rather than a power cycle because the
    cheapest action that could work should be tried first. Note that it
    currently reports unavailable on this platform -- that is honest, and the
    ladder escalating past it is exactly the behaviour being tested.

    Rung 2 is a full system reset: deliberately NOT another radio action,
    because R4 says a ladder must not depend solely on the subsystem it is
    recovering. If two radio-targeted rungs both failed, the next hypothesis
    has to be something other than the radio.
    """
    return [
        Rung(RecoveryAction.RESET_DEVICE, radio_device, max_attempts=1,
             verify=VerifyCondition.RADIO_RESPONSIVE,
             description="soft reset the radio"),
        Rung(RecoveryAction.POWER_CYCLE, radio_rail, max_attempts=2,
             verify=VerifyCondition.RADIO_RESPONSIVE,
             description="power-cycle the radio rail"),
        Rung(RecoveryAction.RESET_DEVICE, SYSTEM_TARGET, max_attempts=1,
             verify=VerifyCondition.RADIO_RESPONSIVE,
             description="full system reset (target -1 = whole spacecraft)"),
    ]
