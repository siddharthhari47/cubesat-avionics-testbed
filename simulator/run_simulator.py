"""
V0/V1-prep flight-computer simulator.

Stands in for the STM32 flight computer (docs/architecture/block-diagram.md).
This module is now a thin server/adapter: SpacecraftEnvironment (environment.py)
generates sensor physics and owns fault-injection ground truth; FDIREngine
(fdir/engine.py) owns the BOOT/NOMINAL/SAFE/TEST state machine and all fault
detection; this file just wires them to a TCP socket standing in for a real
UART/radio link, tracks link-level bookkeeping (sequence numbers, command
counters), and dispatches commands that don't touch FDIR state (PING,
GET_STATUS, SET_TELEMETRY_RATE, REQUEST_LOG).

Fault injection happens at THIS process's own stdin, not over the ground-station
link -- that link is the real spacecraft interface; stdin here plays the role of
a test engineer physically doing something to the hardware.

Run: python simulator/run_simulator.py [--seed N]
Then, at this process's prompt:
  fault sensor_timeout | sensor_lockup | undervoltage | gradual_drift | thermal | clear
  reboot | status | quit
"""

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fdir.engine import FDIREngine, MLAdvisory  # noqa: E402


def _load_ml_detector():
    """
    Optional ML #1. Returns (extractor, model, threshold) or None.

    Deliberately optional and deliberately failing soft: the deterministic FDIR
    layer is the safety-critical path and must run whether or not a model is
    present or loadable. An advisory detector that can prevent the spacecraft
    from booting would be worse than no advisory detector.
    """
    try:
        import joblib
        from ml.features import feature_columns
        from ml.streaming import StreamingFeatureExtractor
        model_path = Path(__file__).resolve().parent.parent / "ml" / "models" / "isolation_forest_v1.joblib"
        if not model_path.exists():
            return None
        model = joblib.load(model_path)

        # Verify FEATURE ORDER once, here, instead of never.
        #
        # The model was fitted on a named DataFrame; the streaming path feeds it
        # a bare ndarray, so sklearn emitted "X does not have valid feature
        # names" on every single tick. That warning is not cosmetic -- it is
        # sklearn saying it cannot check that column 7 is still the column it
        # trained on. Silencing it would have thrown away the only signal that
        # a reordered feature list had silently invalidated every score.
        #
        # So do the check the warning was asking for, once at load, and fail
        # soft if it does not hold. Same property the exported C header depends
        # on (see ml/export_embedded.py's ordered feature comment).
        expected = list(getattr(model, "feature_names_in_", []))
        if expected and expected != list(feature_columns()):
            print("[sim] ML #1 REJECTED: feature order does not match the trained "
                  "model. Scores would be meaningless. Running deterministic "
                  "FDIR only.")
            return None

        # Order is now verified, so the per-tick warning is pure noise -- and at
        # 10 Hz it buried every real [sim] line in the log. Suppress only this
        # exact message, never warnings broadly.
        import warnings
        warnings.filterwarnings(
            "ignore", message="X does not have valid feature names",
            category=UserWarning)
        return StreamingFeatureExtractor(), model
    except Exception as exc:      # noqa: BLE001 - advisory path, never fatal
        print(f"[sim] ML #1 unavailable ({exc}); running deterministic FDIR only")
        return None

from environment import SpacecraftEnvironment, FAULT_TYPES  # noqa: E402
from protocol import (  # noqa: E402
    AckPacket, AckStatus, CommandId, CommandPacket, FaultFlag, HealthFlag, Mode,
    TelemetryPacket, read_packet,
)


class Simulator:
    def __init__(self, telemetry_rate_hz: float, seed=None, use_ml: bool = False):
        self.lock = threading.Lock()
        self.env = SpacecraftEnvironment(seed=seed)
        self.engine = FDIREngine()
        self._ml = _load_ml_detector() if use_ml else None
        if self._ml is not None:
            print("[sim] ML #1 loaded (advisory only -- no SAFE or recovery authority)")
        self.boot_time = time.monotonic()      # for the wire timestamp_ms field only
        self.process_start = time.monotonic()
        self.seq_num = 0
        self.telemetry_rate_hz = telemetry_rate_hz
        self.cmd_rx_count = 0
        self.cmd_accept_count = 0
        self.cmd_reject_count = 0
        self.corrupted_rx_count = 0

        self.conn_lock = threading.Lock()
        self.conn = None
        self.last_client_seen = None

    # ---- tick: environment -> FDIR -> wire packet -------------------------------------------------

    def tick(self) -> TelemetryPacket:
        with self.lock:
            now = time.monotonic()
            sample, _truth = self.env.step(1.0 / self.telemetry_rate_hz)
            self.engine.tick(sample, now, ml_advisory=self._ml_advisory(sample))
            self._update_comms_loss(now)
            # Cheap in V0; on hardware this becomes a wear-aware write of a
            # fixed-size record to backup SRAM, not a per-tick flash cycle.
            self._persist()
            return self._build_packet(sample)

    def _ml_advisory(self, sample):
        """
        Run ML #1 over the streaming feature extractor.

        Returns None while the rolling window is still filling. That is not a
        convenience: on the first samples after any reset every rolling standard
        deviation is 0.0, which is exactly the frozen-sensor signature the model
        detects best -- so emitting a partial-window vector would manufacture
        the strongest anomaly in the model's repertoire at every boot.
        """
        if self._ml is None:
            return None
        extractor, model = self._ml
        vector = extractor.push(sample)
        if vector is None:
            return None
        import numpy as np
        x = np.asarray([vector])
        score = float(model.decision_function(x)[0])
        return MLAdvisory(score=score, is_anomalous=bool(model.predict(x)[0] == -1))

    def _update_comms_loss(self, now):
        # The transport OBSERVES the link and reports it; the engine DECIDES
        # what that means, applies the BOOT guard, and owns the timeout from
        # fdir/config.py. This used to write engine.fault_flags directly with a
        # hardcoded 5.0 while config's COMMS_LOSS_TIMEOUT_S went unused (D6).
        # J1: report EVIDENCE, not a verdict. `self.conn is not None` says a
        # socket object exists, which is not the same as anything being on the
        # other end -- on a half-open link recv blocks forever and this stayed
        # True indefinitely. The engine now requires BOTH a transport link and
        # something heard within the timeout, so last_seen is load-bearing
        # rather than decorative.
        with self.conn_lock:
            link_established = self.conn is not None
            last_seen = self.last_client_seen
        seconds_since_contact = None if last_seen is None else now - last_seen
        self.engine.note_link_state(now, link_established, seconds_since_contact)

    # ---- non-volatile state -------------------------------------------------
    # Round 4: export/import_reference_state and export/import_capability_state
    # existed and were tested, and NOTHING CALLED THEM outside tests. The R7
    # reference and the R8 capability were therefore persisted in the test suite
    # only -- on the real reboot path both were silently lost, which is exactly
    # the failure each was written to prevent.

    def _persist(self) -> None:
        """Snapshot the state that must survive a reset. Real firmware writes
        backup SRAM here; V0 keeps it in memory across the OBC reset."""
        self._nvm = {
            "recovery": self.engine.export_recovery_state(),
            "reference": self.engine.export_reference_state(),
            "capability": self.engine.export_capability_state(),
        }

    def _restore(self, now: float) -> None:
        nvm = getattr(self, "_nvm", None) or {}
        self.engine.import_recovery_state(nvm.get("recovery"), now)
        self.engine.import_reference_state(nvm.get("reference"), now)
        self.engine.import_capability_state(nvm.get("capability"), now)

    def note_link_failure(self, conn) -> None:
        """
        A send failed, so the link is gone. Tear it down.

        This used to be `except OSError: pass` in telemetry_loop -- discarding
        the single strongest piece of evidence available that contact is lost,
        and leaving self.conn set so the spacecraft went on believing it had a
        ground station. Guarded by identity the same way client_handler's
        teardown is, so a stale failure cannot close a newer client's link.
        """
        with self.conn_lock:
            if self.conn is conn:
                self.conn = None
                print("[sim] telemetry send failed -- treating ground contact as lost")

    def _build_packet(self, sample) -> TelemetryPacket:
        self.seq_num = (self.seq_num + 1) % 65536
        timestamp_ms = int((time.monotonic() - self.boot_time) * 1000) % (2**32)
        uptime_s = int(time.monotonic() - self.process_start)
        return TelemetryPacket(
            seq_num=self.seq_num, timestamp_ms=timestamp_ms, mode=int(self.engine.mode),
            fault_flags=int(self.engine.fault_flags), health_flags=int(self.engine.health_flags),
            temp_c=sample.temp_c, accel_x=sample.accel_x, accel_y=sample.accel_y, accel_z=sample.accel_z,
            gyro_x=sample.gyro_x, gyro_y=sample.gyro_y, gyro_z=sample.gyro_z,
            mag_x=sample.mag_x, mag_y=sample.mag_y, mag_z=sample.mag_z,
            bus_voltage_v=sample.bus_voltage_v, bus_current_a=sample.bus_current_a,
            uptime_s=uptime_s, cmd_rx_count=self.cmd_rx_count,
            cmd_accept_count=self.cmd_accept_count, cmd_reject_count=self.cmd_reject_count,
            corrupted_rx_count=self.corrupted_rx_count,
        )

    # ---- command handling -------------------------------------------------

    def handle_command(self, cmd: CommandPacket) -> AckPacket:
        with self.lock:
            self.cmd_rx_count += 1
            now = time.monotonic()
            status = self._apply_command(cmd, now)
            if status == AckStatus.ACCEPTED:
                self.cmd_accept_count += 1
            else:
                self.cmd_reject_count += 1
            return AckPacket(seq_num=cmd.seq_num, cmd_id=cmd.cmd_id, status=status)

    def _apply_command(self, cmd: CommandPacket, now: float) -> int:
        if cmd.cmd_id in (CommandId.PING, CommandId.GET_STATUS):
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.SET_TELEMETRY_RATE:
            if 0.5 <= cmd.param <= 10.0:
                self.telemetry_rate_hz = cmd.param
                return AckStatus.ACCEPTED
            return AckStatus.REJECTED_INVALID_PARAM

        if cmd.cmd_id == CommandId.ENTER_SAFE_MODE:
            self.engine.enter_safe_mode(now)
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.EXIT_SAFE_MODE:
            accepted = self.engine.exit_safe_mode(now)
            return AckStatus.ACCEPTED if accepted else AckStatus.REJECTED_SAFE_MODE_FAULT_ACTIVE

        if cmd.cmd_id == CommandId.RESET_FAULTS:
            cleared, still_latched = self.engine.reset_faults(now)
            # Report what actually happened. Previously this always ACKed
            # ACCEPTED even when it cleared nothing, so a refused reset was
            # indistinguishable from a successful one (D4).
            if still_latched and not cleared:
                return AckStatus.REJECTED_CONDITION_STILL_ACTIVE
            return AckStatus.ACCEPTED

        if cmd.cmd_id == CommandId.REQUEST_LOG:
            return AckStatus.REJECTED_NOT_IMPLEMENTED  # no SD logging until V2

        if cmd.cmd_id == CommandId.ENABLE:
            return AckStatus.ACCEPTED if self.engine.enter_test_mode() else AckStatus.REJECTED_NOT_ALLOWED_IN_MODE
        elif cmd.cmd_id == CommandId.RECOMMISSION_REFERENCE:
            # The escape from a bad commissioning reference. Without this on the
            # command path, a reference captured wrongly degrades the vehicle
            # permanently and the ground can do nothing about it.
            self.engine.recommission_reference(now)
            self._persist()
            return AckStatus.ACCEPTED
        elif cmd.cmd_id == CommandId.RESTORE_CAPABILITY:
            ok = self.engine.restore_capability(now)
            self._persist()
            return AckStatus.ACCEPTED if ok else AckStatus.REJECTED_CONDITION_STILL_ACTIVE

        if cmd.cmd_id == CommandId.DISABLE:
            return AckStatus.ACCEPTED if self.engine.exit_test_mode() else AckStatus.REJECTED_NOT_ALLOWED_IN_MODE

        return AckStatus.REJECTED_UNKNOWN_CMD

    # ---- fault injection (stdin) -------------------------------------------------

    def inject(self, name: str) -> None:
        with self.lock:
            if name == "clear":
                self.env.clear_all()
                print("[sim] all injected conditions cleared (fault flags still need RESET_FAULTS)")
            elif name in FAULT_TYPES:
                self.env.inject(name)
                print(f"[sim] injected: {name}")
            elif name.startswith("clear "):
                target = name.split(" ", 1)[1]
                self.env.clear(target)
                print(f"[sim] cleared: {target}")
            else:
                print(f"[sim] unknown fault name: {name!r} (options: {', '.join(FAULT_TYPES)})")

    def reboot(self) -> None:
        with self.lock:
            now = time.monotonic()
            self.boot_time = now
            self.seq_num = 0
            self._persist()
            self.engine.watchdog_reset(now)
            self._restore(now)
            print("[sim] simulated watchdog reset -> BOOT")

    def status_line(self) -> str:
        with self.lock:
            e = self.engine
            return (f"mode={Mode(e.mode).name} faults={FaultFlag(e.fault_flags)!r} "
                    f"health={HealthFlag(e.health_flags)!r} rate={self.telemetry_rate_hz}Hz "
                    f"env_active={self.env.active_faults()}")


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
                    sim.engine.note_corrupted_packet(time.monotonic())
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
                sim.note_link_failure(conn)
        time.sleep(1.0 / sim.telemetry_rate_hz)


def stdin_loop(sim: Simulator, stop_event: threading.Event):
    print("[sim] commands: fault <" + "|".join(FAULT_TYPES) + "|clear>, reboot, status, quit")
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
            sim.reboot()
        else:
            print(f"[sim] unknown command: {cmd!r}")


def main():
    parser = argparse.ArgumentParser(description="CubeSAT flight-computer simulator")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--rate", type=float, default=1.0, help="initial telemetry rate, Hz")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    parser.add_argument("--ml", action="store_true",
                        help="enable ML #1 advisory detection (advisory only; "
                             "it can raise a flag but never command SAFE or a recovery action)")
    args = parser.parse_args()

    sim = Simulator(telemetry_rate_hz=args.rate, seed=args.seed, use_ml=args.ml)
    stop_event = threading.Event()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.port))
    server.listen(1)
    server.settimeout(0.5)
    print(f"[sim] listening on 127.0.0.1:{args.port}" + (f" (seed={args.seed})" if args.seed is not None else ""))

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
