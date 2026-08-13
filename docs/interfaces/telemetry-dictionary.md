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
| 9 | `fault_flags` | uint32 | 4 | Bitmask, see Fault Flag Bits below |
| 13 | `health_flags` | uint16 | 2 | Bitmask, see Health Flag Bits below |
| 15 | `payload_length` | uint16 | 2 | Byte length of the sensor+stats payload (offset 17 through 76) |
| 17 | `temp_c` | float32 | 4 | Degrees C, range -20 to +60 |
| 21 | `accel_x` | float32 | 4 | g, range +/-4 |
| 25 | `accel_y` | float32 | 4 | g, range +/-4 |
| 29 | `accel_z` | float32 | 4 | g, range +/-4 |
| 33 | `gyro_x` | float32 | 4 | deg/s, range +/-500 |
| 37 | `gyro_y` | float32 | 4 | deg/s, range +/-500 |
| 41 | `gyro_z` | float32 | 4 | deg/s, range +/-500 |
| 45 | `mag_x` | float32 | 4 | microtesla, range +/-100 |
| 49 | `mag_y` | float32 | 4 | microtesla, range +/-100 |
| 53 | `mag_z` | float32 | 4 | microtesla, range +/-100 |
| 57 | `bus_voltage_v` | float32 | 4 | Volts. Nominal ~5.0 V; warning <4.5 V, critical <4.0 V *(targets, TBD — see FDIR-003)* |
| 61 | `bus_current_a` | float32 | 4 | Amps, range 0-2 |
| 65 | `uptime_s` | uint32 | 4 | Seconds since BOOT (separate from `timestamp_ms`, which resets on mode-triggering reboot; uptime does not) |
| 69 | `cmd_rx_count` | uint16 | 2 | Commands received since BOOT, wraps at 65535 |
| 71 | `cmd_accept_count` | uint16 | 2 | Commands accepted |
| 73 | `cmd_reject_count` | uint16 | 2 | Commands rejected |
| 75 | `corrupted_rx_count` | uint16 | 2 | Corrupted packets received (failed integrity check), per `COM-004` |
| 77 | `checksum` | uint32 | 4 | CRC32 (per Python `zlib.crc32`) over bytes 0-76 |

**Total packet size: 81 bytes.**

*Phase 1b widened `fault_flags` uint16 -> uint32 and `health_flags` uint8 -> uint16, shifting every offset from `fault_flags` onward by +3. Ten fault bits are allocated and the planned fault-injection scenario set needs seven more, which overflows uint16.*

## Mode enum (offset 8)

| Value | Mode |
|---|---|
| 0 | BOOT |
| 1 | NOMINAL |
| 2 | SAFE |
| 3 | TEST |

## Fault Flag Bits (offset 9, uint32 bitmask)

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
| 8 | Thermal anomaly | FDIR-009 |
| 9 | Sensor lockup (stuck/frozen reading, distinct from timeout) | FDIR-010 |
| 10 | Recovery campaign exhausted every rung without verification | R3 |
| 11 | Data-path suspect: 2+ devices on one bus invalid together | R6 |
| 12 | Unknown anomaly: no enumerated diagnosis matches | R10 |
| 13 | Sensor invalid: a channel returned a non-finite value (NaN/inf) | F3 |
| 14 | Sensor implausible: one device returning impossible values, its bus healthy | FDIR-012 |
| 15 | Rail overcurrent: a switchable rail above its nominal ceiling | FDIR-011 |
| 16-31 | Reserved | — |

**On bit 13.** Added after the V0 adversarial safety review. Every comparison
with NaN is False in both Python and C, so a NaN reading silently satisfied or
defeated every threshold written about it depending only on how the predicate
happened to be phrased — one detector failed open, another failed closed, on the
same reading. Worse, a NaN bus voltage was recorded as *positive evidence* that a
latched undervoltage had cleared, which could return a vehicle correctly held in
SAFE to service. Making "this channel is carrying no information" a first-class
observable is the fix.

It carries **neither SAFE-mode nor recovery authority**: an invalid reading means
the sensor is untrustworthy, not that the spacecraft is in danger, and inventing a
specific fault out of a broken channel is the wrong-diagnosis failure R10 exists
to prevent. Clearable via `RESET_FAULTS` on the usual positive evidence.

**On bits 14 and 15.** Bit 14 is the single-device partner to bit 11: two devices on
one bus failing together is better explained by the bus, but *one* device failing
alone genuinely is a device fault, and nothing latched for that case until now. Like
bit 13 it carries no authority — the sensor, its wiring and its connector all fit the
evidence equally, so acting would mean inventing a diagnosis.

Bit 15 is the KySat-2 bit. That spacecraft lost its battery to a rail that kept
drawing while every fixed *voltage* threshold stayed satisfied; the current is where
the fault is visible first. It **does** carry recovery authority, because the correct
response is specific and known (remove power from that rail), but deliberately not
SAFE-mode authority — a payload rail must not be able to safe the whole vehicle.

## Health Flag Bits (offset 13, uint16 bitmask)

Bit set = that sensor is healthy/responding.

| Bit | Sensor |
|---|---|
| 0 | Temperature sensor |
| 1 | IMU (accel/gyro) |
| 2 | Magnetometer |
| 3 | Power monitor |
| 4-15 | Reserved |
