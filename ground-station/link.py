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


# The spacecraft can only conclude "I still have ground contact" from something
# it actually HEARS. Telemetry is downlink-only, and the operator sends commands
# by hand, so without this there is no periodic uplink at all: the flight
# computer's seconds_since_contact grew forever while a perfectly good link sat
# idle. Must stay comfortably under fdir/config.py's COMMS_LOSS_TIMEOUT_S (5 s),
# or a healthy idle link latches COMMS_LOSS.
HEARTBEAT_INTERVAL_S = 1.0


def _status_name(value: int) -> str:
    """
    Render an ack status without trusting the wire to hold a value we know.

    G1: `proto.AckStatus(value).name` raises ValueError on anything undefined,
    and that exception killed the reader thread outright. Displaying the raw
    byte is strictly better than dying, and during V1 bring-up -- when firmware
    will legitimately emit codes this ground station has not learned yet -- it
    is also the more useful thing to show an operator.
    """
    try:
        return proto.AckStatus(value).name
    except ValueError:
        return f"UNKNOWN_STATUS(0x{value:02X})"


class GroundLink:
    def __init__(self, host, port, history_len=1800, csv_dir=None,
                 heartbeat=True):
        self.host = host
        self.port = port
        self.lock = threading.Lock()

        self.latest = None
        self.history = deque(maxlen=history_len)
        self.command_log = deque(maxlen=200)
        self.connected = False
        self.connect_error = None

        # G3: read_packet() reports whether it had to discard bytes or saw a
        # failed checksum, and this class used to throw that away entirely
        # (`packet, _corrupted = ...`). Packet loss vs. range is one of the five
        # numbers this project exists to produce, and the one place that can
        # measure it was discarding the evidence.
        self.corrupted_rx_count = 0
        self.decode_error_count = 0
        self.last_decode_error = None

        self._sock = None
        self._pending = {}
        self._heartbeat_seqs = set()
        self._next_seq = 1
        self._stop = threading.Event()

        self._csv_file = None
        self._csv_writer = None
        self.csv_path = None
        if csv_dir is not None:
            self._start_csv_log(csv_dir)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._heartbeat_thread = None
        if heartbeat:
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

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
            except Exception as e:      # noqa: BLE001 - see below
                # G1: this caught only OSError, three lines under a comment
                # promising a bad link could not kill this thread. It could:
                # AckStatus(packet.status) raises ValueError on any status byte
                # the ground station does not recognise, that escaped the
                # thread entirely, and the dashboard went on reporting
                # `connected` while never receiving another packet again.
                # Measured: one 0x08 status byte, then five good telemetry
                # packets, zero of them received.
                #
                # One malformed packet must cost a reconnect, never the thread.
                # A firmware build emitting a status code this ground station
                # has not been taught yet is a NORMAL event during V1 bring-up.
                with self.lock:
                    self.connect_error = str(e)
                    self.decode_error_count += 1
                    self.last_decode_error = f"{type(e).__name__}: {e}"
                try:
                    sock.close()
                except OSError:
                    pass
            with self.lock:
                self.connected = False
                self._sock = None
            time.sleep(1.0)

    def _read_loop(self, sock):
        while not self._stop.is_set():
            packet, corrupted = proto.read_packet(sock)
            if corrupted:
                with self.lock:
                    self.corrupted_rx_count += 1
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
                    was_heartbeat = packet.seq_num in self._heartbeat_seqs
                    self._heartbeat_seqs.discard(packet.seq_num)
                    if was_heartbeat:
                        continue      # keepalive traffic is not operator activity
                    self.command_log.appendleft({
                        "time": time.strftime("%H:%M:%S"),
                        "command": pending["name"] if pending else f"seq {packet.seq_num} (unmatched)",
                        "param": pending["param"] if pending else None,
                        "status": _status_name(packet.status),
                    })

    def _heartbeat_loop(self):
        """
        Periodic uplink so the spacecraft can tell a live link from a dead one.

        Without this the flight computer had no evidence of contact to reason
        about at all -- see J1 in the safety review. PING is used because it is
        already in the command dictionary and has no side effects.
        """
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            with self.lock:
                if not self.connected or self._sock is None:
                    continue
            self.send_command(proto.CommandId.PING, _heartbeat=True)

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

    def send_command(self, cmd_id, param=0.0, name=None, _heartbeat=False):
        with self.lock:
            if not self.connected or self._sock is None:
                return False
            seq = self._next_seq
            self._next_seq = (self._next_seq + 1) % 65536
            # An ack that never arrives used to leave its entry here forever.
            # At one heartbeat a second that leaks steadily, so bound it: the
            # oldest outstanding entries are the ones least likely to be
            # answered.
            while len(self._pending) >= 256:
                self._pending.pop(next(iter(self._pending)), None)
            self._pending[seq] = {"name": name or proto.CommandId(cmd_id).name, "param": param}
            if _heartbeat:
                self._heartbeat_seqs.add(seq)
                if len(self._heartbeat_seqs) > 256:
                    self._heartbeat_seqs.clear()
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
                "corrupted_rx_count": self.corrupted_rx_count,
                "decode_error_count": self.decode_error_count,
                "last_decode_error": self.last_decode_error,
            }
