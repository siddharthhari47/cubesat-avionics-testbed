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

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional

from .ports import RecoveryAction


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


@dataclass
class Rung:
    """One step of an escalation ladder."""

    action: RecoveryAction
    target: int
    max_attempts: int
    verify: VerifyCondition
    description: str = ""


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
        return {
            "schema_version": 1,
            "trigger": int(self.trigger),
            "rung_index": self.rung_index,
            "attempts_on_rung": self.attempts_on_rung,
            "total_attempts": self.total_attempts,
            "state": int(self.state),
            "started_at": self.started_at,
            "rungs": [
                {"action": int(r.action), "target": r.target,
                 "max_attempts": r.max_attempts, "verify": int(r.verify),
                 "description": r.description}
                for r in self.rungs
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Campaign":
        if d.get("schema_version") != 1:
            raise ValueError(f"unsupported campaign schema {d.get('schema_version')!r}")
        return cls(
            trigger=d["trigger"],
            rungs=[Rung(action=RecoveryAction(r["action"]), target=r["target"],
                        max_attempts=r["max_attempts"], verify=VerifyCondition(r["verify"]),
                        description=r.get("description", ""))
                   for r in d["rungs"]],
            rung_index=d["rung_index"],
            attempts_on_rung=d["attempts_on_rung"],
            total_attempts=d["total_attempts"],
            state=CampaignState(d["state"]),
            started_at=d.get("started_at", 0.0),
        )


def comms_loss_ladder(radio_rail: int) -> List[Rung]:
    """
    The CSSWE ladder, ordered least- to most-disruptive.

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
        Rung(RecoveryAction.RESET_DEVICE, radio_rail, max_attempts=1,
             verify=VerifyCondition.RADIO_RESPONSIVE,
             description="soft reset the radio"),
        Rung(RecoveryAction.POWER_CYCLE, radio_rail, max_attempts=2,
             verify=VerifyCondition.RADIO_RESPONSIVE,
             description="power-cycle the radio rail"),
        Rung(RecoveryAction.RESET_DEVICE, -1, max_attempts=1,
             verify=VerifyCondition.RADIO_RESPONSIVE,
             description="full system reset (target -1 = whole spacecraft)"),
    ]
