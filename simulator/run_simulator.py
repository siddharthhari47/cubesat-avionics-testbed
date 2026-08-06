"""
V0 flight-computer simulator.

Stands in for the STM32 flight computer (docs/architecture/block-diagram.md).
Implements the BOOT/NOMINAL/SAFE/TEST state machine (docs/architecture/mode-diagram.md),
generates synthetic telemetry (docs/interfaces/telemetry-dictionary.md), and handles
commands (docs/interfaces/command-dictionary.md) over a local TCP socket standing in
for a real UART/radio link.

Fault injection happens at THIS process's own stdin, not over the ground-station link
-- that link is the real spacecraft interface; stdin here plays the role of a test
engineer physically doing something to the hardware (unplugging a sensor, etc).

Run: python simulator/run_simulator.py
Then, at this process's prompt: fault sensor | fault undervoltage | fault drift |
fault clear | reboot | status | quit
"""

import argparse
import random
import socket
import sys
import threading
import time

from protocol import (
    AckPacket, AckStatus, CommandId, CommandPacket, FaultFlag, HealthFlag, Mode,
    TelemetryPacket, read_packet,
)

BOOT_DURATION_S = 2.0               # SYS-003 target: <=5s
SENSOR_TIMEOUT_DEBOUNCE_S = 0.05    # FDIR-002
UNDERVOLTAGE_DEBOUNCE_S = 0.10      # FDIR-003
ADAPTIVE_DEBOUNCE_SAMPLES = 3       # FDIR-006
MIN_ADAPTIVE_SAMPLES = 20           # warm-up before the baseline is trusted to judge anything
COMMS_LOSS_TIMEOUT_S = 5.0          # COM-003

NOMINAL_VOLTAGE = 5.0
UNDERVOLTAGE_INJECTED_VOLTAGE = 3.8     # below the 4.0V critical threshold -> FDIR-003
DRIFT_INJECTED_VOLTAGE = 4.3            # below normal but ABOVE critical -- demonstrates
                                         # FDIR-006 catching what FDIR-003's fixed
                                         # threshold alone would miss
EWMA_ALPHA = 0.1
ADAPTIVE_K = 4.0    # flag if |x - mean| exceeds this many standard deviations


class EwmaStat:
    """Exponentially-weighted mean/variance -- the 'adaptive baseline' behind FDIR-006."""

    def __init__(self, alpha):
        self.alpha = alpha
        self.mean = None
        self.var = 0.0

    def update(self, x):
        if self.mean is None:
            self.mean = x
            return
        delta = x - self.mean
        self.mean += self.alpha * delta
        self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)

    def deviation_sigma(self, x):
        if self.mean is None or self.var <= 0:
            return 0.0
        return abs(x - self.mean) / (self.var ** 0.5)


class Simulator:
    def __init__(self, telemetry_rate_hz):
        self.lock = threading.Lock()
        self.mode = Mode.BOOT
        self.boot_time = time.monotonic()
        self.process_start = time.monotonic()
        self.seq_num = 0
        self.telemetry_rate_hz = telemetry_rate_hz
        self.fault_flags = FaultFlag.NONE
        self.health_flags = HealthFlag.ALL_OK
        self.cmd_rx_count = 0
        self.cmd_accept_count = 0
        self.cmd_reject_count = 0
        self.corrupted_rx_count = 0

        # Injected "physical" fault conditions -- what a test engineer would actually
        # do to real hardware, stood in for via stdin commands (see module docstring).
        self.injected_sensor_timeout = False
        self.injected_undervoltage = False
        self.injected_drift = False

        self._sensor_timeout_since = None
        self._undervoltage_since = None
        self._adaptive_breach_count = 0
        self._adaptive_sample_count = 0
        self.voltage_baseline = EwmaStat(EWMA_ALPHA)

        self.conn_lock = threading.Lock()
        self.conn = None
        self.last_client_seen = None

    # ---- mode/fault machine -------------------------------------------------

    def tick(self):
        with self.lock:
            now = time.monotonic()

            if self.mode == Mode.BOOT and now - self.boot_time >= BOOT_DURATION_S:
                self.mode = Mode.SAFE if self.fault_flags else Mode.NOMINAL

            self._update_sensor_timeout(now)
            self._update_undervoltage(now)
            self._update_comms_loss(now)

            sample = self._generate_sample()
            self._update_adaptive_baseline(sample["bus_voltage_v"])

            if self.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL and self.mode in (Mode.NOMINAL, Mode.TEST):
                self.mode = Mode.SAFE

            return self._build_packet(sample)

    def _update_sensor_timeout(self, now):
        # health_flags reflects live status; fault_flags LATCHES until RESET_FAULTS
        # (see _apply_command) -- that's the whole point of the command existing.
        if self.injected_sensor_timeout:
            self.health_flags &= ~HealthFlag.IMU_OK
            if self._sensor_timeout_since is None:
                self._sensor_timeout_since = now
            elif now - self._sensor_timeout_since >= SENSOR_TIMEOUT_DEBOUNCE_S:
                self.fault_flags |= FaultFlag.SENSOR_TIMEOUT
        else:
            self.health_flags |= HealthFlag.IMU_OK
            self._sensor_timeout_since = None

    def _update_undervoltage(self, now):
        if self.injected_undervoltage:
            if self._undervoltage_since is None:
                self._undervoltage_since = now
            elif now - self._undervoltage_since >= UNDERVOLTAGE_DEBOUNCE_S:
                self.fault_flags |= FaultFlag.UNDERVOLTAGE_CRITICAL
        else:
            self._undervoltage_since = None

    def _update_comms_loss(self, now):
        # Unlike the latched faults above, this one is a live "are we connected right
        # now" indicator -- there's no operator-side condition to confirm before
        # clearing it, reconnecting IS the recovery.
        with self.conn_lock:
            connected = self.conn is not None
        if self.mode == Mode.BOOT:
            return
        if not connected and (self.last_client_seen is None or now - self.last_client_seen >= COMMS_LOSS_TIMEOUT_S):
            self.fault_flags |= FaultFlag.COMMS_LOSS
        elif connected:
            self.fault_flags &= ~FaultFlag.COMMS_LOSS

    def _update_adaptive_baseline(self, voltage):
        # FDIR isn't fully active until NOMINAL, and -- the important part -- a
        # variance estimate built from only a handful of samples is nearly zero,
        # which makes ordinary noise look like a huge number of standard deviations
        # away. Require a warm-up period before the baseline is trusted to judge
        # anything; this is what stops the detector from flagging itself on cold start.
        if self.mode == Mode.BOOT:
            return
        if self.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
            return  # don't let an active fault get learned as the new normal

        warmed_up = self._adaptive_sample_count >= MIN_ADAPTIVE_SAMPLES
        if warmed_up and self.voltage_baseline.deviation_sigma(voltage) > ADAPTIVE_K:
            self._adaptive_breach_count += 1
            if self._adaptive_breach_count >= ADAPTIVE_DEBOUNCE_SAMPLES:
                self.fault_flags |= FaultFlag.ADAPTIVE_ANOMALY
        else:
            self._adaptive_breach_count = 0
            self.voltage_baseline.update(voltage)
            self._adaptive_sample_count += 1

    # ---- telemetry generation -------------------------------------------------

    def _generate_sample(self):
        voltage = NOMINAL_VOLTAGE + random.gauss(0, 0.02)
        if self.injected_undervoltage:
            voltage = UNDERVOLTAGE_INJECTED_VOLTAGE + random.gauss(0, 0.02)
        elif self.injected_drift:
            voltage = DRIFT_INJECTED_VOLTAGE + random.gauss(0, 0.02)

        return {
            "temp_c": 25.0 + random.gauss(0, 0.3),
            "accel_x": random.gauss(0, 0.01),
            "accel_y": random.gauss(0, 0.01),
            "accel_z": 1.0 + random.gauss(0, 0.01),
            "gyro_x": random.gauss(0, 0.5),
            "gyro_y": random.gauss(0, 0.5),
            "gyro_z": random.gauss(0, 0.5),
            "mag_x": 25.0 + random.gauss(0, 1.0),
            "mag_y": -8.0 + random.gauss(0, 1.0),
            "mag_z": 40.0 + random.gauss(0, 1.0),
            "bus_voltage_v": voltage,
            "bus_current_a": 0.4 + random.gauss(0, 0.02),
        }

    def _build_packet(self, sample):
        self.seq_num = (self.seq_num + 1) % 65536
        timestamp_ms = int((time.monotonic() - self.boot_time) * 1000) % (2**32)
        uptime_s = int(time.monotonic() - self.process_start)
        return TelemetryPacket(
            seq_num=self.seq_num, timestamp_ms=timestamp_ms, mode=int(self.mode),
            fault_flags=int(self.fault_flags), health_flags=int(self.health_flags),
            uptime_s=uptime_s, cmd_rx_count=self.cmd_rx_count,
            cmd_accept_count=self.cmd_accept_count, cmd_reject_count=self.cmd_reject_count,
            corrupted_rx_count=self.corrupted_rx_count, **sample,
        )

    # ---- command handling -------------------------------------------------

    def handle_command(self, cmd: CommandPacket) -> AckPacket:
        with self.lock:
            self.cmd_rx_count += 1
            status = self._apply_command(cmd)
            if status == AckStatus.ACCEPTED:
                self.cmd_accept_count += 1
            else:
                self.cmd_reject_count += 1
            return AckPacket(seq_num=cmd.seq_num, cmd_id=cmd.cmd_id, status=status)

    def _apply_command(self, cmd: CommandPacket) -> int:
        if cmd.cmd_id in (CommandId.PING, CommandId.GET_STATUS):
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.SET_TELEMETRY_RATE:
            if 0.5 <= cmd.param <= 10.0:
                self.telemetry_rate_hz = cmd.param
                return AckStatus.ACCEPTED
            return AckStatus.REJECTED_INVALID_PARAM

        if cmd.cmd_id == CommandId.ENTER_SAFE_MODE:
            self.mode = Mode.SAFE
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.EXIT_SAFE_MODE:
            if self.mode != Mode.SAFE:
                return AckStatus.ACCEPTED
            if self.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
                return AckStatus.REJECTED_SAFE_MODE_FAULT_ACTIVE
            self.mode = Mode.NOMINAL
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.RESET_FAULTS:
            if not self.injected_sensor_timeout:
                self.fault_flags &= ~FaultFlag.SENSOR_TIMEOUT
            if not self.injected_undervoltage:
                self.fault_flags &= ~FaultFlag.UNDERVOLTAGE_CRITICAL
            if not self.injected_drift:
                self.fault_flags &= ~FaultFlag.ADAPTIVE_ANOMALY
            self.fault_flags &= ~(FaultFlag.WATCHDOG_RESET | FaultFlag.CORRUPTED_PACKET)
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.REQUEST_LOG:
            return AckStatus.REJECTED_NOT_IMPLEMENTED  # no SD logging until V2

        if cmd.cmd_id == CommandId.ENABLE:
            if self.mode not in (Mode.NOMINAL, Mode.TEST):
                return AckStatus.REJECTED_NOT_ALLOWED_IN_MODE
            self.mode = Mode.TEST
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.DISABLE:
            if self.mode != Mode.TEST:
                return AckStatus.REJECTED_NOT_ALLOWED_IN_MODE
            self.mode = Mode.NOMINAL
            return AckStatus.ACCEPTED

        return AckStatus.REJECTED_UNKNOWN_CMD

    # ---- fault injection (stdin) -------------------------------------------------

    def inject(self, name):
        with self.lock:
            if name == "sensor":
                self.injected_sensor_timeout = not self.injected_sensor_timeout
                print(f"[sim] sensor timeout injection: {self.injected_sensor_timeout}")
            elif name == "undervoltage":
                self.injected_undervoltage = not self.injected_undervoltage
                print(f"[sim] undervoltage injection: {self.injected_undervoltage}")
            elif name == "drift":
                self.injected_drift = not self.injected_drift
                print(f"[sim] voltage drift injection: {self.injected_drift}")
            elif name == "clear":
                self.injected_sensor_timeout = False
                self.injected_undervoltage = False
                self.injected_drift = False
                print("[sim] all injected conditions cleared (fault flags still need RESET_FAULTS)")
            elif name == "reboot":
                self.mode = Mode.BOOT
                self.boot_time = time.monotonic()
                self.seq_num = 0
                self.fault_flags |= FaultFlag.WATCHDOG_RESET
                print("[sim] simulated watchdog reset -> BOOT")
            else:
                print(f"[sim] unknown fault name: {name!r}")

    def status_line(self):
        with self.lock:
            return (f"mode={Mode(self.mode).name} faults={FaultFlag(self.fault_flags)!r} "
                    f"health={HealthFlag(self.health_flags)!r} rate={self.telemetry_rate_hz}Hz")


def client_handler(sim: Simulator, conn: socket.socket, addr):
    # V0 supports exactly one ground-station link at a time, matching the real
    # spacecraft model. Silently handing telemetry to whichever socket connected
    # most recently (and dropping it to None on that one's disconnect) would pull
    # the rug out from under an existing, still-open client -- reject instead.
    with sim.conn_lock:
        if sim.conn is not None:
            print(f"[sim] rejecting connection from {addr}: link already in use")
            conn.close()
            return
        sim.conn = conn
        sim.last_client_seen = time.monotonic()
    print(f"[sim] ground station connected: {addr}")
    try:
        while True:
            packet, corrupted = read_packet(conn)
            with sim.conn_lock:
                sim.last_client_seen = time.monotonic()
            if corrupted:
                with sim.lock:
                    sim.corrupted_rx_count += 1
                    sim.fault_flags |= FaultFlag.CORRUPTED_PACKET
            if packet is None:
                break
            if not isinstance(packet, CommandPacket):
                continue
            ack = sim.handle_command(packet)
            try:
                conn.sendall(ack.pack())
            except OSError:
                break
    finally:
        with sim.conn_lock:
            if sim.conn is conn:
                sim.conn = None
        conn.close()
        print(f"[sim] ground station disconnected: {addr}")


def telemetry_loop(sim: Simulator, stop_event: threading.Event):
    while not stop_event.is_set():
        packet = sim.tick()
        with sim.conn_lock:
            conn = sim.conn
        if conn is not None:
            try:
                conn.sendall(packet.pack())
            except OSError:
                pass
        time.sleep(1.0 / sim.telemetry_rate_hz)


def stdin_loop(sim: Simulator, stop_event: threading.Event):
    print("[sim] commands: fault sensor|undervoltage|drift|clear, reboot, status, quit")
    for line in sys.stdin:
        cmd = line.strip().lower()
        if not cmd:
            continue
        if cmd in ("quit", "exit"):
            stop_event.set()
            break
        elif cmd == "status":
            print("[sim] " + sim.status_line())
        elif cmd.startswith("fault "):
            sim.inject(cmd.split(" ", 1)[1])
        elif cmd == "reboot":
            sim.inject("reboot")
        else:
            print(f"[sim] unknown command: {cmd!r}")


def main():
    parser = argparse.ArgumentParser(description="V0 CubeSAT flight-computer simulator")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--rate", type=float, default=1.0, help="initial telemetry rate, Hz")
    args = parser.parse_args()

    sim = Simulator(telemetry_rate_hz=args.rate)
    stop_event = threading.Event()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.port))
    server.listen(1)
    server.settimeout(0.5)
    print(f"[sim] listening on 127.0.0.1:{args.port}")

    threading.Thread(target=telemetry_loop, args=(sim, stop_event), daemon=True).start()
    threading.Thread(target=stdin_loop, args=(sim, stop_event), daemon=True).start()

    try:
        while not stop_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=client_handler, args=(sim, conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        print("[sim] shut down")


if __name__ == "__main__":
    main()
