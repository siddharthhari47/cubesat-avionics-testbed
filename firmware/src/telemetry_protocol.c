/**
 * @file telemetry_protocol.c
 * @brief CRC32 and packet-finalizer implementation for telemetry_protocol.h.
 *
 * STATUS: the CRC32 algorithm below is verified against zlib.crc32() via a
 * line-for-line Python transliteration -- see tests/test_firmware_crc32.py.
 * That proves the ALGORITHM is right; it does not prove this C file compiles
 * or behaves identically once arm-none-eabi-gcc has optimized it, because no
 * C toolchain was available to build it in the environment that wrote it.
 * The load-bearing check is still on target: build the fixed test vector
 * from that same test file in C, transmit it over USART2, and diff the
 * received bytes against the Python pack() output for identical field
 * values. Do that before trusting this file either.
 */

#include "telemetry_protocol.h"
#include <stddef.h>

uint32_t tm_crc32(const uint8_t *data, uint32_t length)
{
    /* Standard bitwise CRC-32/ISO-HDLC (the zlib/Ethernet/gzip/PNG variant):
     * poly 0xEDB88320 reflected, init 0xFFFFFFFF, final XOR 0xFFFFFFFF. No
     * lookup table -- packets here are at most 81 bytes and this runs at
     * most a handful of Hz, so the ~8x speed a table buys costs 1KB of
     * flash for a device that isn't sending enough bytes to notice the
     * difference. Revisit only if profiling ever says otherwise. */
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < length; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 1u) {
                crc = (crc >> 1) ^ 0xEDB88320u;
            } else {
                crc = crc >> 1;
            }
        }
    }
    return crc ^ 0xFFFFFFFFu;
}

void tm_finalize_telemetry(TelemetryPacket_t *pkt)
{
    pkt->sync = TM_SYNC_BYTE;
    pkt->packet_id = TM_PACKET_ID_TELEMETRY;
    /* Bytes from temp_c through corrupted_rx_count, i.e. everything between
     * the framing header and the checksum -- matches simulator/protocol.py's
     * `_TM_STRUCT.size - 17` exactly (see TelemetryPacket.pack() there). */
    pkt->payload_length = (uint16_t)(offsetof(TelemetryPacket_t, checksum) -
                                      offsetof(TelemetryPacket_t, temp_c));
    pkt->checksum = tm_crc32((const uint8_t *)pkt,
                              offsetof(TelemetryPacket_t, checksum));
}

void tm_finalize_command(CommandPacket_t *pkt)
{
    pkt->sync = TM_SYNC_BYTE;
    pkt->packet_id = TM_PACKET_ID_COMMAND;
    pkt->checksum = tm_crc32((const uint8_t *)pkt,
                              offsetof(CommandPacket_t, checksum));
}

void tm_finalize_ack(AckPacket_t *pkt)
{
    pkt->sync = TM_SYNC_BYTE;
    pkt->packet_id = TM_PACKET_ID_ACK;
    pkt->checksum = tm_crc32((const uint8_t *)pkt,
                              offsetof(AckPacket_t, checksum));
}
