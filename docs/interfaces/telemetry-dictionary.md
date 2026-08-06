# Telemetry Dictionary (ICD)

**Status:** V0 draft. This is the binary wire format both `simulator/` and
`ground-station/` implement against — the protocol is defined here first, and code
conforms to it, not the reverse (see CLAUDE.md conventions). V1 firmware must produce
byte-identical packets from real sensors.

**Byte order:** little-endian, matching Cortex-M (STM32) native encoding — chosen so
V1 firmware doesn't need to byte-swap anything when it eventually replaces the
simulator.

**Transport, V0:** a local TCP socket standing in for a real serial/radio link (see
`docs/architecture/block-diagram.md`). The packet bytes below are exactly what goes
over that socket — when V1 swaps the socket for a UART, this format doesn't change.

## Packet structure

One fixed-format packet, sent at the configured telemetry rate (`FSW-001`).

| Offset | Field | Type | Size | Description |
|---|---|---|---|---|
| 0 | `sync` | uint8 | 1 | Fixed `0xA5` — lets a receiver find packet boundaries in a byte stream |
| 1 | `packet_id` | uint8 | 1 | `0x01` = TELEMETRY |
| 2 | `seq_num` | uint16 | 2 | Increments every packet, wraps at 65535 |
| 4 | `timestamp_ms` | uint32 | 4 | Mission elapsed time in ms since BOOT |
| 8 | `mode` | uint8 | 1 | See Mode enum below |
| 9 | `fault_flags` | uint16 | 2 | Bitmask, see Fault Flag Bits below |
| 11 | `health_flags` | uint8 | 1 | Bitmask, see Health Flag Bits below |
| 12 | `payload_length` | uint16 | 2 | Byte length of the sensor+stats payload (offset 14 through 73). Constant in V0 since there's only one packet shape; becomes meaningful once variable-length packets (e.g. log dumps) exist in V2 — included now per `COM-001` rather than retrofitted later |
| 14 | `temp_c` | float32 | 4 | Degrees C, range -20 to +60 |
| 18 | `accel_x` | float32 | 4 | g, range +/-4 |
| 22 | `accel_y` | float32 | 4 | g, range +/-4 |
| 26 | `accel_z` | float32 | 4 | g, range +/-4 |
| 30 | `gyro_x` | float32 | 4 | deg/s, range +/-500 |
| 34 | `gyro_y` | float32 | 4 | deg/s, range +/-500 |
| 38 | `gyro_z` | float32 | 4 | deg/s, range +/-500 |
| 42 | `mag_x` | float32 | 4 | microtesla, range +/-100 |
| 46 | `mag_y` | float32 | 4 | microtesla, range +/-100 |
| 50 | `mag_z` | float32 | 4 | microtesla, range +/-100 |
| 54 | `bus_voltage_v` | float32 | 4 | Volts. Nominal ~5.0 V; warning <4.5 V, critical <4.0 V *(targets, TBD — see FDIR-003)* |
| 58 | `bus_current_a` | float32 | 4 | Amps, range 0-2 |
| 62 | `uptime_s` | uint32 | 4 | Seconds since BOOT (separate from `timestamp_ms`, which resets on mode-triggering reboot; uptime does not) |
| 66 | `cmd_rx_count` | uint16 | 2 | Commands received since BOOT, wraps at 65535 |
| 68 | `cmd_accept_count` | uint16 | 2 | Commands accepted |
| 70 | `cmd_reject_count` | uint16 | 2 | Commands rejected |
| 72 | `corrupted_rx_count` | uint16 | 2 | Corrupted packets received (failed integrity check), per `COM-004` |
| 74 | `checksum` | uint32 | 4 | CRC32 (per Python `zlib.crc32`) over bytes 0-73 |

**Total packet size: 78 bytes.**

## Mode enum (offset 8)

| Value | Mode |
|---|---|
| 0 | BOOT |
| 1 | NOMINAL |
| 2 | SAFE |
| 3 | TEST |

## Fault Flag Bits (offset 9, uint16 bitmask)

| Bit | Flag | Requirement |
|---|---|---|
| 0 | Sensor timeout | FDIR-002 |
| 1 | Undervoltage warning | FDIR-003 |
| 2 | Undervoltage critical | FDIR-003 |
| 3 | Communications loss | COM-003 |
| 4 | Corrupted packet received | COM-004 |
| 5 | Adaptive-baseline anomaly | FDIR-006 |
| 6 | ML anomaly *(reserved, FDIR-007 not yet implemented)* | FDIR-007 |
| 7 | Last reset was watchdog-triggered | — |
| 8-15 | Reserved | — |

## Health Flag Bits (offset 11, uint8 bitmask)

Bit set = that sensor is healthy/responding.

| Bit | Sensor |
|---|---|
| 0 | Temperature sensor |
| 1 | IMU (accel/gyro) |
| 2 | Magnetometer |
| 3 | Power monitor |
| 4-7 | Reserved |
