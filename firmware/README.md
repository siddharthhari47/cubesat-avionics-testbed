# Firmware (`firmware/`)

**Status: not yet implemented. Nothing in this directory has run on real hardware.**

No board has been purchased (see the root `README.md` — "V0 complete... No
hardware has been purchased and no firmware written"). This directory holds V1
planning documentation and one hand-written header stub only. Nothing here has
been compiled, flashed, or executed on a microcontroller. It exists so that
module boundaries, the wire-format contract, and the toolchain choice are
decided ahead of time instead of improvised once hardware arrives under time
pressure — consistent with this project's documentation-before-code convention
(protocol defined in V0, hardware conforms to it, not the reverse).

## Toolchain

STM32CubeIDE with STM32 HAL/LL drivers — the choice already stated in the root
`CLAUDE.md`; not relitigated here. Generating the actual CubeIDE project
(`.ioc` device/pin/clock configuration via CubeMX, `.project`/`.cproject`)
requires knowing the exact physical board variant, which isn't chosen yet.
Producing that project now against a guessed part number would just be thrown
away once the real board is in hand, so it is deliberately deferred, per this
task's scope, to when hardware exists.

## Planned module boundaries (V1)

The job of everything below is to reproduce on real hardware what
`simulator/environment.py`, `fdir/engine.py`, and `simulator/protocol.py`
already do/define in Python — not to design new behavior. Each planned module
maps to one existing Python counterpart:

| Planned firmware module | Python counterpart | Status |
|---|---|---|
| `inc/telemetry_protocol.h` | `simulator/protocol.py` (structs) | **Done — this task.** Header stub only. |
| `src/telemetry_protocol.c` (CRC32 + pack/unpack) | `simulator/protocol.py` (`pack()`/`unpack()`, `zlib.crc32`) | Not started |
| `src/imu.c` / `inc/imu.h` | `simulator/environment.py` (accel/gyro/mag generation) | Not started |
| `src/power_monitor.c` / `inc/power_monitor.h` | `simulator/environment.py` (bus voltage/current generation) | Not started |
| `src/temp_sensor.c` / `inc/temp_sensor.h` | `simulator/environment.py` (temp generation) | Not started |
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

### Telemetry packetizer (planned)

Takes the current sensor readings plus current FDIR state (mode, fault_flags,
health_flags) and produces the exact byte sequence `simulator/protocol.py`'s
`TelemetryPacket.pack()` would produce for the same field values, using the
structs in `inc/telemetry_protocol.h`. This is directly testable once hardware
exists — and should be tested first, before anything else in this list,
because every other module's output only matters if it survives the wire
format unchanged: feed known field values through both the Python `pack()`
and the C packetizer and diff the bytes.

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

- **No CubeIDE project files** (`.ioc`, `.project`, `.cproject`). Real project
  generation needs CubeMX with the actual board's pin map, clock tree, and
  peripheral assignment — none of which can be decided correctly before the
  board is chosen and in hand.
- **No claim of hardware validation.** Nothing in this directory has been
  compiled by an ARM GCC toolchain, flashed, or run on a microcontroller.
  Everything here is Simulated/Trained-equivalent-in-Python, ported to C by
  hand, and not yet Hardware-tested — see `inc/telemetry_protocol.h`'s header
  comment for exactly how its layout was checked in the absence of a
  compiler.
- **No new FDIR or telemetry design.** `fdir/engine.py`, `fdir/config.py`, and
  `simulator/protocol.py` are the design; this directory's job in V1 is to
  port and conform to them, not to reinvent any part of that logic.

## Open items for a human to confirm once the board is chosen

1. **Exact part numbers.** `CLAUDE.md`'s hardware list says "ICM-20948-class,"
   "INA219/INA226," etc. — genuinely undecided until purchase. The register
   maps, I2C addresses, and conversion constants in the sensor HAL modules
   above all depend on the specific part actually bought.
2. **CRC32 implementation.** Flagged explicitly in `inc/telemetry_protocol.h`:
   the checksum algorithm (CRC-32/ISO-HDLC, matching `zlib.crc32`) is
   well-documented, but no C implementation has been written or tested here
   yet. Write it against shared test vectors with the Python side before
   trusting it.
3. **`fdir/config.py` sync mechanism.** Whether V1 hand-copies the debounce/
   threshold constants into a C header, or generates both from one source, is
   an open decision — see "FDIR port" above.
4. **Resolved since this was written:** `docs/requirements/SRS.md` now has
   `FDIR-009`/`FDIR-010` entries matching `fdir/config.py`.
5. **Resolved since this was written:** the `ml/` pipeline and
   `inc/anomaly_model.h` now exist (see above) — generated and structurally
   sanity-checked, not compiled or hardware-tested.
