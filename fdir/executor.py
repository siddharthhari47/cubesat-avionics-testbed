"""
Carries out recovery intents against the hardware ports.

This is the only place in the system that commands an actuator. FDIREngine
proposes; this executes. Keeping them apart is what lets the engine stay a
pure function and what makes "exactly one power cycle was attempted, at T+30 s"
a directly assertable fact.

WHAT THIS DELIBERATELY DOES NOT DO YET (Phase 5):
  * verify that an action achieved anything
  * bound retries or escalate when it didn't
  * persist attempt counters across a reset

Those are the KySat-2 lessons and they are the substance of Phase 5. Executing
an action and *assuming* it worked is precisely the failure this project exists
to study, so the gap is called out here rather than quietly left as an
implication. What this class does provide is the record needed to build them:
every attempt is logged with its outcome, so Phase 5 has something to verify
against and something to count.

A power cycle here is two commands separated by a dwell -- power off, wait,
power on -- because off-time is load-bearing: a latch clears only if power was
genuinely removed for long enough. The executor cannot block, so the dwell is
tracked across ticks rather than slept through.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from . import config as cfg
from .ports import PowerPort, RecoveryAction, RecoveryIntent, ResetPort


@dataclass
class ExecutionRecord:
    """What was attempted, and what the hardware said about it."""

    intent: RecoveryIntent
    started_at: float
    completed_at: Optional[float] = None
    accepted: bool = True
    detail: str = ""


@dataclass
class _PendingCycle:
    intent: RecoveryIntent
    powered_off_at: float
    record: ExecutionRecord


class RecoveryExecutor:
    def __init__(self, power: PowerPort, reset: Optional[ResetPort] = None):
        self._power = power
        self._reset = reset
        self.history: List[ExecutionRecord] = []
        self._in_flight: Optional[_PendingCycle] = None

    @property
    def busy(self) -> bool:
        return self._in_flight is not None

    def step(self, engine, now: float) -> None:
        """
        Drain the engine's proposals and advance any action already underway.

        Call once per tick, after engine.tick(). One action at a time: a second
        recovery starting while the first is mid-dwell would leave a rail off
        and the reason for it ambiguous.
        """
        self._advance_in_flight(now)

        for intent in engine.take_pending_intents():
            if self._in_flight is not None:
                self.history.append(ExecutionRecord(
                    intent=intent, started_at=now, completed_at=now, accepted=False,
                    detail="refused: another recovery action is already in progress",
                ))
                continue
            self._begin(intent, now)

    def _begin(self, intent: RecoveryIntent, now: float) -> None:
        record = ExecutionRecord(intent=intent, started_at=now)

        if intent.action == RecoveryAction.POWER_CYCLE:
            if not self._power.set_enabled(intent.target, False):
                record.accepted = False
                record.completed_at = now
                record.detail = "power-off command refused by port"
                self.history.append(record)
                return
            record.detail = "powered off, waiting out dwell"
            self._in_flight = _PendingCycle(intent=intent, powered_off_at=now, record=record)
            return

        if intent.action == RecoveryAction.RESET_DEVICE:
            ok = self._reset.reset_device(intent.target) if self._reset else False
            record.accepted = ok
            record.completed_at = now
            record.detail = "device reset issued" if ok else "device reset unavailable"
            self.history.append(record)
            return

        record.accepted = False
        record.completed_at = now
        record.detail = f"unsupported action {intent.action!r}"
        self.history.append(record)

    def _advance_in_flight(self, now: float) -> None:
        cycle = self._in_flight
        if cycle is None:
            return
        if now - cycle.powered_off_at < cfg.POWER_CYCLE_OFF_TIME_S:
            return          # dwell not yet satisfied; power stays off

        ok = self._power.set_enabled(cycle.intent.target, True)
        cycle.record.completed_at = now
        cycle.record.accepted = ok
        cycle.record.detail = (
            f"power cycle complete (off for {now - cycle.powered_off_at:.3f} s)"
            if ok else "power-on command refused by port"
        )
        self.history.append(cycle.record)
        self._in_flight = None

    # ---- reporting ---------------------------------------------------------

    def attempts_for(self, action: RecoveryAction, target: int) -> int:
        return sum(1 for r in self.history
                   if r.intent.action == action and r.intent.target == target)
