# Firmware (`firmware/`)

**Status: hardware bring-up has started.** NUCLEO-F446RE, ICM-20948 (IMU),
TMP102 (temp), and INA226 (power monitor) have arrived. The telemetry
packetizer (CRC32 + packed-struct wire format) has been compiled by a real
`arm-none-eabi-gcc`, flashed, and transmitted over real USART2 hardware — the
received bytes were diffed programmatically against
`simulator/protocol.py`'s `TelemetryPacket.pack()` output for identical field
values and are byte-for-byte identical, checksum included. That is the first
thing in this directory that is genuinely hardware-tested, not just
structurally checked. A first-pass sensor HAL now exists in the CubeIDE project
and is ready for hardware validation; the FDIR port is not written yet.

This directory still holds the V1 planning documentation this file always
did, kept for what remains true: module boundaries and the wire-format
contract were decided ahead of hardware arriving, not improvised under time
pressure once it did — consistent with this project's documentation-before-
code convention (protocol defined in V0, hardware conforms to it, not the
reverse).

**Housekeeping note:** the actual CubeIDE project currently lives at
`CubeSAT/MX/cubesat-fw-blinky` (repo root, outside this directory) rather than
here — that was the fastest path to a working toolchain-proof build and it
hasn't been consolidated into `firmware/` yet. Do not `git add` inside `MX/`;
it's a few thousand CubeMX-generated files that don't belong in version
control yet, and where the real project ought to live is still an open
decision (see below).

`firmware/src/main.c`, `firmware/src/sensors.c` and `firmware/inc/sensors.h` are
copies of the hand-written sources in `MX/`, kept here so they are in version
control. `MX/` is still where they are built and edited, so copy them back here
after any change. These copies are not built on their own: the HAL, startup code
and linker script exist only in the CubeIDE project.

## Toolchain

STM32CubeIDE with STM32 HAL/LL drivers — the choice already stated in the root
`CLAUDE.md`; not relitigated here. Board variant is now known: **NUCLEO-F446RE**.

One bring-up-specific wrinkle worth recording: this CubeIDE build (2.2.0) does
not expose a unified "STM32 Project" wizard with a board selector the way
newer versions do — its own "STM32CubeIDE Empty Project" wizard produces a
bare project with no `.ioc` and no HAL init code. The working path was
standalone STM32CubeMX (board selector → generate code targeting the
STM32CubeIDE toolchain), then CubeIDE's "Import STM32 Project →
STM32CubeMX/STM32CubeIDE Project" wizard to bring the generated project in.
Re-opening the same `.ioc` in standalone CubeMX and regenerating is also how
peripherals get enabled after the fact (this is how USART2 got turned on —
the board selector reserves its pins but doesn't enable the peripheral by
itself if you decline the "initialize with default mode" prompt).

## Sensor bring-up procedure

The current test image is intentionally an ASCII diagnostic image, not a
binary telemetry image. In `MX/cubesat-fw-blinky/Core/Src/main.c`,
`SENSOR_BRINGUP_ONLY` is `1`, so the board prints readable results at 115200
8-N-1. Set it to `0` only after the readings are valid; that re-enables the
binary `TelemetryPacket` transmit path.

Wire the common I2C bus as follows:

| NUCLEO-F446RE | Sensor bus |
|---|---|
| D15 / PB8 | SCL on every sensor |
| D14 / PB9 | SDA on every sensor |
| 3V3 | VCC on every sensor |
| GND | GND on every sensor and the logic analyzer |

Use external 4.7 kOhm pull-ups from SCL and SDA to 3V3 if the breakout boards
do not already provide them. The driver discovers these 7-bit addresses:

- ICM-20948: `0x68` or `0x69`; `WHO_AM_I` must be `0xEA`.
- TMP102: `0x48` through `0x4B`, selected by `ADD0`.
- INA226: `0x40` through `0x4F`, selected by `A0/A1`.
- AK09916 magnetometer inside the ICM-20948 module: `0x0C` after ICM bypass
  mode is enabled.

Open the Nucleo USART2 serial port in PuTTY, reset/run the board, and expect
lines beginning with `IMU`, `TEMP`, `POWER`, `MAG`, followed by `DATA`. The
`DATA` fields are integer-scaled to avoid relying on `printf` floating-point
support: `ax_mg` is milli-g, `temp_cC` is centi-degrees C, `bus_mV` is mV,
and `current_mA` is mA.

The INA226 current value depends on the physical shunt resistor. The driver
defaults to `INA226_SHUNT_OHMS = 0.1`; change that macro in
`Core/Inc/sensors.h` to match the resistor actually fitted on the breakout
before treating current as calibrated. Bus voltage and raw shunt voltage do
not depend on that calibration.

The register choices follow the manufacturer documentation for the
[ICM-20948](https://invensense.tdk.com/wp-content/uploads/2024/03/DS-000189-ICM-20948-v1.6.pdf),
[TMP102](https://www.ti.com/lit/ds/symlink/tmp102.pdf), and
[INA226](https://www.ti.com/lit/ds/symlink/ina226.pdf).

## Planned module boundaries (V1)

The job of everything below is to reproduce on real hardware what
`simulator/environment.py`, `fdir/engine.py`, and `simulator/protocol.py`
already do/define in Python — not to design new behavior. Each planned module
maps to one existing Python counterpart:

| Planned firmware module | Python counterpart | Status |
|---|---|---|
| `inc/telemetry_protocol.h` | `simulator/protocol.py` (structs) | **Done.** Compiles under `arm-none-eabi-gcc`. |
| `src/telemetry_protocol.c` (CRC32 + pack) | `simulator/protocol.py` (`pack()`, `zlib.crc32`) | **Done — hardware-verified, TX direction only.** See below. Receive-side `unpack()`/CRC-check of incoming `CommandPacket`s (needed once telecommands are handled) is not yet ported. |
| `Core/Src/sensors.c` / `Core/Inc/sensors.h` | `simulator/environment.py` (accel/gyro/mag/temp/power generation) | **Bring-up driver written; hardware validation pending** |
| `src/fdir_engine.c` / `inc/fdir_engine.h` | `fdir/engine.py` (`FDIREngine`) | Not started |
| `inc/anomaly_model.h` | `ml/export_embedded.py`, trained from `ml/train.py` | **Done — generated, not compiled.** See below. |

### Sensor HAL drivers (planned, one module per sensor)

- `imu.c/.h` — ICM-20948-class IMU driver (per the root `CLAUDE.md` "Planned
  hardware" list, or an equivalent part chosen once ordering happens), I2C/SPI
  read plus conversion into the accel/gyro/mag engineering units and ranges
  documented in `docs/interfaces/telemetry-dictionary.md` (g, deg/s,
  microtesla).
- `power_monitor.c/.h` — INA219/INA226-class bus voltage/current monitor.
- `temp_sensor.c/.h` — digital temperature sensor driver.

Each driver's contract ends at producing a `fdir/engine.py` `RawSample`-
equivalent C struct: physical values plus a per-sensor responded/ACK flag. A
driver does not decide fault state — that stays the FDIR port's job, matching
`fdir/engine.py`'s existing separation between `RawSample` (physical
observables only) and `FDIREngine` (the decision logic).

### Telemetry packetizer (done — TX direction)

`tm_finalize_telemetry()`/`tm_finalize_command()`/`tm_finalize_ack()` in
`src/telemetry_protocol.c` fill each struct's framing fields (`sync`,
`packet_id`, `payload_length` where applicable) and compute the CRC32 in
place. Because the structs are `__attribute__((packed))` and the target is
little-endian (Cortex-M4, matching Python's `<` struct format), a finished
struct's raw bytes *are* the wire packet — no separate serialization step.

This was tested exactly the way this section used to say it should be: a
fixed test-vector `TelemetryPacket` was built in both Python
(`simulator/protocol.py`) and C, transmitted over real USART2 hardware, the
81 bytes captured directly off `COM5`, and diffed programmatically against
the Python `pack()` output for identical field values. Byte-for-byte
identical across six repeated transmissions, checksum included.

The CRC32 algorithm itself was also checked independently, before ever
touching hardware: `tests/test_firmware_crc32.py` is a line-for-line Python
transliteration of `tm_crc32()`, checked against `zlib.crc32()` on fixed
vectors, random data, and a real packet body. That proves the *algorithm* is
right; the on-hardware byte diff above is what proves the *compiled C file*
is right, which is the check that actually matters.

**Still open:** every field in the test vector was a hand-typed constant, not
a live sensor reading — the next thing this needs is a real value (starting
with the temp sensor) flowing into the packet instead of `23.5f`. Receive-side
parsing of incoming `CommandPacket`s (validate CRC, dispatch by `cmd_id`) is
also not written yet — that's uplink, and everything tested so far is
downlink only.

### FDIR port (planned)

A C port of `fdir/engine.py`'s `FDIREngine.tick()` decision logic. This is
explicitly a **port** of already-designed-and-tested Python logic, not a new
design — the state machine, the per-detector debounce/latch pattern, and
critically the `SAFE_MODE_TRIGGER_FLAGS` boundary (which deliberately
excludes `FaultFlag.ML_ANOMALY` — see `fdir/engine.py`'s module docstring)
must carry over unchanged. That boundary is architectural, not incidental:
the ML advisory must remain unable to force a mode transition in the C port
exactly as it is in the Python engine.

The debounce windows and thresholds in `fdir/config.py`
(`SENSOR_TIMEOUT_DEBOUNCE_S`, `UNDERVOLTAGE_CRITICAL_V`, `THERMAL_DEBOUNCE_S`,
`LOCKUP_WINDOW_SAMPLES`, etc.) must either be kept numerically in sync with
that file, or re-derived independently from the same SRS requirement IDs each
constant already cites (FDIR-002, FDIR-003, FDIR-006, FDIR-009, FDIR-010, …).
Whichever approach gets chosen when this port is actually written, it should
be a deliberate decision, not silent drift between two hand-maintained copies
of the same numbers. A single generated-from-one-spec source for both files is
worth considering at that point rather than committing to hand-copying twice
now, before either side has changed under real use.

### Anomaly model export (done — generated, not compiled or run)

`inc/anomaly_model.h` now exists, generated by `ml/export_embedded.py` from
the Isolation Forest trained in `ml/train.py` (see
`docs/architecture/ml-evaluation-report.md` for what that model actually
catches and misses, evaluated in simulation). It's a flat array export per
tree (feature index, threshold, left/right child, leaf sample count) plus an
`isolation_forest_score()` traversal function using only integer/float
comparisons — no ML runtime dependency. Its header comment documents the
score-sign convention explicitly (original Isolation Forest paper convention,
the *opposite* sign to scikit-learn's `decision_function` used in the
evaluation report — a real thing to get wrong if copied carelessly).

No C compiler exists in this environment, so instead of skipping
verification the struct layouts were mirrored in Python's `ctypes` (which
follows the same packed-struct rules as C) and every field offset/size was
diffed against `simulator/protocol.py`'s actual `struct.Struct` output,
byte-for-byte — see `inc/telemetry_protocol.h`'s own note on this. The
generated `anomaly_model.h` itself was sanity-checked structurally (balanced
braces/parens, no NaN/Inf leaked into thresholds, plausible-looking split
values) but **has not been compiled**. Compiling with a real ARM (or even
host) GCC and checking it against `ml/evaluate.py`'s Python-side scores on
the same inputs is a concrete, currently-undone V1 task.

The FDIR port's tick function should consume this exactly as
`fdir/engine.py`'s `FDIREngine.tick(sample, now, ml_advisory=...)` already
does in Python: as a `MLAdvisory`-equivalent (`score`, `is_anomalous`) input
that can only ever latch `FaultFlag.ML_ANOMALY` through the normal debounce
gate. Nothing about that boundary changes for the port.

## What's explicitly out of scope here

- **No general claim of hardware validation.** The telemetry packetizer (TX
  direction) is genuinely hardware-tested now — see above. Everything else in
  this directory (sensor drivers, the FDIR port, command receive/unpack) is
  still Simulated/Trained-equivalent-in-Python at best, not yet ported to C,
  let alone compiled or run. Don't let one verified piece read as more than
  it is.
- **No new FDIR or telemetry design.** `fdir/engine.py`, `fdir/config.py`, and
  `simulator/protocol.py` are the design; this directory's job in V1 is to
  port and conform to them, not to reinvent any part of that logic.

## Open items

1. **`fdir/config.py` sync mechanism.** Whether V1 hand-copies the debounce/
   threshold constants into a C header, or generates both from one source, is
   an open decision — see "FDIR port" above.
2. **Consolidate the CubeIDE project into this directory.** It currently lives
   at `CubeSAT/MX/cubesat-fw-blinky` — see the housekeeping note at the top of
   this file.
3. **Resolved since this was written:** `docs/requirements/SRS.md` now has
   `FDIR-009`/`FDIR-010` entries matching `fdir/config.py`.
4. **Resolved since this was written:** the `ml/` pipeline and
   `inc/anomaly_model.h` now exist (see above) — generated and structurally
   sanity-checked, not compiled or hardware-tested.
5. **Resolved since this was written:** exact part numbers are known and in
   hand — ICM-20948 (IMU), TMP102 (temp), INA226 (power monitor), all I2C.
6. **Resolved since this was written:** the CRC32 implementation exists in
   `src/telemetry_protocol.c`, checked against `zlib.crc32` (algorithm) and
   against `simulator/protocol.py`'s `pack()` output on real hardware (the
   compiled result) — see "Telemetry packetizer" above.
