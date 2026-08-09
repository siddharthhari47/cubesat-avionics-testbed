"""
Wire protocol implementation matching docs/interfaces/telemetry-dictionary.md and
docs/interfaces/command-dictionary.md exactly. This is the single source of truth
for packet layout on the Python side; both simulator/ and ground-station/ import
from here so the two can never silently drift apart on packet format.
"""

import struct
import zlib
from dataclasses import dataclass, fields
from enum import IntEnum, IntFlag

SYNC_BYTE = 0xA5

PACKET_ID_TELEMETRY = 0x01
PACKET_ID_COMMAND = 0x10
PACKET_ID_ACK = 0x11


class Mode(IntEnum):
    BOOT = 0
    NOMINAL = 1
    SAFE = 2
    TEST = 3


class FaultFlag(IntFlag):
    NONE = 0
    SENSOR_TIMEOUT = 1 << 0
    UNDERVOLTAGE_WARNING = 1 << 1
    UNDERVOLTAGE_CRITICAL = 1 << 2
    COMMS_LOSS = 1 << 3
    CORRUPTED_PACKET = 1 << 4
    ADAPTIVE_ANOMALY = 1 << 5
    ML_ANOMALY = 1 << 6
    WATCHDOG_RESET = 1 << 7
    THERMAL_ANOMALY = 1 << 8
    SENSOR_LOCKUP = 1 << 9


class HealthFlag(IntFlag):
    NONE = 0
    TEMP_OK = 1 << 0
    IMU_OK = 1 << 1
    MAG_OK = 1 << 2
    POWER_OK = 1 << 3
    ALL_OK = TEMP_OK | IMU_OK | MAG_OK | POWER_OK


class CommandId(IntEnum):
    PING = 0x01
    GET_STATUS = 0x02
    SET_TELEMETRY_RATE = 0x03
    ENTER_SAFE_MODE = 0x04
    EXIT_SAFE_MODE = 0x05
    RESET_FAULTS = 0x06
    REQUEST_LOG = 0x07
    ENABLE = 0x08
    DISABLE = 0x09


class AckStatus(IntEnum):
    ACCEPTED = 0x00
    REJECTED_UNKNOWN_CMD = 0x01
    REJECTED_BAD_CHECKSUM = 0x02
    REJECTED_INVALID_PARAM = 0x03
    REJECTED_SAFE_MODE_FAULT_ACTIVE = 0x04
    REJECTED_NOT_ALLOWED_IN_MODE = 0x05
    REJECTED_NOT_IMPLEMENTED = 0x06


# --- Telemetry packet ---------------------------------------------------

_TM_FIELD_FORMAT = "<BBHIBHBH" + "f" * 12 + "I" + "HHHH"
_TM_STRUCT = struct.Struct(_TM_FIELD_FORMAT)  # everything except the trailing checksum
_TM_CHECKSUM_STRUCT = struct.Struct("<I")
TELEMETRY_PACKET_SIZE = _TM_STRUCT.size + _TM_CHECKSUM_STRUCT.size
assert TELEMETRY_PACKET_SIZE == 78, TELEMETRY_PACKET_SIZE


@dataclass
class TelemetryPacket:
    seq_num: int
    timestamp_ms: int
    mode: int
    fault_flags: int
    health_flags: int
    temp_c: float
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    mag_x: float
    mag_y: float
    mag_z: float
    bus_voltage_v: float
    bus_current_a: float
    uptime_s: int
    cmd_rx_count: int
    cmd_accept_count: int
    cmd_reject_count: int
    corrupted_rx_count: int

    def pack(self) -> bytes:
        payload_length = _TM_STRUCT.size - 14  # bytes from offset 14 (temp_c) onward, minus checksum
        body = _TM_STRUCT.pack(
            SYNC_BYTE,
            PACKET_ID_TELEMETRY,
            self.seq_num,
            self.timestamp_ms,
            self.mode,
            self.fault_flags,
            self.health_flags,
            payload_length,
            self.temp_c,
            self.accel_x,
            self.accel_y,
            self.accel_z,
            self.gyro_x,
            self.gyro_y,
            self.gyro_z,
            self.mag_x,
            self.mag_y,
            self.mag_z,
            self.bus_voltage_v,
            self.bus_current_a,
            self.uptime_s,
            self.cmd_rx_count,
            self.cmd_accept_count,
            self.cmd_reject_count,
            self.corrupted_rx_count,
        )
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return body + _TM_CHECKSUM_STRUCT.pack(checksum)

    @classmethod
    def unpack(cls, data: bytes) -> "TelemetryPacket | None":
        if len(data) != TELEMETRY_PACKET_SIZE:
            return None
        body, checksum_bytes = data[:_TM_STRUCT.size], data[_TM_STRUCT.size:]
        (checksum,) = _TM_CHECKSUM_STRUCT.unpack(checksum_bytes)
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            return None
        unpacked = _TM_STRUCT.unpack(body)
        (
            sync, packet_id, seq_num, timestamp_ms, mode, fault_flags, health_flags,
            _payload_length, temp_c, accel_x, accel_y, accel_z, gyro_x, gyro_y,
            gyro_z, mag_x, mag_y, mag_z, bus_voltage_v, bus_current_a, uptime_s,
            cmd_rx_count, cmd_accept_count, cmd_reject_count, corrupted_rx_count,
        ) = unpacked
        if sync != SYNC_BYTE or packet_id != PACKET_ID_TELEMETRY:
            return None
        return cls(
            seq_num=seq_num, timestamp_ms=timestamp_ms, mode=mode,
            fault_flags=fault_flags, health_flags=health_flags, temp_c=temp_c,
            accel_x=accel_x, accel_y=accel_y, accel_z=accel_z, gyro_x=gyro_x,
            gyro_y=gyro_y, gyro_z=gyro_z, mag_x=mag_x, mag_y=mag_y, mag_z=mag_z,
            bus_voltage_v=bus_voltage_v, bus_current_a=bus_current_a,
            uptime_s=uptime_s, cmd_rx_count=cmd_rx_count,
            cmd_accept_count=cmd_accept_count, cmd_reject_count=cmd_reject_count,
            corrupted_rx_count=corrupted_rx_count,
        )


# --- Command packet -------------------------------------------------------

_CMD_STRUCT = struct.Struct("<BBHBf")
_CMD_CHECKSUM_STRUCT = struct.Struct("<I")
COMMAND_PACKET_SIZE = _CMD_STRUCT.size + _CMD_CHECKSUM_STRUCT.size
assert COMMAND_PACKET_SIZE == 13, COMMAND_PACKET_SIZE


@dataclass
class CommandPacket:
    seq_num: int
    cmd_id: int
    param: float = 0.0

    def pack(self) -> bytes:
        body = _CMD_STRUCT.pack(SYNC_BYTE, PACKET_ID_COMMAND, self.seq_num, self.cmd_id, self.param)
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return body + _CMD_CHECKSUM_STRUCT.pack(checksum)

    @classmethod
    def unpack(cls, data: bytes) -> "CommandPacket | None":
        """Returns None on any framing/checksum failure — caller counts this as corrupted (COM-004)."""
        if len(data) != COMMAND_PACKET_SIZE:
            return None
        body, checksum_bytes = data[:_CMD_STRUCT.size], data[_CMD_STRUCT.size:]
        (checksum,) = _CMD_CHECKSUM_STRUCT.unpack(checksum_bytes)
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            return None
        sync, packet_id, seq_num, cmd_id, param = _CMD_STRUCT.unpack(body)
        if sync != SYNC_BYTE or packet_id != PACKET_ID_COMMAND:
            return None
        return cls(seq_num=seq_num, cmd_id=cmd_id, param=param)


# --- Ack packet -------------------------------------------------------------

_ACK_STRUCT = struct.Struct("<BBHBB")
_ACK_CHECKSUM_STRUCT = struct.Struct("<I")
ACK_PACKET_SIZE = _ACK_STRUCT.size + _ACK_CHECKSUM_STRUCT.size
assert ACK_PACKET_SIZE == 10, ACK_PACKET_SIZE

UNKNOWN_SEQ_NUM = 0xFFFF
UNKNOWN_CMD_ID = 0xFF


@dataclass
class AckPacket:
    seq_num: int
    cmd_id: int
    status: int

    def pack(self) -> bytes:
        body = _ACK_STRUCT.pack(SYNC_BYTE, PACKET_ID_ACK, self.seq_num, self.cmd_id, self.status)
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        return body + _ACK_CHECKSUM_STRUCT.pack(checksum)

    @classmethod
    def unpack(cls, data: bytes) -> "AckPacket | None":
        if len(data) != ACK_PACKET_SIZE:
            return None
        body, checksum_bytes = data[:_ACK_STRUCT.size], data[_ACK_STRUCT.size:]
        (checksum,) = _ACK_CHECKSUM_STRUCT.unpack(checksum_bytes)
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            return None
        sync, packet_id, seq_num, cmd_id, status = _ACK_STRUCT.unpack(body)
        if sync != SYNC_BYTE or packet_id != PACKET_ID_ACK:
            return None
        return cls(seq_num=seq_num, cmd_id=cmd_id, status=status)


# --- Stream framing ----------------------------------------------------------

def recv_exact(sock, n: int) -> bytes | None:
    """Read exactly n bytes from a socket, or None if the connection closed first."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_packet(sock):
    """
    Scan a socket for the next valid packet. Bytes that don't start a recognizable
    packet are discarded one at a time (resync) rather than treated as fatal --
    the same tolerance a real serial link needs against line noise.

    Returns (packet, was_corrupted) where packet is a TelemetryPacket/CommandPacket/
    AckPacket or None (connection closed), and was_corrupted is True if bytes had
    to be discarded or a checksum failed before a valid packet was found.
    """
    corrupted = False
    while True:
        sync = recv_exact(sock, 1)
        if sync is None:
            return None, corrupted
        if sync[0] != SYNC_BYTE:
            corrupted = True
            continue

        packet_id_byte = recv_exact(sock, 1)
        if packet_id_byte is None:
            return None, corrupted
        packet_id = packet_id_byte[0]

        size_by_id = {
            PACKET_ID_TELEMETRY: (TELEMETRY_PACKET_SIZE, TelemetryPacket),
            PACKET_ID_COMMAND: (COMMAND_PACKET_SIZE, CommandPacket),
            PACKET_ID_ACK: (ACK_PACKET_SIZE, AckPacket),
        }
        if packet_id not in size_by_id:
            corrupted = True
            continue

        size, cls = size_by_id[packet_id]
        rest = recv_exact(sock, size - 2)
        if rest is None:
            return None, corrupted
        data = bytes([sync[0], packet_id]) + rest
        packet = cls.unpack(data)
        if packet is None:
            corrupted = True
            continue
        return packet, corrupted
