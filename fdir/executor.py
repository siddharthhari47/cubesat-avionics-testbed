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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import Rail  # noqa: E402

from .ports import PowerPort, RecoveryAction, RecoveryIntent, ResetPort  # noqa: E402


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
                self._report(engine, intent, now, accepted=False)
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
                self._report(engine, intent, now, accepted=False)
                return
            record.detail = "powered off, waiting out dwell"
            self._in_flight = _PendingCycle(intent=intent, powered_off_at=now, record=record)
            return

        if intent.action in (RecoveryAction.POWER_OFF, RecoveryAction.POWER_ON):
            # One command, no dwell, no restore. Shedding a load is a state
            # change that is meant to PERSIST until something reverses it --
            # unlike a power cycle, whose whole purpose is to come back.
            on = intent.action == RecoveryAction.POWER_ON
            ok = self._power.set_enabled(intent.target, on)
            record.accepted = ok
            record.completed_at = now
            record.detail = (f"rail {'powered' if on else 'shed'}" if ok
                             else f"power {'on' if on else 'off'} command refused by port")
            self.history.append(record)
            # Load shedding reports to the CAPABILITY state machine, not the
            # recovery campaign -- they are different decisions with different
            # bookkeeping, and routing a shed through note_action_completed
            # would advance a campaign that never issued it.
            self._report(engine, intent, now, ok)
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
            self._report(engine, intent, now, ok)
            return

        record.accepted = False
        record.completed_at = now
        record.detail = f"unsupported action {intent.action!r}"
        self.history.append(record)
        self._report(engine, intent, now, accepted=False)

    def _report(self, engine, intent: RecoveryIntent, now: float, accepted: bool) -> None:
        """
        Tell the RIGHT state machine that an action finished.

        Exactly one place decides this, and that is the fix. The routing was
        added in _begin() only, so the busy branch of step() still sent every
        refused intent to the recovery campaign -- including load sheds. A
        POWER_OFF proposed while a comms-recovery power cycle was in its
        off-dwell therefore reported to the campaign, `_shed_pending` was never
        cleared, and `_update_degraded_mode` returned on its first line for the
        rest of the mission. R8 autonomy died silently, with no attempt counted
        and no stand-down logged, and the campaign it collided with was handed a
        completion it never issued.
        """
        if intent.action == RecoveryAction.POWER_OFF:
            engine.note_shed_completed(now, intent.target, accepted)
        elif intent.action == RecoveryAction.POWER_ON:
            engine.note_restore_completed(now, intent.target, accepted)
        else:
            engine.note_action_completed(now, accepted=accepted)

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
        completed = cycle.intent
        self._in_flight = None
        # Report completion, NOT success. Whether the fault actually cleared is
        # re-observed from telemetry by the engine's verification window -- the
        # port accepting a command is not evidence of anything.
        self._report(engine, completed, now, ok)

    def report_rail_states(self, engine, now: float) -> None:
        """
        Read every rail back from the port and tell the engine what is actually true.

        The engine derives capability from a BELIEF about rail power, and holds
        no ports, so it has no way to check that belief itself. That is fine
        while the belief is built from confirmed results -- and not fine after a
        reboot with unreadable NVM, where the engine falls back to assuming
        everything is on. An over-claim it cannot detect is exactly the class
        this review keeps finding, so give it the one thing that resolves it:
        a readback.

        Called once after boot. On real hardware this is a power-good line per
        rail, which is why PowerPort has is_enabled() at all.
        """
        for rail in Rail:
            try:
                engine.note_rail_readback(now, int(rail), bool(self._power.is_enabled(int(rail))))
            except Exception:      # noqa: BLE001 - a port that cannot answer is not fatal
                continue

    # ---- reporting ---------------------------------------------------------

    def attempts_for(self, action: RecoveryAction, target: int) -> int:
        return sum(1 for r in self.history
                   if r.intent.action == action and r.intent.target == target)
