"""
Tests for the ground-side FDIR timeline reconstruction (Phase 8).

The value being protected here is narrative honesty. The timeline is what a
reviewer will actually read to decide whether the autonomy did something
sensible, so the two things worth guarding are (a) that edges are found at the
right times and (b) that the ADVISORY/AUTHORITATIVE boundary is rendered
truthfully. A timeline that quietly paints ML_ANOMALY the same red as
UNDERVOLTAGE_CRITICAL would misrepresent the entire architecture to the one
audience that matters.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ground-station"))

from icd import FaultFlag, HealthFlag, Mode  # noqa: E402
from simulator.protocol import TelemetryPacket  # noqa: E402
from timeline import (  # noqa: E402
    ADVISORY_ONLY_FLAGS,
    build_timeline,
    flag_authority,
    summarise,
)


def pkt(t_ms, mode=Mode.NOMINAL, faults=0, health=HealthFlag.ALL_OK, seq=0):
    """Minimal packet: the timeline only reads 4 of the 22 fields."""
    return TelemetryPacket(
        seq_num=seq, timestamp_ms=t_ms, mode=int(mode),
        fault_flags=int(faults), health_flags=int(health),
        temp_c=25.0, accel_x=0.0, accel_y=0.0, accel_z=9.81,
        gyro_x=0.0, gyro_y=0.0, gyro_z=0.0,
        mag_x=0.0, mag_y=0.0, mag_z=0.0,
        bus_voltage_v=5.0, bus_current_a=0.4, uptime_s=t_ms // 1000,
        cmd_rx_count=0, cmd_accept_count=0, cmd_reject_count=0,
        corrupted_rx_count=0,
    )


# --- the authority boundary, which is the whole point ---------------------

def test_ml_and_adaptive_anomaly_are_rendered_advisory_only():
    """
    The architectural invariant, restated at the display layer. If either of
    these ever reports anything else, either the engine's authority sets grew
    a flag they must never contain, or this renderer lies about them.
    """
    assert flag_authority(FaultFlag.ML_ANOMALY) == "advisory only"
    assert flag_authority(FaultFlag.ADAPTIVE_ANOMALY) == "advisory only"


def test_advisory_flags_never_overlap_the_authority_sets():
    from fdir.engine import RECOVERY_AUTHORITY_FLAGS, SAFE_MODE_TRIGGER_FLAGS
    assert not (ADVISORY_ONLY_FLAGS & SAFE_MODE_TRIGGER_FLAGS)
    assert not (ADVISORY_ONLY_FLAGS & RECOVERY_AUTHORITY_FLAGS)


@pytest.mark.parametrize("flag,expected", [
    (FaultFlag.UNDERVOLTAGE_CRITICAL, "commands SAFE"),
    (FaultFlag.THERMAL_ANOMALY, "commands SAFE"),
    (FaultFlag.SENSOR_LOCKUP, "commands SAFE"),
    (FaultFlag.COMMS_LOSS, "can authorise recovery"),
    (FaultFlag.SENSOR_TIMEOUT, "can authorise recovery"),
    (FaultFlag.ML_ANOMALY, "advisory only"),
    (FaultFlag.WATCHDOG_RESET, "informational"),
    (FaultFlag.UNDERVOLTAGE_WARNING, "informational"),
])
def test_authority_labels(flag, expected):
    assert flag_authority(flag) == expected


def test_an_advisory_fault_never_renders_as_critical():
    events = build_timeline([
        pkt(0), pkt(1000, faults=FaultFlag.ML_ANOMALY),
    ])
    ml = [e for e in events if e.label == "ML_ANOMALY"][0]
    assert ml.severity == "info"
    assert ml.authority == "advisory only"


# --- edge detection -------------------------------------------------------

def test_empty_input_is_not_an_error():
    assert build_timeline([]) == []


def test_a_single_packet_yields_only_the_starting_mode():
    events = build_timeline([pkt(0, mode=Mode.BOOT)])
    assert len(events) == 1
    assert events[0].label == "start in BOOT"


def test_steady_state_produces_no_events():
    """No transitions means no rows. A timeline that fires every tick is noise."""
    packets = [pkt(i * 1000) for i in range(20)]
    events = build_timeline(packets)
    assert [e.kind for e in events] == ["mode"]  # just the start marker


def test_rising_and_falling_edges_are_both_captured():
    events = build_timeline([
        pkt(0),
        pkt(1000, faults=FaultFlag.COMMS_LOSS),
        pkt(2000, faults=FaultFlag.COMMS_LOSS),
        pkt(3000, faults=0),
    ])
    kinds = [(e.kind, e.label, e.t_s) for e in events if e.kind.startswith("fault")]
    assert kinds == [("fault_set", "COMMS_LOSS", 1.0),
                     ("fault_clear", "COMMS_LOSS", 3.0)]


def test_simultaneous_flags_all_appear():
    both = FaultFlag.COMMS_LOSS | FaultFlag.UNDERVOLTAGE_CRITICAL
    events = build_timeline([pkt(0), pkt(1000, faults=both)])
    names = {e.label for e in events if e.kind == "fault_set"}
    assert names == {"COMMS_LOSS", "UNDERVOLTAGE_CRITICAL"}


def test_mode_transitions_are_captured_with_direction():
    events = build_timeline([
        pkt(0, mode=Mode.BOOT), pkt(1000, mode=Mode.NOMINAL),
        pkt(2000, mode=Mode.SAFE), pkt(3000, mode=Mode.NOMINAL),
    ])
    modes = [e.label for e in events if e.kind == "mode"]
    assert modes == ["start in BOOT", "BOOT -> NOMINAL",
                     "NOMINAL -> SAFE", "SAFE -> NOMINAL"]


def test_entering_safe_is_critical_but_leaving_it_is_not():
    """
    Regression: severity was matched with a bare `"SAFE" in label` substring
    test, which painted the recovery event in the same alarm colour as the
    failure event.
    """
    events = build_timeline([
        pkt(0, mode=Mode.NOMINAL), pkt(1000, mode=Mode.SAFE),
        pkt(2000, mode=Mode.NOMINAL),
    ])
    by_label = {e.label: e.severity for e in events if e.kind == "mode"}
    assert by_label["NOMINAL -> SAFE"] == "critical"
    assert by_label["SAFE -> NOMINAL"] == "recovery"


def test_health_flags_are_active_low_so_a_cleared_bit_is_a_loss():
    """
    HealthFlag is ALL_OK-by-default: the bit being SET means healthy. Getting
    this backwards would report every healthy sensor as failed.
    """
    degraded = HealthFlag.ALL_OK & ~HealthFlag.IMU_OK
    events = build_timeline([pkt(0), pkt(1000, health=degraded),
                             pkt(2000, health=HealthFlag.ALL_OK)])
    hs = [(e.kind, e.label) for e in events if e.kind.startswith("health")]
    assert hs == [("health_lost", "IMU_OK"), ("health_restored", "IMU_OK")]


def test_a_reboot_is_surfaced_and_does_not_fold_the_axis():
    """
    timestamp_ms restarts at reboot. Naive subtraction goes negative exactly
    when the operator most needs a readable axis.
    """
    events = build_timeline([
        pkt(0), pkt(5000), pkt(100, mode=Mode.BOOT),  # clock restarted
    ])
    assert all(e.t_s >= 0.0 for e in events)
    assert any(e.label == "flight computer rebooted" for e in events)


# --- summary --------------------------------------------------------------

def test_summary_measures_flag_to_safe_not_fault_onset():
    """
    The ground station cannot see injection time, so the only latency it may
    honestly report is first-flag -> SAFE. Claiming an onset-referenced number
    here would be a test-harness measurement dressed up as a flight one.
    """
    s = summarise(build_timeline([
        pkt(0),
        pkt(1000, faults=FaultFlag.UNDERVOLTAGE_CRITICAL),
        pkt(1500, mode=Mode.SAFE, faults=FaultFlag.UNDERVOLTAGE_CRITICAL),
    ]))
    assert s["first_fault"] == "UNDERVOLTAGE_CRITICAL"
    assert s["first_fault_t_s"] == 1.0
    assert s["safe_entered_t_s"] == 1.5
    assert s["flag_to_safe_s"] == 0.5


def test_summary_is_all_none_when_nothing_happened():
    s = summarise(build_timeline([pkt(0), pkt(1000), pkt(2000)]))
    assert s["first_fault"] is None
    assert s["flag_to_safe_s"] is None
    assert s["advisory_only_events"] == 0


def test_summary_counts_advisory_events_separately():
    s = summarise(build_timeline([
        pkt(0),
        pkt(1000, faults=FaultFlag.ML_ANOMALY),
        pkt(2000, faults=FaultFlag.ML_ANOMALY | FaultFlag.ADAPTIVE_ANOMALY),
    ]))
    assert s["advisory_only_events"] == 2


def test_advisory_flags_alone_never_produce_a_safe_transition():
    """
    Not a property of this module -- a property of the system, observed
    through it. If a run ever shows SAFE entered with only advisory flags
    set, the engine's authority boundary has been breached.
    """
    events = build_timeline([
        pkt(0), pkt(1000, faults=FaultFlag.ML_ANOMALY),
        pkt(2000, faults=FaultFlag.ML_ANOMALY),
    ])
    assert not any(e.kind == "mode" and e.label.endswith("-> SAFE")
                   for e in events)
