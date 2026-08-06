"""
Background link to the flight computer (V0: simulator/run_simulator.py over TCP).

Runs its own reader thread so the socket is never blocked on Streamlit's rerun
cycle -- the UI just reads a snapshot of whatever's arrived so far. protocol.py
is imported from simulator/ since it's the shared ICD implementation and this is
the only thing ground-station/ needs from that package.
"""

import csv
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))
import protocol as proto  # noqa: E402


class GroundLink:
    def __init__(self, host, port, history_len=1800, csv_dir=None):
        self.host = host
        self.port = port
        self.lock = threading.Lock()

        self.latest = None
        self.history = deque(maxlen=history_len)
        self.command_log = deque(maxlen=200)
        self.connected = False
        self.connect_error = None

        self._sock = None
        self._pending = {}
        self._next_seq = 1
        self._stop = threading.Event()

        self._csv_file = None
        self._csv_writer = None
        self.csv_path = None
        if csv_dir is not None:
            self._start_csv_log(csv_dir)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- connection lifecycle -------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
            except OSError as e:
                with self.lock:
                    self.connected = False
                    self.connect_error = str(e)
                time.sleep(1.0)
                continue
            sock.settimeout(None)  # the 5s connect timeout must not linger onto recv()
            with self.lock:
                self._sock = sock
                self.connected = True
                self.connect_error = None
            try:
                self._read_loop(sock)
            except OSError as e:
                # A dropped link must not silently kill this background thread --
                # that's exactly the kind of failure a ground station has to survive.
                with self.lock:
                    self.connect_error = str(e)
            with self.lock:
                self.connected = False
                self._sock = None
            time.sleep(1.0)

    def _read_loop(self, sock):
        while not self._stop.is_set():
            packet, _corrupted = proto.read_packet(sock)
            if packet is None:
                return  # connection closed
            if isinstance(packet, proto.TelemetryPacket):
                with self.lock:
                    self.latest = packet
                    self.history.append(packet)
                self._log_csv(packet)
            elif isinstance(packet, proto.AckPacket):
                with self.lock:
                    pending = self._pending.pop(packet.seq_num, None)
                    self.command_log.appendleft({
                        "time": time.strftime("%H:%M:%S"),
                        "command": pending["name"] if pending else f"seq {packet.seq_num} (unmatched)",
                        "param": pending["param"] if pending else None,
                        "status": proto.AckStatus(packet.status).name,
                    })

    def close(self):
        self._stop.set()
        with self.lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
        if self._csv_file is not None:
            self._csv_file.close()

    # ---- commands -------------------------------------------------

    def send_command(self, cmd_id, param=0.0, name=None):
        with self.lock:
            if not self.connected or self._sock is None:
                return False
            seq = self._next_seq
            self._next_seq = (self._next_seq + 1) % 65536
            self._pending[seq] = {"name": name or proto.CommandId(cmd_id).name, "param": param}
            sock = self._sock
        cmd = proto.CommandPacket(seq_num=seq, cmd_id=cmd_id, param=param)
        try:
            sock.sendall(cmd.pack())
            return True
        except OSError:
            return False

    # ---- CSV logging (GS-003) -------------------------------------------------

    def _start_csv_log(self, csv_dir):
        csv_dir = Path(csv_dir)
        csv_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.csv_path = csv_dir / f"telemetry_{stamp}.csv"
        self._csv_file = open(self.csv_path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        header = ["recv_time"] + [f.name for f in fields(proto.TelemetryPacket)]
        self._csv_writer.writerow(header)
        self._csv_file.flush()

    def _log_csv(self, packet):
        if self._csv_writer is None:
            return
        row = [time.strftime("%Y-%m-%d %H:%M:%S")] + [
            getattr(packet, f.name) for f in fields(proto.TelemetryPacket)
        ]
        with self.lock:
            self._csv_writer.writerow(row)
            self._csv_file.flush()

    # ---- read access -------------------------------------------------

    def snapshot(self):
        """Thread-safe copy of current state for the UI to render."""
        with self.lock:
            return {
                "latest": self.latest,
                "history": list(self.history),
                "command_log": list(self.command_log),
                "connected": self.connected,
                "connect_error": self.connect_error,
            }
