/**
 * @file telemetry_protocol.h
 * @brief Wire-format structs for the TELEMETRY / COMMAND / COMMAND_ACK packets,
 *        hand-ported to match simulator/protocol.py EXACTLY -- same field
 *        order, same byte offsets, same sizes. The authoritative byte layout
 *        is docs/interfaces/telemetry-dictionary.md and
 *        docs/interfaces/command-dictionary.md; simulator/protocol.py is the
 *        reference implementation. This header must conform to both, not the
 *        other way around (per CLAUDE.md: "Protocol is defined in V0 and
 *        hardware conforms to it, not the reverse").
 *
 * STATUS: not yet implemented against real hardware. This header has been
 * checked for correct struct layout by mirroring it in Python's ctypes
 * (which follows the same packed-struct layout rules as C) and diffing the
 * packed bytes against simulator/protocol.py's actual TelemetryPacket.pack()
 * output field-for-field -- see the verification performed for this task.
 * It has NOT been compiled or linked by an actual ARM GCC / STM32CubeIDE
 * toolchain (none is available in this environment), and it has never run on
 * a microcontroller. Re-verify sizeof() for each struct against the packet
 * sizes below the first time this header is actually compiled.
 *
 * CRC32 NOTE (read before implementing the checksum function):
 * The `checksum` field in every packet below must be computed with the same
 * algorithm as Python's `zlib.crc32()` -- standard CRC-32/ISO-HDLC, polynomial
 * 0xEDB88320 (reflected), init 0xFFFFFFFF, final XOR 0xFFFFFFFF, computed over
 * every byte of the packet from `sync` up to (not including) `checksum`
 * itself. This is the same CRC-32 variant used by Ethernet, gzip, and PNG, so
 * a standard table-driven or bitwise implementation is fine -- but it has NOT
 * been cross-validated against a real C implementation on this project yet,
 * because no hardware/toolchain has been available to test on. Treat "write a
 * crc32() function and confirm it reproduces zlib.crc32() on a handful of
 * known test vectors shared with the Python side" as a concrete, must-do V1
 * task -- not something to silently assume already works because the
 * algorithm is well-known.
 */

#ifndef TELEMETRY_PROTOCOL_H
#define TELEMETRY_PROTOCOL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ==========================================================================
 * Framing constants (simulator/protocol.py: SYNC_BYTE, PACKET_ID_*)
 * ======================================================================== */

#define TM_SYNC_BYTE            0xA5u

#define TM_PACKET_ID_TELEMETRY  0x01u
#define TM_PACKET_ID_COMMAND    0x10u
#define TM_PACKET_ID_ACK        0x11u

/* ==========================================================================
 * Mode enum (simulator/protocol.py: class Mode(IntEnum))
 *
 * Deliberately NOT used as the type of any packed struct's `mode` field
 * below -- C enum underlying-type size is implementation-defined (commonly
 * 4 bytes unless the toolchain is built with -fshort-enums), which would
 * silently break the packed layout. Struct fields stay uint8_t; use these
 * constants for comparisons/assignment, e.g. `if (pkt.mode == MODE_SAFE)`.
 * ======================================================================== */

typedef enum {
    MODE_BOOT    = 0,
    MODE_NOMINAL = 1,
    MODE_SAFE    = 2,
    MODE_TEST    = 3,
} Mode_t;

/* ==========================================================================
 * FaultFlag bits (simulator/protocol.py: class FaultFlag(IntFlag))
 * Wire field is uint32_t (telemetry-dictionary.md offset 9). Bits 10-31
 * reserved. Same reasoning as Mode_t applies -- plain #defines, not an enum,
 * so bitwise OR/AND of the uint16_t wire field needs no casts.
 *
 * FAULT_ML_ANOMALY is advisory-only in fdir/engine.py: it is deliberately
 * excluded from that engine's SAFE_MODE_TRIGGER_FLAGS and can never by
 * itself force a mode transition. Any firmware port of the FDIR decision
 * logic (see firmware/README.md) MUST preserve that exclusion -- do not let
 * this bit gain autonomous authority in the C port either.
 * ======================================================================== */

#define FAULT_NONE                      0x00000000u
#define FAULT_SENSOR_TIMEOUT            (1u << 0)   /* FDIR-002 */
#define FAULT_UNDERVOLTAGE_WARNING      (1u << 1)   /* FDIR-003 */
#define FAULT_UNDERVOLTAGE_CRITICAL     (1u << 2)   /* FDIR-003 */
#define FAULT_COMMS_LOSS                (1u << 3)   /* COM-003 */
#define FAULT_CORRUPTED_PACKET          (1u << 4)   /* COM-004 */
#define FAULT_ADAPTIVE_ANOMALY          (1u << 5)   /* FDIR-006 */
#define FAULT_ML_ANOMALY                (1u << 6)   /* FDIR-007, advisory only -- see comment above */
#define FAULT_WATCHDOG_RESET            (1u << 7)
#define FAULT_THERMAL_ANOMALY           (1u << 8)   /* FDIR-009 */
#define FAULT_SENSOR_LOCKUP             (1u << 9)   /* FDIR-010 */
#define FAULT_RECOVERY_FAILED           (1u << 10)  /* escalation exhausted */
#define FAULT_DATA_PATH_SUSPECT         (1u << 11)  /* R6: shared bus, not the devices */
#define FAULT_UNKNOWN_ANOMALY           (1u << 12)  /* R10: no diagnosis matches */
/* bits 13-31 reserved */

/* ==========================================================================
 * HealthFlag bits (simulator/protocol.py: class HealthFlag(IntFlag))
 * Wire field is uint16_t (telemetry-dictionary.md offset 13). Bit set = that
 * sensor is healthy/responding. Bits 4-15 reserved.
 * ======================================================================== */

#define HEALTH_NONE      0x0000u
#define HEALTH_TEMP_OK   (1u << 0)
#define HEALTH_IMU_OK    (1u << 1)
#define HEALTH_MAG_OK    (1u << 2)
#define HEALTH_POWER_OK  (1u << 3)
#define HEALTH_ALL_OK    (HEALTH_TEMP_OK | HEALTH_IMU_OK | HEALTH_MAG_OK | HEALTH_POWER_OK)

/* ==========================================================================
 * CommandId enum (simulator/protocol.py: class CommandId(IntEnum))
 * Wire field (`cmd_id`) is uint8_t in both CommandPacket and AckPacket.
 * ======================================================================== */

#define CMD_PING                0x01u
#define CMD_GET_STATUS          0x02u
#define CMD_SET_TELEMETRY_RATE  0x03u
#define CMD_ENTER_SAFE_MODE     0x04u
#define CMD_EXIT_SAFE_MODE      0x05u
#define CMD_RESET_FAULTS        0x06u
#define CMD_REQUEST_LOG         0x07u
#define CMD_ENABLE               0x08u
#define CMD_DISABLE              0x09u
/* CMD_ENABLE/DISABLE: param is the test-function ID (command-dictionary.md) */

/* ==========================================================================
 * AckStatus enum (simulator/protocol.py: class AckStatus(IntEnum))
 * Wire field (`status`) is uint8_t in AckPacket.
 * ======================================================================== */

#define ACK_ACCEPTED                            0x00u
#define ACK_REJECTED_UNKNOWN_CMD                0x01u
#define ACK_REJECTED_BAD_CHECKSUM               0x02u
#define ACK_REJECTED_INVALID_PARAM              0x03u
#define ACK_REJECTED_SAFE_MODE_FAULT_ACTIVE     0x04u
#define ACK_REJECTED_NOT_ALLOWED_IN_MODE        0x05u
#define ACK_REJECTED_NOT_IMPLEMENTED            0x06u
/* Command understood and attempted, but achieved nothing because the underlying
 * condition is still present. Keeps a refused RESET_FAULTS distinguishable from
 * a successful one. Additive to a uint8 field -- no packet layout change. */
#define ACK_REJECTED_CONDITION_STILL_ACTIVE     0x07u

/* Sentinel values for a corrupted CommandPacket's ack (command-dictionary.md
 * "Checksum failures never get silently dropped"): the real seq_num/cmd_id
 * in a corrupted packet can't be trusted, so the ack echoes these instead. */
#define TM_UNKNOWN_SEQ_NUM  0xFFFFu
#define TM_UNKNOWN_CMD_ID   0xFFu

/* ==========================================================================
 * Packet structs
 *
 * __attribute__((packed)) is required, not cosmetic: without it, a
 * standards-conforming compiler is free to insert padding so multi-byte
 * fields land on their natural alignment. Concretely here, `mode` (uint8_t,
 * offset 8) would otherwise force padding before `fault_flags`
 * (uint32_t) to align it to offset 12 instead of the wire format's offset 9
 * -- silently shifting every field after it and desyncing from
 * simulator/protocol.py. GCC/arm-none-eabi-gcc (the STM32CubeIDE toolchain
 * per CLAUDE.md) supports this attribute; `#pragma pack(push,1)` is an
 * equivalent alternative if a different compiler ever needs it.
 *
 * Field order and types below match struct.Struct format codes in
 * simulator/protocol.py 1:1 (B=uint8_t, H=uint16_t, I=uint32_t, f=float),
 * and were verified byte-for-byte against that module's packed output using
 * a ctypes mirror of these exact layouts -- see this task's notes.
 * ======================================================================== */

/**
 * TELEMETRY packet (spacecraft -> ground station).
 * Mirrors simulator/protocol.py TelemetryPacket / docs/interfaces/
 * telemetry-dictionary.md. Total size: 81 bytes.
 *
 * Phase 1b widened fault_flags uint16->uint32 and health_flags uint8->uint16.
 * Ten fault bits are allocated and the planned fault-injection scenario set
 * needs seven more, which overflows uint16. Every offset from fault_flags
 * onward shifted by +3 as a result.
 */
typedef struct __attribute__((packed)) {
    uint8_t  sync;                  /* offset  0: TM_SYNC_BYTE (0xA5) */
    uint8_t  packet_id;             /* offset  1: TM_PACKET_ID_TELEMETRY (0x01) */
    uint16_t seq_num;               /* offset  2: increments every packet, wraps at 65535 */
    uint32_t timestamp_ms;          /* offset  4: mission elapsed time, ms since BOOT */
    uint8_t  mode;                  /* offset  8: Mode_t value */
    uint32_t fault_flags;           /* offset  9: FAULT_* bitmask */
    uint16_t health_flags;          /* offset 13: HEALTH_* bitmask */
    uint16_t payload_length;        /* offset 15: bytes from temp_c through corrupted_rx_count (60) */
    float    temp_c;                /* offset 17: deg C, range -20..+60 */
    float    accel_x;               /* offset 21: g, range +/-4 */
    float    accel_y;               /* offset 25 */
    float    accel_z;               /* offset 29 */
    float    gyro_x;                /* offset 33: deg/s, range +/-500 */
    float    gyro_y;                /* offset 37 */
    float    gyro_z;                /* offset 41 */
    float    mag_x;                 /* offset 45: microtesla, range +/-100 */
    float    mag_y;                 /* offset 49 */
    float    mag_z;                 /* offset 53 */
    float    bus_voltage_v;         /* offset 57: volts, nominal ~5.0 (targets, TBD) */
    float    bus_current_a;         /* offset 61: amps, range 0-2 */
    uint32_t uptime_s;              /* offset 65: seconds since BOOT (not reset by mode-triggering reboot) */
    uint16_t cmd_rx_count;          /* offset 69 */
    uint16_t cmd_accept_count;      /* offset 71 */
    uint16_t cmd_reject_count;      /* offset 73 */
    uint16_t corrupted_rx_count;    /* offset 75: per COM-004 */
    uint32_t checksum;              /* offset 77: CRC32 over bytes 0-76, see CRC32 NOTE above */
} TelemetryPacket_t;

_Static_assert(sizeof(TelemetryPacket_t) == 81,
               "TelemetryPacket_t must be exactly 81 bytes to match simulator/protocol.py");

/**
 * COMMAND packet (ground station -> spacecraft).
 * Mirrors simulator/protocol.py CommandPacket / docs/interfaces/
 * command-dictionary.md. Total size: 13 bytes.
 */
typedef struct __attribute__((packed)) {
    uint8_t  sync;          /* offset 0: TM_SYNC_BYTE (0xA5) */
    uint8_t  packet_id;     /* offset 1: TM_PACKET_ID_COMMAND (0x10) */
    uint16_t seq_num;        /* offset 2: ground-assigned, increments per command sent */
    uint8_t  cmd_id;         /* offset 4: CMD_* value */
    float    param;          /* offset 5: meaning depends on cmd_id; 0 if unused */
    uint32_t checksum;       /* offset 9: CRC32 over bytes 0-8 */
} CommandPacket_t;

_Static_assert(sizeof(CommandPacket_t) == 13,
               "CommandPacket_t must be exactly 13 bytes to match simulator/protocol.py");

/**
 * COMMAND_ACK packet (spacecraft -> ground station).
 * Mirrors simulator/protocol.py AckPacket / docs/interfaces/
 * command-dictionary.md. Total size: 10 bytes.
 */
typedef struct __attribute__((packed)) {
    uint8_t  sync;          /* offset 0: TM_SYNC_BYTE (0xA5) */
    uint8_t  packet_id;     /* offset 1: TM_PACKET_ID_ACK (0x11) */
    uint16_t seq_num;        /* offset 2: echoes command's seq_num, or TM_UNKNOWN_SEQ_NUM if checksum failed */
    uint8_t  cmd_id;         /* offset 4: echoes command's cmd_id, or TM_UNKNOWN_CMD_ID if checksum failed */
    uint8_t  status;         /* offset 5: ACK_* value */
    uint32_t checksum;       /* offset 6: CRC32 over bytes 0-5 */
} AckPacket_t;

_Static_assert(sizeof(AckPacket_t) == 10,
               "AckPacket_t must be exactly 10 bytes to match simulator/protocol.py");

#ifdef __cplusplus
}
#endif

#endif /* TELEMETRY_PROTOCOL_H */
