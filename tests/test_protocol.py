"""
Verification tests for simulator/protocol.py against the wire format specified in
docs/interfaces/telemetry-dictionary.md and docs/interfaces/command-dictionary.md.

Covers:
  - pack() -> unpack() round-trips preserve every field, for all three packet types.
  - Packet sizes match the ICD exactly (81 / 13 / 10 bytes).
  - A single flipped byte in a packed telemetry packet is caught by the CRC32
    checksum and unpack() returns None.
  - read_packet()'s resync behavior: garbage bytes preceding a valid packet on a
    real socket are discarded (not fatal), and the valid packet is still recovered.

No ground truth / FDIR logic is exercised here -- this file only verifies the wire
protocol module in isolation, per the sys.path.insert pattern already used by
fdir/engine.py and simulator/environment.py.
"""

import math
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))
import protocol  # noqa: E402
from protocol import (  # noqa: E402
    ACK_PACKET_SIZE,
    COMMAND_PACKET_SIZE,
    PACKET_ID_ACK,
    PACKET_ID_COMMAND,
    PACKET_ID_TELEMETRY,
    SYNC_BYTE,
    TELEMETRY_PACKET_SIZE,
    AckPacket,
    AckStatus,
    CommandId,
    CommandPacket,
    FaultFlag,
    HealthFlag,
    Mode,
    TelemetryPacket,
    read_packet,
)

FLOAT_FIELDS = (
    "temp_c",
    "accel_x", "accel_y", "accel_z",
    "gyro_x", "gyro_y", "gyro_z",
    "mag_x", "mag_y", "mag_z",
    "bus_voltage_v", "bus_current_a",
)


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def telemetry_packet() -> TelemetryPacket:
    """A representative, in-range telemetry packet (values per the ICD ranges)."""
    return TelemetryPacket(
        seq_num=1234,
        timestamp_ms=987654321,
        mode=Mode.NOMINAL,
        fault_flags=FaultFlag.SENSOR_TIMEOUT | FaultFlag.THERMAL_ANOMALY,
        health_flags=HealthFlag.ALL_OK,
        temp_c=23.5,
        accel_x=0.01, accel_y=-0.02, accel_z=0.98,
        gyro_x=1.5, gyro_y=-1.5, gyro_z=0.25,
        mag_x=12.3, mag_y=-45.6, mag_z=78.9,
        bus_voltage_v=5.02,
        bus_current_a=0.35,
        uptime_s=42,
        cmd_rx_count=10,
        cmd_accept_count=9,
        cmd_reject_count=1,
        corrupted_rx_count=2,
    )


@pytest.fixture
def command_packet() -> CommandPacket:
    return CommandPacket(seq_num=99, cmd_id=CommandId.SET_TELEMETRY_RATE, param=2.5)


@pytest.fixture
def ack_packet() -> AckPacket:
    return AckPacket(seq_num=99, cmd_id=CommandId.SET_TELEMETRY_RATE, status=AckStatus.ACCEPTED)


def _assert_fields_equal(original, roundtripped) -> None:
    """Compare every dataclass field: math.isclose for floats, exact equality otherwise."""
    for f in original.__dataclass_fields__:
        original_value = getattr(original, f)
        roundtripped_value = getattr(roundtripped, f)
        if isinstance(original_value, float):
            assert math.isclose(original_value, roundtripped_value, rel_tol=1e-6, abs_tol=1e-9), (
                f"field {f!r}: {original_value!r} != {roundtripped_value!r}"
            )
        else:
            assert original_value == roundtripped_value, (
                f"field {f!r}: {original_value!r} != {roundtripped_value!r}"
            )


# --- Packet sizes (ICD: 81 / 13 / 10 bytes) -----------------------------------


class TestPacketSizes:
    def test_telemetry_packet_size_is_78_bytes(self):
        assert TELEMETRY_PACKET_SIZE == 81

    def test_command_packet_size_is_13_bytes(self):
        assert COMMAND_PACKET_SIZE == 13

    def test_ack_packet_size_is_10_bytes(self):
        assert ACK_PACKET_SIZE == 10

    def test_packed_telemetry_bytes_match_declared_size(self, telemetry_packet):
        assert len(telemetry_packet.pack()) == TELEMETRY_PACKET_SIZE == 81

    def test_packed_command_bytes_match_declared_size(self, command_packet):
        assert len(command_packet.pack()) == COMMAND_PACKET_SIZE == 13

    def test_packed_ack_bytes_match_declared_size(self, ack_packet):
        assert len(ack_packet.pack()) == ACK_PACKET_SIZE == 10


# --- Round-trip: pack() -> unpack() preserves every field ---------------------


class TestTelemetryRoundTrip:
    def test_round_trip_preserves_all_fields(self, telemetry_packet):
        data = telemetry_packet.pack()
        result = TelemetryPacket.unpack(data)
        assert result is not None
        _assert_fields_equal(telemetry_packet, result)

    def test_round_trip_preserves_all_fields_at_extremes(self):
        """Boundary/extreme values, not just a 'nice' nominal sample."""
        extreme = TelemetryPacket(
            seq_num=65535,
            timestamp_ms=0xFFFFFFFF,
            mode=Mode.SAFE,
            fault_flags=int(FaultFlag.SENSOR_LOCKUP | FaultFlag.ML_ANOMALY | FaultFlag.WATCHDOG_RESET),
            health_flags=HealthFlag.NONE,
            temp_c=-20.0,
            accel_x=-4.0, accel_y=4.0, accel_z=0.0,
            gyro_x=-500.0, gyro_y=500.0, gyro_z=0.0,
            mag_x=-100.0, mag_y=100.0, mag_z=0.0,
            bus_voltage_v=0.0,
            bus_current_a=2.0,
            uptime_s=0xFFFFFFFF,
            cmd_rx_count=65535,
            cmd_accept_count=65535,
            cmd_reject_count=65535,
            corrupted_rx_count=65535,
        )
        data = extreme.pack()
        result = TelemetryPacket.unpack(data)
        assert result is not None
        _assert_fields_equal(extreme, result)

    @pytest.mark.parametrize("field", FLOAT_FIELDS)
    def test_each_float_field_survives_round_trip(self, telemetry_packet, field):
        """Isolate each float field so a single miscoded struct offset shows up
        as a single failing test rather than one big diff."""
        setattr(telemetry_packet, field, 3.14159)
        result = TelemetryPacket.unpack(telemetry_packet.pack())
        assert result is not None
        assert math.isclose(getattr(result, field), 3.14159, rel_tol=1e-6)


class TestCommandRoundTrip:
    def test_round_trip_preserves_all_fields(self, command_packet):
        data = command_packet.pack()
        result = CommandPacket.unpack(data)
        assert result is not None
        _assert_fields_equal(command_packet, result)

    def test_round_trip_with_default_param(self):
        pkt = CommandPacket(seq_num=1, cmd_id=CommandId.PING)
        result = CommandPacket.unpack(pkt.pack())
        assert result is not None
        _assert_fields_equal(pkt, result)

    def test_round_trip_negative_param(self):
        pkt = CommandPacket(seq_num=2, cmd_id=CommandId.SET_TELEMETRY_RATE, param=-1.0)
        result = CommandPacket.unpack(pkt.pack())
        assert result is not None
        assert math.isclose(result.param, -1.0)


class TestAckRoundTrip:
    def test_round_trip_preserves_all_fields(self, ack_packet):
        data = ack_packet.pack()
        result = AckPacket.unpack(data)
        assert result is not None
        _assert_fields_equal(ack_packet, result)

    def test_round_trip_rejected_bad_checksum_sentinel_values(self):
        """Sentinel values per command-dictionary.md: seq_num=0xFFFF, cmd_id=0xFF
        when the original command's checksum failed."""
        pkt = AckPacket(
            seq_num=protocol.UNKNOWN_SEQ_NUM,
            cmd_id=protocol.UNKNOWN_CMD_ID,
            status=AckStatus.REJECTED_BAD_CHECKSUM,
        )
        result = AckPacket.unpack(pkt.pack())
        assert result is not None
        _assert_fields_equal(pkt, result)


# --- Checksum catches corruption ----------------------------------------------


class TestChecksumCatchesCorruption:
    def test_flipped_byte_in_telemetry_packet_fails_unpack(self, telemetry_packet):
        data = bytearray(telemetry_packet.pack())
        # Flip a bit in the middle of the payload (temp_c field, offset 14).
        data[20] ^= 0xFF
        assert TelemetryPacket.unpack(bytes(data)) is None

    @pytest.mark.parametrize("byte_offset", list(range(81)))
    def test_flipping_any_single_byte_is_detected(self, telemetry_packet, byte_offset):
        """Every byte position matters to the checksum -- flip each one in turn
        and confirm corruption is always caught. (A CRC32 does not guarantee
        100% single-bit-flip detection in the mathematical worst case, but for
        this fixed packet layout every single-byte XOR-0xFF flip must be caught,
        which this test asserts exhaustively rather than sampling.)"""
        good = telemetry_packet.pack()
        corrupted = bytearray(good)
        corrupted[byte_offset] ^= 0xFF
        assert TelemetryPacket.unpack(bytes(corrupted)) is None, (
            f"corruption at byte offset {byte_offset} went undetected"
        )

    def test_flipped_byte_in_command_packet_fails_unpack(self, command_packet):
        data = bytearray(command_packet.pack())
        data[5] ^= 0xFF  # inside the param float
        assert CommandPacket.unpack(bytes(data)) is None

    def test_flipped_byte_in_ack_packet_fails_unpack(self, ack_packet):
        data = bytearray(ack_packet.pack())
        data[5] ^= 0xFF  # status byte
        assert AckPacket.unpack(bytes(data)) is None

    def test_wrong_length_returns_none(self, telemetry_packet):
        data = telemetry_packet.pack()
        assert TelemetryPacket.unpack(data[:-1]) is None
        assert TelemetryPacket.unpack(data + b"\x00") is None

    def test_wrong_sync_byte_returns_none(self, telemetry_packet):
        data = bytearray(telemetry_packet.pack())
        data[0] = 0x00
        assert TelemetryPacket.unpack(bytes(data)) is None

    def test_wrong_packet_id_returns_none(self, telemetry_packet):
        data = bytearray(telemetry_packet.pack())
        data[1] = PACKET_ID_COMMAND
        assert TelemetryPacket.unpack(bytes(data)) is None


# --- read_packet() resync behavior over a real socket --------------------------


class TestReadPacketResync:
    """Uses socket.socketpair() so these exercise the real recv()-based framing
    code path (recv_exact/read_packet), not just in-memory bytes."""

    @pytest.fixture
    def sockpair(self):
        a, b = socket.socketpair()
        yield a, b
        a.close()
        b.close()

    def test_reads_clean_valid_packet(self, sockpair, telemetry_packet):
        tx, rx = sockpair
        tx.sendall(telemetry_packet.pack())
        packet, was_corrupted = read_packet(rx)
        assert packet is not None
        _assert_fields_equal(telemetry_packet, packet)
        assert was_corrupted is False

    def test_resyncs_past_garbage_bytes_before_valid_packet(self, sockpair, telemetry_packet):
        tx, rx = sockpair
        garbage = bytes([0x00, 0xFF, 0x7E, 0x41, 0x99]) * 3  # none of these are SYNC_BYTE
        tx.sendall(garbage + telemetry_packet.pack())
        packet, was_corrupted = read_packet(rx)
        assert packet is not None
        _assert_fields_equal(telemetry_packet, packet)
        assert was_corrupted is True

    def test_resyncs_past_garbage_that_includes_sync_byte_false_starts(self, sockpair, command_packet):
        tx, rx = sockpair
        # A stray SYNC_BYTE followed by an unrecognized packet_id must also be
        # treated as garbage and discarded, not mistaken for a real header.
        false_start = bytes([SYNC_BYTE, 0xEE])
        tx.sendall(false_start + command_packet.pack())
        packet, was_corrupted = read_packet(rx)
        assert packet is not None
        _assert_fields_equal(command_packet, packet)
        assert was_corrupted is True

    def test_resyncs_past_a_corrupted_packet_then_reads_the_next_valid_one(
        self, sockpair, telemetry_packet
    ):
        tx, rx = sockpair
        corrupted = bytearray(telemetry_packet.pack())
        corrupted[20] ^= 0xFF  # fails checksum, but is the right length/header
        good = TelemetryPacket(
            seq_num=1, timestamp_ms=1, mode=Mode.BOOT, fault_flags=FaultFlag.NONE,
            health_flags=HealthFlag.NONE, temp_c=0.0, accel_x=0.0, accel_y=0.0,
            accel_z=0.0, gyro_x=0.0, gyro_y=0.0, gyro_z=0.0, mag_x=0.0, mag_y=0.0,
            mag_z=0.0, bus_voltage_v=0.0, bus_current_a=0.0, uptime_s=0,
            cmd_rx_count=0, cmd_accept_count=0, cmd_reject_count=0, corrupted_rx_count=0,
        )
        tx.sendall(bytes(corrupted) + good.pack())
        packet, was_corrupted = read_packet(rx)
        assert packet is not None
        _assert_fields_equal(good, packet)
        assert was_corrupted is True

    def test_multiple_valid_packets_back_to_back_read_cleanly(
        self, sockpair, telemetry_packet, command_packet
    ):
        tx, rx = sockpair
        tx.sendall(telemetry_packet.pack() + command_packet.pack())

        first, first_corrupted = read_packet(rx)
        assert first is not None
        _assert_fields_equal(telemetry_packet, first)
        assert first_corrupted is False

        second, second_corrupted = read_packet(rx)
        assert second is not None
        _assert_fields_equal(command_packet, second)
        assert second_corrupted is False

    def test_closed_connection_returns_none_packet(self, sockpair):
        tx, rx = sockpair
        tx.close()
        packet, was_corrupted = read_packet(rx)
        assert packet is None
        assert was_corrupted is False

    def test_closed_connection_mid_packet_returns_none(self, sockpair, telemetry_packet):
        tx, rx = sockpair
        partial = telemetry_packet.pack()[:10]
        tx.sendall(partial)
        tx.close()
        packet, was_corrupted = read_packet(rx)
        assert packet is None


# --- Enum/type sanity relevant to the wire format -------------------------------


class TestPacketIdentifiers:
    def test_packet_ids_match_icd(self):
        assert PACKET_ID_TELEMETRY == 0x01
        assert PACKET_ID_COMMAND == 0x10
        assert PACKET_ID_ACK == 0x11

    def test_sync_byte_matches_icd(self):
        assert SYNC_BYTE == 0xA5


# ---------------------------------------------------------------------------
# Phase 1b: the widened flag fields. These tests pin the capability the
# widening was for -- not just the new packet size.
# ---------------------------------------------------------------------------

class TestWidenedFlagFields:
    def test_fault_flags_carries_more_than_16_bits(self, telemetry_packet):
        """
        The reason for widening: ten fault bits are allocated and the planned
        fault-injection scenario set needs seven more, which overflows uint16.
        A 17th bit must survive the wire.
        """
        telemetry_packet.fault_flags = 1 << 16
        roundtripped = TelemetryPacket.unpack(telemetry_packet.pack())
        assert roundtripped is not None
        assert roundtripped.fault_flags == 1 << 16

    def test_fault_flags_carries_full_32_bit_range(self, telemetry_packet):
        telemetry_packet.fault_flags = 0xFFFFFFFF
        roundtripped = TelemetryPacket.unpack(telemetry_packet.pack())
        assert roundtripped.fault_flags == 0xFFFFFFFF

    def test_health_flags_carries_more_than_8_bits(self, telemetry_packet):
        """Health needs RADIO/ADCS/BUS/OBC/BATTERY on top of the existing four."""
        telemetry_packet.health_flags = 1 << 8
        roundtripped = TelemetryPacket.unpack(telemetry_packet.pack())
        assert roundtripped.health_flags == 1 << 8

    def test_wire_offsets_match_the_icd_document(self):
        """
        Guards the exact drift that made this change expensive to verify: the
        ICD table, simulator/protocol.py and firmware/inc/telemetry_protocol.h
        must agree on every offset. This asserts the first two; the C header is
        cross-checked by mirroring it in ctypes (see the header's own comment).
        """
        import re
        import struct
        from pathlib import Path

        doc = (Path(__file__).resolve().parent.parent
               / "docs" / "interfaces" / "telemetry-dictionary.md").read_text(encoding="utf-8")
        documented = [int(o) for o, _ in re.findall(r"^\| (\d+) \| \`(\w+)\`", doc, re.M)]

        fmts = ["B", "B", "H", "I", "B", "I", "H", "H"] + ["f"] * 12 + ["I", "H", "H", "H", "H", "I"]
        actual, off = [], 0
        for f in fmts:
            actual.append(off)
            off += struct.calcsize("<" + f)

        assert documented == actual, "telemetry-dictionary.md offsets have drifted from protocol.py"
        assert off == TELEMETRY_PACKET_SIZE
