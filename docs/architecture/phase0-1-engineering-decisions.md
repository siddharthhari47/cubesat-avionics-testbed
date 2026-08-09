# Phase 0/1 Engineering Decisions

**Status of this document itself:** written during Phase 1 implementation, not
before it -- decisions below were made and then executed, not speculated about
in advance and left to rot. Where the ML pipeline or firmware readiness work
changed a decision's specifics, this document was updated to match, not the
other way around.

**Scope discipline:** this document covers Phase 0 (understand and define) and
Phase 1 (build the digital foundation) only. Phase 2 is deliberately
undefined -- it depends on what Phase 1 actually teaches us once it's
finished, not on a plan made before any of this existed. Nothing below should
be read as a Phase 2 commitment.

## Phase 0: repository audit

Before any code changed, the repository already contained a working V0:

- `CLAUDE.md` -- the project's own operating rules (staged V0-V4 roadmap,
  requirement ID conventions, "hardware ordered only after V0 works,"
  documentation-proportionality principle, instruction to push back on
  premature scope).
- `docs/requirements/SRS.md` -- 26+ traceable requirements (`SYS`/`FSW`/`COM`/
  `GS`/`FDIR` prefixes), each with a verification method, several already
  anticipating a hybrid ML+deterministic FDIR architecture (`FDIR-006`
  adaptive statistical baseline, `FDIR-007` ML-augmented anomaly detection
  marked *Planned* with citations to published CubeSat TinyML literature
  rather than a claim of novelty) and a false-positive budget (`FDIR-008`).
- `docs/architecture/` -- a block diagram and a BOOT/NOMINAL/SAFE/TEST mode
  state diagram, both Mermaid-in-Markdown, with debounce/persistence
  reasoning already worked into the transition table.
- `docs/interfaces/` -- a byte-exact telemetry dictionary and command
  dictionary (little-endian, CRC32-checked, sync-byte framed).
- `simulator/protocol.py` -- the Python implementation of that ICD:
  `TelemetryPacket`/`CommandPacket`/`AckPacket` with `pack()`/`unpack()`, a
  `FaultFlag` `IntFlag` with two reserved bit ranges, a mode enum.
  Well-tested, working, not touched except to add two new flag bits (see
  below) -- this was a case of "where the existing solution is good,
  preserve it."
- `simulator/run_simulator.py` -- a single-file TCP-serving simulator: sensor
  generation, fault injection via stdin, and fault-detection/mode-transition
  logic all coupled together inside one `Simulator` class. This worked
  correctly and was genuinely tested (including two real bugs found and
  fixed during that testing: an adaptive-baseline cold-start false positive,
  and a connection-hijacking bug when a second client connected). It was
  also the one piece of the existing system that needed real surgery for
  Phase 1 -- see "What was preserved vs. changed" below.
- `ground-station/` -- a Streamlit dashboard (`dashboard.py`) and a threaded
  TCP client (`link.py`) that reads telemetry independent of Streamlit's
  rerun cycle. Fully working, live-tested including command round-trips and
  CSV logging. **Not modified during Phase 1** -- the wire protocol didn't
  change shape, only gained two new flag bits, so nothing here needed to
  change, confirmed by a live TCP test against the refactored server.
- `firmware/`, `hardware/`, `media/`, `tests/` -- empty placeholders.
  `firmware/` and `tests/` are populated in Phase 1 (see below); `hardware/`
  and `media/` correctly remain empty, since no hardware exists yet.

**What this meant for Phase 0's engineering definition:** the project already
had a sound requirements-and-interfaces foundation and a working (if
tightly-coupled) reference implementation of FDIR-flavored logic. The Phase 1
job was not "design FDIR from scratch" -- it was "extract what already works
into a form that's independently testable, evaluable against an ML pipeline
on equal footing, and portable to firmware later," plus build the ML pipeline
and dataset infrastructure that didn't exist yet at all.

## Key decisions

### 1. New top-level packages: `fdir/` and `ml/`

`simulator/run_simulator.py`'s original `Simulator` class mixed three
concerns: TCP/threading (server), sensor physics + fault injection
(environment), and fault detection + mode transitions (FDIR). That coupling
was a reasonable simplification for a single-script V0 demo. It stopped being
reasonable once two new requirements appeared: the FDIR logic needed to be
evaluated against an ML detector on identical footing (impossible if it's
tangled with socket code), and it needed to eventually port to firmware
without dragging Python threading along with it.

Decision: extract into three independent modules, each usable standalone:

- `fdir/engine.py` -- `FDIREngine`, a (near-)pure function of
  `(RawSample, elapsed time, optional MLAdvisory) -> (mode, fault_flags,
  health_flags)`. No sockets, no threads, no simulation-only shortcuts.
- `simulator/environment.py` -- `SpacecraftEnvironment`, sensor physics and
  fault-injection ground truth. No concept of BOOT/NOMINAL/SAFE/TEST at all.
- `simulator/run_simulator.py` -- now a thin adapter wiring the two together
  to a TCP socket, plus link-level bookkeeping (sequence numbers, command
  counters) that belongs to neither module.

This is not a rewrite for its own sake -- the original file's actual decision
logic (thresholds, debounce windows, command semantics) was preserved
line-for-line in meaning, moved rather than redesigned, and re-verified
against the same test scenarios after moving. Where the move *did* change
something, it's called out explicitly below.

`ml/` is new because there was no ML component in the pre-existing directory
layout at all -- it doesn't overload `simulator/` (the environment, not the
detector) or `ground-station/` (the operator UI, not model training).

### 2. FDIR must never read ground truth

This is the single most important design rule in the codebase, and it's
enforced structurally, not just by convention: `fdir/engine.py`'s
`RawSample` dataclass contains only values a real STM32 could physically
observe -- raw sensor readings, and whether a sensor produced a fresh
reading this tick (`imu_responded`/`temp_responded`, a real ACK/NACK-level
observable). It never contains "is fault X currently injected."

The original V0 code violated this in a way that was fine for a demo and
would have been a real problem here: its undervoltage detector read the
simulator's own `injected_undervoltage` boolean directly, rather than
thresholding the voltage value the sample actually carried. That makes the
"detector" a mirror of the test harness's own answer key, not something that
can be meaningfully evaluated or compared against an ML detector reading the
same data. Every deterministic detector in `fdir/engine.py` was rewritten to
threshold the actual sample value (or, for timeout/lockup, actual
response/repeat behavior) instead. `simulator/environment.py`'s ground truth
(`GroundTruth.active_faults`) exists only for test harnesses, the dataset
generator, and the evaluation report -- never as an input to `FDIREngine`.

### 3. ML is advisory only -- enforced in code, not policy

`fdir/engine.py` defines:

```python
SAFE_MODE_TRIGGER_FLAGS = (
    FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY | FaultFlag.SENSOR_LOCKUP
)
```

`FaultFlag.ML_ANOMALY` is deliberately absent from this mask. The ML
advisory can only ever latch that one flag bit, through the same debounce
gate every deterministic detector uses -- it is visible in telemetry, it is
logged, and it can never by itself change `self.mode`. `FaultFlag.
ADAPTIVE_ANOMALY` (the existing statistical/EWMA detector, deterministic but
not threshold-based) is held to the same standard and was already
advisory-only in V0; that choice was preserved rather than revisited, since
no new evidence argued for changing it.

This was a deliberate, conservative choice among real alternatives (e.g.
"ML + one corroborating deterministic signal could trigger SAFE") that were
considered and rejected for Phase 1: the bright, simple line -- learned
models never move spacecraft state, full stop -- is easier to defend, easier
to test, and matches the project owner's explicit instruction ("the AI
should never simply be given unrestricted control") without needing a case
by-case argument about which combinations of corroboration would be safe
enough. Revisiting this is a legitimate Phase 2+ question once real
detection performance data exists; it should not be decided speculatively.

### 4. ML algorithm: Isolation Forest, not a neural network

Investigated tradeoffs (see `docs/architecture/ml-evaluation-report.md` for
the full writeup produced alongside the trained model):

- **Isolation Forest** (chosen): inference is pure comparison-based tree
  traversal -- no floating-point matrix multiplication, no NN runtime
  dependency, trivially portable to hand-written C (see
  `ml/export_embedded.py` and `firmware/inc/anomaly_model.h`). Training is
  cheap, unsupervised (appropriate since reliable fault-labeled data isn't
  realistically available operationally), and doesn't assume Gaussian-
  distributed data the way a pure z-score approach does, so it can catch
  multivariate correlated anomalies a per-channel threshold would miss.
  Model size and inference cost both scale directly with `n_estimators` and
  tree depth, which are chosen explicitly with the STM32 flash/RAM budget in
  mind, not defaulted blindly.
- **Rejected: One-Class SVM / Local Outlier Factor** -- both need to retain
  training data or support vectors at inference time, which is a real memory
  cost an MCU doesn't have to spare.
- **Rejected (for Phase 1): small autoencoder / other NN** -- would need
  either a NN inference runtime (TFLite Micro) or hand-rolled matrix-multiply
  code, more surface area to validate and port correctly, for a synthetic
  multivariate problem that doesn't yet justify the complexity. Not ruled out
  forever -- worth revisiting once real sensor data exists and if Isolation
  Forest's detection performance turns out to be the limiting factor, not the
  MCU budget.
- **Not attempted: reinforcement learning** -- explicitly out of scope per
  the project owner's constraints, and not a fit for this problem regardless
  (RL is for learning control policies through interaction; this is anomaly
  detection over a fixed telemetry stream, a much better match for
  unsupervised tabular ML).

**Honesty constraint, enforced in the evaluation report:** the model's
anomaly score is the standard Isolation Forest path-length-based score. It is
explicitly documented as *not* a probability, and nothing in this codebase
presents it as one.

### 5. Reproducibility: seeded environment, not the global `random` module

The original simulator called `random.gauss(...)` directly against Python's
global RNG state. That's fine for a live interactive demo and wrong for
anything that needs to be reproduced or scored -- two runs of the same
"experiment" would never produce the same telemetry, which makes ground-truth
evaluation meaningless. `SpacecraftEnvironment` now owns a single
`random.Random(seed)` instance; the live TCP server still defaults to
`seed=None` (matching the old non-deterministic demo behavior, since nothing
about live dashboard use needs reproducibility) but accepts `--seed` for
reproducible runs, and `simulator/dataset_gen.py` always seeds explicitly and
records the seed in its manifest.

### 6. Fault types: two new, one generalized

Starting set from the project brief was power abnormalities, sensor lockup,
gradual drift, and thermal abnormalities. V0 already had power (undervoltage)
and a *step-change* version of "drift." Added for Phase 1:

- **`thermal`** -- new, threshold + debounce, same pattern as undervoltage.
- **`sensor_lockup`** -- new, and deliberately distinct from the existing
  `sensor_timeout`: a locked-up sensor keeps ACKing but returns a frozen
  value, versus a timed-out sensor not responding at all. These are
  different real failure modes with different detection strategies (a
  response/no-response check vs. a repeated-value check over a short
  window), and conflating them would have been a real modeling mistake, not
  a simplification.
- **`gradual_drift`** -- generalized from a step change to a true linear
  ramp toward a floor that stays above the fixed critical threshold for its
  entire duration. This preserves V0's original point (the adaptive/
  statistical detector should catch something the fixed threshold structurally
  can't) but makes it scientifically honest -- a step to a fixed offset is
  not "drift" in any real sense.

Fault types are added as named cases in `simulator/environment.py`'s
`FAULT_TYPES` tuple and a `case` in `.step()`; nothing about the interface
constrains the set to these five, and the codebase should be read as
expecting more to be added the same way, not as a closed list.

### 7. Embedded readiness: documentation and struct stubs, not a CubeIDE project

No hardware exists yet, and CubeIDE project generation is meaningfully tied
to the exact board/pin configuration via CubeMX -- generating one now would
mean inventing configuration that would just get thrown away once the real
board arrives. What *was* built: `firmware/README.md` documenting the
planned module boundaries and explicitly marked not-yet-implemented, and
`firmware/inc/telemetry_protocol.h` -- hand-written C structs mirroring
`protocol.py`'s wire format exactly, byte-for-byte, which is real, useful,
hardware-independent work that removes a translation step from V1 rather
than deferring it. `ml/export_embedded.py` similarly produces a real C header
of the trained model's tree structure, not a placeholder.

## What was preserved vs. changed vs. new

**Preserved without change:** `simulator/protocol.py`'s existing packet
formats and enums (only additive changes -- two new flag bits in previously
reserved space); `ground-station/` in its entirety; all existing
documentation's tone, ID conventions, and TBD-marking discipline;
`ADAPTIVE_ANOMALY`'s advisory-only status; the BOOT/NOMINAL/SAFE/TEST mode
model itself.

**Changed, with a stated reason each time:** FDIR logic extracted out of
`run_simulator.py` (reason: independent testability + firmware portability,//
sections 1-2 above); fault detectors changed from ground-truth-flag-reading
to actual value-thresholding (reason: section 2, makes detection actually
evaluable); `reset_faults()` changed from taking an externally-supplied
"still active" ground-truth parameter to deriving clearance from the
engine's own tracked debounce state (reason: the external parameter was
ground truth by another name; real firmware couldn't supply it either); the
"drift" fault generalized from a step to a ramp (reason: section 6).

**New:** `fdir/` package; `simulator/environment.py`;
`simulator/dataset_gen.py`; `ml/` package (features, training, evaluation,
embedded export); `firmware/inc/telemetry_protocol.h` and
`firmware/inc/anomaly_model.h`; formal `tests/` suite; `THERMAL_ANOMALY` and
`SENSOR_LOCKUP` fault types and their corresponding `FaultFlag` bits;
`FDIR-009`/`FDIR-010` requirements; a `Status` (Simulated/Trained/
Hardware-tested/Experimentally-validated) column added across the SRS.

## Bugs found and fixed during this work

Listed here deliberately, per this project's own convention that the
debugging trail is evidence, not clutter to hide:

1. **Adaptive-baseline cold-start false positive** (found during V0, fixed
   then, preserved here): a variance estimate built from a handful of
   samples is nearly zero, making ordinary noise look like many standard
   deviations away. Fixed with a warm-up sample count before the baseline is
   trusted to judge anything.
2. **`sensor_lockup` detector had no BOOT guard**, found while testing the
   new detector directly: a test using perfectly static (non-jittered)
   fixture values tripped "identical value repeated N times" during boot
   itself. This is the *same class* of bug as (1) -- a window/history-based
   detector trusted before it had a settled baseline -- caught this time by
   applying the lesson deliberately rather than by accident.
3. **`_update_comms_loss` lost its BOOT guard during the extraction
   refactor**: "no ground-station client connected yet" was read as
   `COMMS_LOSS` from the very first tick, before boot even completed,
   because the guard present in the original code wasn't carried over.
   Caught by a live end-to-end TCP test immediately after the refactor, not
   by unit tests alone -- worth noting as an argument for always running the
   real integration path, not just isolated module tests.

## Open questions deliberately deferred, not decided

Per instruction, these are recorded rather than guessed at:

- **Exact thermal thresholds** (`fdir/config.py`'s `THERMAL_CRITICAL_LOW_C`/
  `HIGH_C`) are reasonable bench-scenario placeholders, not measured values.
  Revisit once real hardware exists to characterize actual thermal behavior.
- **Which IMU channels drive lockup detection** currently uses all six
  accel/gyro channels as one fingerprint. Once real sensor noise
  characteristics are known, this may need per-channel tuning (a real IMU's
  noise floor differs from this simulation's).
- **Whether `ADAPTIVE_ANOMALY`/`ML_ANOMALY` should ever gain conditional SAFE
  authority** (e.g. only when corroborated by a second signal) is explicitly
  left open -- see decision 3 above. Not a Phase 1 decision.
- **Packaging**: cross-directory imports currently use an explicit
  `sys.path.insert` pattern (consistent across `fdir/`, `simulator/`,
  `ground-station/`), not a proper installable package via `pyproject.toml`.
  Working and consistent, but a reasonable future cleanup, not a Phase 1
  necessity.
- **CRC32 C compatibility**: `firmware/inc/telemetry_protocol.h` documents
  that its checksum must match Python's `zlib.crc32` bit-for-bit (standard
  CRC-32/ISO-HDLC) but this has not been cross-validated against a real C
  implementation, since there's no hardware or C toolchain run to test it
  against yet. Flagged as a concrete early V1 task.
- **Exact STM32 board pin/peripheral assignments** await the physical board;
  no CubeMX-generated project exists yet, deliberately.
