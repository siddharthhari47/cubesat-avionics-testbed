"""
Reconstructs the FDIR narrative from telemetry already in hand.

THE POINT: mode, fault_flags and health_flags arrive in EVERY packet, and the
ground station already buffers 1800 of them. Diffing consecutive packets yields
the whole detection -> isolation -> recovery -> verification story for free --
edge times, mode sequence, which flag rose first, how long each state lasted.
None of that needed a wire change; it was simply being discarded.

This is deliberately Tier 1 of the three options the observability audit laid
out. Tier 2 (spare flag bits) and Tier 3 (a dedicated FDIR-event packet) buy
things this cannot -- the debounce progress a detector rejected, the measured
value that tripped a threshold -- but both cost ICD changes, and neither is
needed to make the story legible. Do the free thing first.

WHAT THIS CANNOT SHOW, stated so nobody assumes otherwise: validation
(a glitch correctly rejected before latching) and diagnosis detail (the value
that tripped a detector, per-detector attribution) never leave the flight
software. A viewer cannot distinguish "a glitch was correctly rejected" from
"nothing happened", which is exactly the evidence FDIR-008's false-positive
budget is about. That needs Tier 3.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fdir.engine import RECOVERY_AUTHORITY_FLAGS, SAFE_MODE_TRIGGER_FLAGS  # noqa: E402
from icd import FaultFlag, Mode  # noqa: E402

# Flags that can only ever advise. Rendering these identically to a flag that
# can command SAFE makes the architecture's central boundary invisible to the
# operator -- an advisory anomaly looked exactly as alarming as a critical
# undervoltage. This is the cheapest correction available and needs no wire
# change at all.
ADVISORY_ONLY_FLAGS = FaultFlag.ADAPTIVE_ANOMALY | FaultFlag.ML_ANOMALY


def flag_authority(flag: FaultFlag) -> str:
    """How much power does this flag actually carry? Operator-facing."""
    if flag & SAFE_MODE_TRIGGER_FLAGS:
        return "commands SAFE"
    if flag & RECOVERY_AUTHORITY_FLAGS:
        return "can authorise recovery"
    if flag & ADVISORY_ONLY_FLAGS:
        return "advisory only"
    return "informational"


@dataclass
class TimelineEvent:
    t_s: float
    kind: str          # "mode", "fault_set", "fault_clear", "health_lost", "health_restored"
    label: str
    detail: str = ""
    authority: str = ""

    @property
    def severity(self) -> str:
        # ENTERING safe mode is critical; LEAVING it is recovery. A naive
        # "SAFE" substring test marked both the same, which would paint the
        # good-news event red.
        if self.kind == "mode" and self.label.endswith("-> SAFE"):
            return "critical"
        if self.kind == "mode" and self.label.startswith("SAFE ->"):
            return "recovery"
        if self.kind == "fault_set":
            return {"commands SAFE": "critical",
                    "can authorise recovery": "warning"}.get(self.authority, "info")
        if self.kind == "health_lost":
            return "warning"
        return "info"


def _mode_name(value: int) -> str:
    """
    G2: `Mode(value).name` raises ValueError on an out-of-range mode, and a
    single CRC-valid packet carrying one crashed the whole timeline -- and the
    dashboard with it. A viewer is far better served by "mode 99" than by a
    stack trace.
    """
    try:
        return Mode(value).name
    except ValueError:
        return f"mode {value}"


def _t(pkt, first_ms: int) -> float:
    """
    Seconds since the first buffered packet.

    timestamp_ms RESETS on a watchdog reboot, so a naive subtraction goes
    negative exactly at the moment the narrative most needs to be readable.
    Clamping at zero keeps the axis monotonic; the reboot itself shows up as its
    own event rather than as a fold in the timeline.
    """
    return max(0.0, (pkt.timestamp_ms - first_ms) / 1000.0)


def build_timeline(packets: Sequence) -> List[TimelineEvent]:
    """Diff consecutive packets into an ordered event list."""
    if not packets:
        return []

    events: List[TimelineEvent] = []
    first_ms = packets[0].timestamp_ms
    prev = None

    for pkt in packets:
        t = _t(pkt, first_ms)
        if prev is None:
            events.append(TimelineEvent(t, "mode", f"start in {_mode_name(pkt.mode)}"))
            prev = pkt
            continue

        if pkt.mode != prev.mode:
            events.append(TimelineEvent(
                t, "mode", f"{_mode_name(prev.mode)} -> {_mode_name(pkt.mode)}"))

        # timestamp going backwards is the reboot signature -- surface it rather
        # than letting it silently corrupt the axis.
        if pkt.timestamp_ms < prev.timestamp_ms:
            events.append(TimelineEvent(t, "mode", "flight computer rebooted",
                                        detail="mission clock restarted"))

        rose = FaultFlag(pkt.fault_flags & ~prev.fault_flags)
        fell = FaultFlag(prev.fault_flags & ~pkt.fault_flags)
        for flag in FaultFlag:
            if flag == FaultFlag.NONE:
                continue
            if flag & rose:
                events.append(TimelineEvent(t, "fault_set", flag.name,
                                            authority=flag_authority(flag)))
            if flag & fell:
                events.append(TimelineEvent(t, "fault_clear", flag.name,
                                            authority=flag_authority(flag)))

        lost = prev.health_flags & ~pkt.health_flags
        regained = pkt.health_flags & ~prev.health_flags
        for bit in range(16):
            mask = 1 << bit
            if lost & mask:
                events.append(TimelineEvent(t, "health_lost", _health_name(mask)))
            if regained & mask:
                events.append(TimelineEvent(t, "health_restored", _health_name(mask)))

        prev = pkt

    return events


def _health_name(mask: int) -> str:
    from icd import HealthFlag
    for h in HealthFlag:
        if h not in (HealthFlag.NONE, HealthFlag.ALL_OK) and int(h) == mask:
            return h.name
    return f"bit{mask.bit_length() - 1}"


def summarise(events: Sequence[TimelineEvent]) -> dict:
    """
    Headline numbers a viewer wants without reading every row.

    Latencies here are relative to the FIRST FLAG RISING, not to fault onset --
    the ground station has no way to know when a fault was physically injected,
    because injection happens at the spacecraft, not over this link. Reporting
    an onset-referenced latency would require an out-of-band marker and would be
    a test-harness measurement, not a flight one.
    """
    first_fault = next((e for e in events if e.kind == "fault_set"), None)
    first_safe = next((e for e in events
                       if e.kind == "mode" and e.label.endswith("SAFE")), None)
    recovered = next((e for e in events
                      if e.kind == "mode" and e.label.startswith("SAFE ->")), None)
    return {
        "events": len(events),
        "first_fault": first_fault.label if first_fault else None,
        "first_fault_t_s": first_fault.t_s if first_fault else None,
        "safe_entered_t_s": first_safe.t_s if first_safe else None,
        "flag_to_safe_s": (round(first_safe.t_s - first_fault.t_s, 3)
                           if first_safe and first_fault else None),
        "left_safe_t_s": recovered.t_s if recovered else None,
        "advisory_only_events": sum(1 for e in events
                                    if e.authority == "advisory only"),
    }
