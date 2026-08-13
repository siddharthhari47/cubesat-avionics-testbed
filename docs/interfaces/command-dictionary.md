# Command Dictionary (ICD)

**Status:** V0 draft. Same rules as the telemetry dictionary: little-endian, sent
over the V0 TCP socket standing in for a real link, and this format doesn't change
when V1 firmware replaces the simulator.

## Command packet (ground station -> spacecraft)

| Offset | Field | Type | Size | Description |
|---|---|---|---|---|
| 0 | `sync` | uint8 | 1 | Fixed `0xA5` |
| 1 | `packet_id` | uint8 | 1 | `0x10` = COMMAND |
| 2 | `seq_num` | uint16 | 2 | Ground-assigned, increments per command sent |
| 4 | `cmd_id` | uint8 | 1 | See Command ID enum |
| 5 | `param` | float32 | 4 | Meaning depends on `cmd_id`; `0` if unused |
| 9 | `checksum` | uint32 | 4 | CRC32 over bytes 0-8 |

**Total: 13 bytes.**

## Acknowledgement packet (spacecraft -> ground station)

Every command gets exactly one of these back, per `COM-002` (within 1 s).

| Offset | Field | Type | Size | Description |
|---|---|---|---|---|
| 0 | `sync` | uint8 | 1 | Fixed `0xA5` |
| 1 | `packet_id` | uint8 | 1 | `0x11` = COMMAND_ACK |
| 2 | `seq_num` | uint16 | 2 | Echoes the command's `seq_num` — `0xFFFF` if the command's checksum failed and `seq_num` can't be trusted |
| 4 | `cmd_id` | uint8 | 1 | Echoes which command this responds to — `0xFF` (UNKNOWN) if checksum failed |
| 5 | `status` | uint8 | 1 | See Status enum |
| 6 | `checksum` | uint32 | 4 | CRC32 over bytes 0-5 |

**Total: 10 bytes.**

## Command ID enum (offset 4 of command packet)

| Value | Command | `param` meaning | V0 behavior |
|---|---|---|---|
| 0x01 | `PING` | unused | Always accepted |
| 0x02 | `GET_STATUS` | unused | Always accepted (status arrives on the next telemetry packet, not a separate reply) |
| 0x03 | `SET_TELEMETRY_RATE` | rate in Hz, 0.5-10 (`FSW-001`) | Accepted if in range, else `REJECTED_INVALID_PARAM` |
| 0x04 | `ENTER_SAFE_MODE` | unused | Accepted from any mode (operator-forced SAFE entry) |
| 0x05 | `EXIT_SAFE_MODE` | unused | Accepted only if not in SAFE, or in SAFE with no active fault (`FDIR-005`); otherwise `REJECTED_SAFE_MODE_FAULT_ACTIVE` |
| 0x06 | `RESET_FAULTS` | unused | Clears latched fault flags whose underlying condition has cleared; a fault still actively occurring is not cleared |
| 0x07 | `REQUEST_LOG` | unused | `REJECTED_NOT_IMPLEMENTED` in V0 — no SD logging exists until V2 |
| 0x08 | `ENABLE` (test function) | test function ID | Accepted only in NOMINAL/TEST, else `REJECTED_NOT_ALLOWED_IN_MODE` |
| 0x09 | `DISABLE` (test function) | test function ID | Accepted only in TEST, else `REJECTED_NOT_ALLOWED_IN_MODE` |
| `0x0A` | `RECOMMISSION_REFERENCE` | — | Discard the commissioning voltage reference and capture a new one. **The escape from R7's trap:** a reference captured wrongly makes the drift detector latch on healthy telemetry, which sheds a rail, and neither `RESET_FAULTS` nor a capability restore can clear it — the condition genuinely is breaching against a reference that is itself wrong. Always `ACCEPTED`. |
| `0x0B` | `RESTORE_CAPABILITY` | — | Return to full capability from a degraded rung, re-powering shed rails. Refused with `REJECTED_CONDITION_STILL_ACTIVE` while the cause is present — the same evidence discipline as `EXIT_SAFE_MODE`. |

## Status enum (offset 5 of ack packet)

| Value | Status |
|---|---|
| 0x00 | ACCEPTED |
| 0x01 | REJECTED_UNKNOWN_CMD |
| 0x02 | REJECTED_BAD_CHECKSUM |
| 0x03 | REJECTED_INVALID_PARAM |
| 0x04 | REJECTED_SAFE_MODE_FAULT_ACTIVE |
| 0x05 | REJECTED_NOT_ALLOWED_IN_MODE |
| 0x06 | REJECTED_NOT_IMPLEMENTED |
| 0x07 | REJECTED_CONDITION_STILL_ACTIVE |

## Notes

- **Checksum failures never get silently dropped.** Per `COM-004`, a corrupted
  command still gets an ack — `REJECTED_BAD_CHECKSUM` — using the sentinel
  `seq_num`/`cmd_id` values above, since the real values in a corrupted packet
  can't be trusted.
- **A command that achieves nothing must not report `ACCEPTED`.**
  `RESET_FAULTS` returns `REJECTED_CONDITION_STILL_ACTIVE` (0x07) when every
  latched flag it was asked to clear is still backed by an active condition.
  It previously always returned `ACCEPTED`, which made a refused reset
  indistinguishable from a successful one — unacceptable in a system whose
  whole premise is verifying what an action actually did. `ACCEPTED` means
  either "something was cleared" or "there was nothing to clear".
- **`RESET_FAULTS` requires positive evidence.** A condition-backed flag clears
  only after the engine has *observed* the condition non-breaching for
  `RESET_EVIDENCE_SAMPLES` consecutive samples. Event flags
  (`WATCHDOG_RESET`, `CORRUPTED_PACKET`) record something that already happened
  rather than an ongoing condition, so acknowledging them always clears them.
- **`ENTER_SAFE_MODE` is intentionally unrestricted** — an operator should always be
  able to force SAFE mode as a manual safety action, regardless of current state.
  `EXIT_SAFE_MODE` is the direction that's restricted, matching `FDIR-005`.
