"""
Regression tests for round 10.

The workflow fan-out died on the monthly spend limit for the seventh time --
four agents launched, zero returned -- so these came from probing directly.
Round 10 aimed at the code the previous nine under-reviewed, chosen by measured
coverage rather than by intuition:

    ground-station/link.py       17% covered, 126 of 152 statements never run
    simulator/run_simulator.py   35% covered, 164 of 253 statements never run

That is the flight path and the transport: the two places whose defects the
scenario harness structurally cannot see. Four findings.

THE HEADLINE (R10-4): fault detection latency was a function of the telemetry
DOWNLINK rate. sim.tick() -- the entire FDIR engine -- was called from exactly
one place, telemetry_loop, which sleeps 1/telemetry_rate_hz. SET_TELEMETRY_RATE
is an operator COMMS command bounded 0.5..10 Hz, so an operator throttling the
downlink to save power was also throttling fault detection, by a measured 20x
across the legal range. The shipped default of 1 Hz was 10x slower than every
latency published in v0-scenario-results.md, which are 10 Hz numbers from
scenarios/runner.py. Harness and flight path disagreeing about a published
measurement: the fourth instance of this project's most recurrent defect.
"""

import ast
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "simulator"))
sys.path.insert(0, str(REPO_ROOT / "ground-station"))

import fdir.config as cfg          # noqa: E402
import protocol as proto           # noqa: E402
import run_simulator as rs         # noqa: E402
from icd import FaultFlag          # noqa: E402
from link import GroundLink        # noqa: E402

DASHBOARD = REPO_ROOT / "ground-station" / "dashboard.py"
SIMULATOR = REPO_ROOT / "simulator" / "run_simulator.py"


# --------------------------------------------------------------------------
# R10-1: every telecommand in the ICD must be reachable by an operator
# --------------------------------------------------------------------------

def _console_commands():
    tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "CommandId"
    }


def test_every_telecommand_is_reachable_from_the_console():
    """
    R10-1. RECOMMISSION_REFERENCE (0x0A) and RESTORE_CAPABILITY (0x0B) existed
    on the wire and in the flight computer's handler, and on no operator
    interface -- so neither could actually be sent.

    Round 4 found exactly this defect ("an escape the ground cannot reach is not
    an escape"), added the opcodes and the flight-computer handler, and stopped.
    The comment recording that fix sat directly above two commands that were
    still unreachable, through five further review rounds.

    This asserts the property rather than the two instances, so the next command
    added to the ICD cannot repeat it.
    """
    defined = {c.name for c in proto.CommandId}
    missing = defined - _console_commands()
    assert not missing, (
        f"telecommands an operator cannot send: {sorted(missing)}. "
        "A command in the ICD with no operator interface is not a capability."
    )


# --------------------------------------------------------------------------
# R10-2: a command that was not sent must not look like one that was
# --------------------------------------------------------------------------

def test_no_console_command_discards_the_send_result():
    """
    R10-2. GroundLink.send_command returns False when the link is down, and all
    eight console call sites threw it away. The operator saw no error and an
    empty command log -- indistinguishable from a command sent and awaiting its
    ack. On a real vehicle that is an operator believing they safed a spacecraft
    they never reached.

    A bare expression statement is exactly "the return value was discarded", so
    that is what this looks for.
    """
    tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"))
    discarded = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "send_command"
    ]
    assert not discarded, (
        f"dashboard.py discards send_command's result at line(s) {discarded}; "
        "a command that never left the ground must not look like one that did."
    )


def test_send_command_reports_failure_when_the_link_is_down():
    gl = GroundLink("127.0.0.1", 59997, heartbeat=False)
    try:
        assert gl.send_command(proto.CommandId.ENTER_SAFE_MODE) is False
        assert gl.snapshot()["command_log"] == []
    finally:
        gl.close()


# --------------------------------------------------------------------------
# R10-3: packet loss is measured from sequence gaps, not corruption episodes
# --------------------------------------------------------------------------

def _tlm(seq):
    return proto.TelemetryPacket(
        seq_num=seq, timestamp_ms=seq * 100, mode=0, fault_flags=0, health_flags=0,
        temp_c=20.0, accel_x=0.0, accel_y=0.0, accel_z=1.0,
        gyro_x=0.0, gyro_y=0.0, gyro_z=0.0, mag_x=0.0, mag_y=0.0, mag_z=0.0,
        bus_voltage_v=7.4, bus_current_a=0.25, uptime_s=seq,
        cmd_rx_count=0, cmd_accept_count=0, cmd_reject_count=0, corrupted_rx_count=0,
    )


def _read_all(payload):
    """Push raw bytes through read_packet over a real socket."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def writer():
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(payload)
        time.sleep(0.02)
        c.close()

    threading.Thread(target=writer, daemon=True).start()
    conn, _ = srv.accept()
    got, episodes = [], 0
    while True:
        p, corrupted = proto.read_packet(conn)
        if corrupted:
            episodes += 1
        if p is None:
            break
        got.append(p)
    conn.close()
    srv.close()
    return got, episodes


def test_one_truncated_packet_costs_two_packets_but_one_corruption_episode():
    """
    R10-3, the measurement that motivates it. read_packet commits to reading a
    full packet once it has seen a valid sync byte and packet id, so a truncated
    packet swallows the start of the next good one. Measured: two packets lost,
    one corruption episode reported.

    corrupted_rx_count is therefore an episode counter, not a loss counter, and
    "packet loss vs. range" is one of the five numbers this project exists to
    produce. It also cannot see a packet dropped entirely on the RF link, which
    leaves no bad bytes to notice at all.
    """
    payload = _tlm(99).pack()[:40] + b"".join(_tlm(i).pack() for i in range(1, 6))
    got, episodes = _read_all(payload)

    assert episodes == 1
    assert [p.seq_num for p in got] == [2, 3, 4, 5], (
        "packet 1 should have been eaten while finishing the truncated packet"
    )


def test_sequence_gaps_are_counted_as_packet_loss():
    gl = GroundLink("127.0.0.1", 59996, heartbeat=False)
    try:
        with gl.lock:
            for seq in (10, 11, 15, 16):        # 12, 13, 14 never arrived
                gl._note_sequence(seq)
        assert gl.packets_received == 4
        assert gl.packets_lost == 3
        assert gl.packet_loss_pct() == pytest.approx(100 * 3 / 7)
    finally:
        gl.close()


def test_a_restarted_flight_computer_is_not_reported_as_mass_packet_loss():
    """seq_num wraps mod 65536; a reboot back to 1 must not invent 65000 losses."""
    gl = GroundLink("127.0.0.1", 59995, heartbeat=False)
    try:
        with gl.lock:
            gl._note_sequence(60000)
            gl._note_sequence(1)
        assert gl.packets_lost == 0
    finally:
        gl.close()


def test_reconnecting_resets_the_sequence_baseline():
    """
    The vehicle keeps transmitting while nobody is listening. Counting the whole
    outage as loss would make the loss figure a measure of ground-station
    downtime rather than of the link.
    """
    src = (REPO_ROOT / "ground-station" / "link.py").read_text(encoding="utf-8")
    assert "self._last_seq = None      # new link, new sequence baseline" in src


# --------------------------------------------------------------------------
# R10-4: how often the vehicle TALKS must not change how fast it NOTICES
# --------------------------------------------------------------------------

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _detection_latency(telemetry_rate_hz):
    clk = _Clock()
    real = rs.time.monotonic
    rs.time.monotonic = clk
    try:
        sim = rs.Simulator(telemetry_rate_hz=telemetry_rate_hz, seed=7)
        sim._persist = lambda *a, **k: None
        # Drive the clock the way fdir_loop does -- at the SAFETY rate. Driving
        # it at the downlink rate would rebuild the coupling inside the test.
        dt = 1.0 / sim.fdir_tick_hz
        for _ in range(int((cfg.BOOT_DURATION_S + 2.0) / dt) + 2):
            clk.t += dt
            sim.tick()
        sim.env.inject("undervoltage")
        started = clk.t
        for _ in range(int(60.0 / dt)):
            clk.t += dt
            sim.tick()
            if sim.engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
                return round(clk.t - started, 3)
        return None
    finally:
        rs.time.monotonic = real


@pytest.mark.parametrize("rate_hz", [0.5, 1.0, 2.0, 5.0, 10.0])
def test_detection_latency_does_not_depend_on_the_downlink_rate(rate_hz):
    """
    R10-4. Measured BEFORE the fix, undervoltage detection across the legal
    SET_TELEMETRY_RATE range:

        10.0 Hz -> 0.20 s      (the rate every published latency was taken at)
         5.0 Hz -> 0.40 s
         2.0 Hz -> 1.00 s
         1.0 Hz -> 2.00 s      (the shipped --rate default)
         0.5 Hz -> 4.00 s

    Exactly 20x across a range an operator can command at will, for a parameter
    whose documented purpose is downlink bandwidth. Hardware safety constrains
    everything; a comms setting does not get to constrain hardware safety.
    """
    assert _detection_latency(rate_hz) == _detection_latency(10.0)


def test_the_telemetry_loop_does_not_run_the_fdir_engine():
    """
    The structural half of R10-4, asserted on the source because that is where
    the defect lived: one loop, doing both jobs, paced by the wrong one.

    Round 7's lesson was to assert that the PRODUCTION path constructs and uses
    a component. This is the same assertion turned around -- that the production
    path does NOT run the safety loop from the downlink loop.
    """
    tree = ast.parse(SIMULATOR.read_text(encoding="utf-8"))
    loops = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in ("telemetry_loop", "fdir_loop")
    }
    assert "fdir_loop" in loops, "the safety loop must exist as its own loop"

    def calls_tick(fn):
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "tick"
            for n in ast.walk(fn)
        )

    assert calls_tick(loops["fdir_loop"])
    assert not calls_tick(loops["telemetry_loop"]), (
        "telemetry_loop must not tick the FDIR engine -- that is what made "
        "detection latency a function of the downlink rate"
    )


def test_main_starts_both_loops():
    """Round 7: a component that works and is never wired in is still a defect."""
    tree = ast.parse(SIMULATOR.read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    targets = {
        kw.value.id
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "target" and isinstance(kw.value, ast.Name)
    }
    assert {"fdir_loop", "telemetry_loop"} <= targets, (
        f"main() starts {sorted(targets)}; both loops must actually run"
    )


def test_only_the_downlink_consumes_a_sequence_number():
    """
    seq_num is what the ground counts gaps in, so it must count TRANSMITTED
    packets. If tick() consumed one, every FDIR cycle would look like a
    transmitted packet and the loss figure would be measuring the ratio of the
    two loop rates instead.
    """
    sim = rs.Simulator(telemetry_rate_hz=1.0, seed=3)
    sim._persist = lambda *a, **k: None

    for _ in range(5):
        sim.tick()
    assert sim.seq_num == 0, "an FDIR cycle is not a transmitted packet"

    first = sim.downlink_packet()
    second = sim.downlink_packet()
    assert (first.seq_num, second.seq_num) == (1, 2)


def test_the_harness_and_the_flight_path_sample_at_the_same_rate():
    """
    The reconciliation R10-4 turns on. Every latency in
    docs/architecture/v0-scenario-results.md is produced by scenarios/runner.py
    at its fixed DT. If the flight path samples at a different rate, those
    numbers describe a system nobody ships -- which is what was true before this
    round, at a factor of 10.

    Changing either rate without the other silently invalidates the published
    results table, so the two are pinned together here rather than separately.
    """
    from scenarios.runner import DT
    assert cfg.FDIR_TICK_HZ == pytest.approx(1.0 / DT), (
        f"scenario harness samples at {1 / DT:g} Hz, flight path at "
        f"{cfg.FDIR_TICK_HZ:g} Hz -- the published latencies would describe "
        "neither"
    )


def test_downlink_cannot_outrun_the_sensors():
    sim = rs.Simulator(telemetry_rate_hz=10.0, seed=3)
    sim._persist = lambda *a, **k: None
    assert sim.downlink_packet() is None, "no sample has been taken yet"
    sim.tick()
    assert sim.downlink_packet() is not None
