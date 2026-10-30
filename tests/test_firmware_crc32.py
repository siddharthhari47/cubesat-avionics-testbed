"""
Correctness check for the CRC32 algorithm in firmware/src/telemetry_protocol.c.

What this DOES prove: the bitwise CRC-32/ISO-HDLC algorithm used there --
poly 0xEDB88320 reflected, init 0xFFFFFFFF, final XOR 0xFFFFFFFF, no lookup
table -- reproduces zlib.crc32() bit-for-bit. `_bitwise_crc32` below is a
line-for-line transliteration of `tm_crc32()` in that C file; keep them in
sync by hand if either changes.

What this does NOT prove: that the C file compiles, or behaves identically
once arm-none-eabi-gcc has optimized it. No C toolchain was available in this
environment to compile and run the actual C source. The real, load-bearing
check is on target: build the fixed test vector in tests/test_scenarios.py-
style form (see the CubeIDE bring-up notes), transmit it over USART2, and
diff the received bytes against TelemetryPacket(...).pack() for the same
field values. This test only rules out the algorithm being wrong before that
much slower on-hardware round trip.
"""

import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))
import protocol as proto  # noqa: E402


def _bitwise_crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc = crc >> 1
    return crc ^ 0xFFFFFFFF


def test_bitwise_algorithm_matches_zlib_on_known_vectors():
    for data in (b"", b"\x00", b"\xff", b"A", b"123456789", b"\xa5\x01\x00\x00"):
        assert _bitwise_crc32(data) == zlib.crc32(data), data


def test_bitwise_algorithm_matches_zlib_on_random_lengths():
    import random
    rng = random.Random(0)
    for length in (0, 1, 4, 13, 60, 77, 81, 255, 1000):
        data = bytes(rng.randrange(256) for _ in range(length))
        assert _bitwise_crc32(data) == zlib.crc32(data), (length, data.hex())


def test_bitwise_algorithm_matches_zlib_on_a_real_telemetry_packet_body():
    """The exact bytes tm_finalize_telemetry() will checksum on the MCU."""
    pkt = proto.TelemetryPacket(
        seq_num=1234, timestamp_ms=567890, mode=1, fault_flags=0x00000205,
        health_flags=0x000F, temp_c=23.5, accel_x=0.01, accel_y=-0.02, accel_z=0.98,
        gyro_x=1.5, gyro_y=-1.5, gyro_z=0.0, mag_x=30.2, mag_y=-15.7, mag_z=45.0,
        bus_voltage_v=7.42, bus_current_a=0.315, uptime_s=42, cmd_rx_count=10,
        cmd_accept_count=9, cmd_reject_count=1, corrupted_rx_count=0,
    )
    full = pkt.pack()
    body = full[:-4]
    assert _bitwise_crc32(body) == zlib.crc32(body)
    assert full[-4:] == zlib.crc32(body).to_bytes(4, "little")
