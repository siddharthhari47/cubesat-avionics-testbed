"""
Carries out recovery intents against the hardware ports.

This is the only place in the system that commands an actuator. FDIREngine
proposes; this executes. Keeping them apart is what lets the engine stay a
pure function and what makes "exactly one power cycle was attempted, at T+30 s"
a directly assertable fact.

WHAT THIS DELIBERATELY DOES NOT DECIDE (and must not):
whether an action WORKED. It reports only that an action completed and what the
port said about the command. Verification, bounded retries and escalation live
in the engine (fdir/recovery.py), because deciding "did the fault clear" means
observing telemetry afterwards, and treating "the port accepted the command" as
success is precisely the conflation KySat-2 died of -- it reset hourly forever,
each reset re-entering the same condition, with nothing checking whether any of
it helped.

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
        self._advance_in_flight(engine, now)

        for intent in engine.take_pending_intents():
            if self._in_flight is not None:
                self.history.append(ExecutionRecord(
                    intent=intent, started_at=now, completed_at=now, accepted=False,
                    detail="refused: another recovery action is already in progress",
                ))
                engine.note_action_completed(now, accepted=False)
                continue
            self._begin(engine, intent, now)

    def _begin(self, engine, intent: RecoveryIntent, now: float) -> None:
        record = ExecutionRecord(intent=intent, started_at=now)

        if intent.action == RecoveryAction.POWER_CYCLE:
            if not self._power.set_enabled(intent.target, False):
                record.accepted = False
                record.completed_at = now
                record.detail = "power-off command refused by port"
                self.history.append(record)
                engine.note_action_completed(now, accepted=False)
                return
            record.detail = "powered off, waiting out dwell"
            self._in_flight = _PendingCycle(intent=intent, powered_off_at=now, record=record)
            return

        if intent.action == RecoveryAction.RESET_DEVICE:
            if intent.target < 0:
                # Whole-spacecraft reset. Deliberately a different code path:
                # it does not return on real hardware, so it can never be
                # treated as "an action that completed and can be verified"
                # in the same breath as a device-level one.
                if self._reset is not None:
                    self._reset.reset_system(intent.reason)
                    ok = True
                else:
                    ok = False
            else:
                ok = self._reset.reset_device(intent.target) if self._reset else False
            record.accepted = ok
            record.completed_at = now
            record.detail = "device reset issued" if ok else "device reset unavailable"
            self.history.append(record)
            engine.note_action_completed(now, accepted=ok)
            return

        record.accepted = False
        record.completed_at = now
        record.detail = f"unsupported action {intent.action!r}"
        self.history.append(record)
        engine.note_action_completed(now, accepted=False)

    def _advance_in_flight(self, engine, now: float) -> None:
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
        # Report completion, NOT success. Whether the fault actually cleared is
        # re-observed from telemetry by the engine's verification window -- the
        # port accepting a command is not evidence of anything.
        engine.note_action_completed(now, accepted=ok)

    # ---- reporting ---------------------------------------------------------

    def attempts_for(self, action: RecoveryAction, target: int) -> int:
        return sum(1 for r in self.history
                   if r.intent.action == action and r.intent.target == target)
