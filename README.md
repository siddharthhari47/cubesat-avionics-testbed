# CubeSat-Inspired Avionics & Mission Control Testbed

**Status: V0 complete.** Mission, requirements, architecture, and interfaces are
documented; the software-only telemetry simulator and ground-station dashboard both
run and have been tested end-to-end, including fault injection and recovery. No
hardware has been purchased and no firmware written — that's V1.

## Mission

This project is a benchtop, CubeSat-inspired avionics testbed built to demonstrate the
full chain of spacecraft flight-computer functions on the bench: acquiring real sensor
data, packaging it into structured telemetry, streaming it to a ground-station
dashboard, executing telecommands sent back from the operator, logging mission data,
and autonomously detecting and responding to faults through a dedicated SAFE mode. It
is not flight hardware — there is no vibration, thermal, or EMI qualification — and its
purpose is to produce a verifiable, requirements-driven engineering artifact rather
than a working demo alone.

## V0: running the simulator + ground station

Requires Python 3.10+, `streamlit`, and `pandas` (`pip install streamlit pandas`).

1. Start the flight-computer simulator (leave it running in its own terminal):
   ```
   python simulator/run_simulator.py
   ```
   At its prompt you can inject faults to test FDIR behavior: `fault sensor`,
   `fault undervoltage`, `fault drift`, `fault clear`, `reboot`, `status`, `quit`.
   See `docs/architecture/mode-diagram.md` for what each should trigger.

2. In a second terminal, start the ground station:
   ```
   streamlit run ground-station/dashboard.py
   ```
   It connects to the simulator over a local TCP socket (127.0.0.1:5555 by default,
   configurable in the sidebar), shows live telemetry and mode/fault status, sends
   telecommands from the command console, and logs everything to a timestamped CSV
   under `data/`.

V0 supports one ground-station connection at a time, matching the real spacecraft
model — a second connection attempt is rejected, not silently swapped in.

## Running the test suite

The deterministic FDIR engine (`fdir/engine.py`) was extracted from the original
simulator into its own hardware-agnostic package and now has an automated `pytest`
suite (`tests/`) covering wire-protocol pack/unpack round-trips, environment
fault-injection behavior, and the FDIR engine's debounce timing, mode transitions,
and SAFE-mode recovery gating, including an end-to-end integration scenario against
`simulator/run_simulator.py`.

Install `pytest` if you haven't (`pip install -r requirements.txt` covers it), then
from the repo root:
```
pytest
```

## Running the ML anomaly-detection pipeline

No hardware needed -- the full pipeline runs against the simulator. From the repo root:
```
python simulator/dataset_gen.py          # generates data/datasets/phase1_dataset.csv
python ml/train.py                       # trains ml/models/isolation_forest_v1.joblib
python ml/evaluate.py                    # writes docs/architecture/ml-evaluation-report.md
python ml/export_embedded.py             # writes firmware/inc/anomaly_model.h
```
Read `docs/architecture/ml-evaluation-report.md` for what the model actually
catches, what it misses, and its false-positive rate compared to the
deterministic FDIR engine on the same held-out data -- not just a headline
number.

## Documentation

- Requirements: `docs/requirements/SRS.md`
- Architecture: `docs/architecture/block-diagram.md`, `docs/architecture/mode-diagram.md`
- Interfaces (ICD): `docs/interfaces/telemetry-dictionary.md`, `docs/interfaces/command-dictionary.md`

## Repository layout

```
firmware/           STM32 embedded C/C++
ground-station/     Python mission-control application
simulator/          V0 synthetic telemetry generator
fdir/               Deterministic FDIR decision engine, hardware-agnostic (no sockets/threads)
ml/                 Anomaly-detection pipeline: features, training, evaluation, embedded export
hardware/           Schematics, KiCad files, photographs
docs/requirements/  SRS, traceability matrix
docs/architecture/  Block diagrams, mode state machines
docs/interfaces/    ICD, telemetry dictionary, command dictionary
tests/              Test procedures, results, fault-injection evidence
data/               Captured telemetry logs
media/              Photos, demo video
```

## Current status

**Simulated and tested:** the V0 telemetry simulator, ground-station dashboard, and
fault injection (`simulator/environment.py`) run and have been exercised end-to-end
over a live TCP link, including telecommand round-trips and CSV logging. The
deterministic FDIR engine has since been pulled out into its own hardware-agnostic
package (`fdir/engine.py`) and is covered by an automated `pytest` suite exercising
debounce timing, mode transitions, and SAFE-mode recovery gating against synthetic
data. All of this is **Simulated**, not **Hardware-tested** — no STM32 board or
sensor exists yet, so nothing here has been run against real hardware or
**Experimentally-validated** in that sense.

**Trained (not hardware-tested):** a labeled synthetic dataset
(`simulator/dataset_gen.py`), an Isolation Forest anomaly detector trained on it
(`ml/train.py`), and a full evaluation comparing it against the deterministic FDIR
engine on held-out episodes (`ml/evaluate.py` →
`docs/architecture/ml-evaluation-report.md`) — per-fault-type recall and detection
latency, false-positive rates, and an explicit statement that the anomaly score is
not a probability. `fdir/engine.py` consumes this as an advisory input
(`FaultFlag.ML_ANOMALY`, bit 6) that can never by itself force a mode transition —
see `FDIR-007` in the SRS. The trained model has also been exported to a C header
(`ml/export_embedded.py` → `firmware/inc/anomaly_model.h`) for eventual embedded
inference, though it has not been compiled or run (no C toolchain in this
environment, no hardware to run it on yet).

**Not yet built:** everything under `firmware/` beyond documentation and
hand-written protocol-matching C structs/headers — no CubeIDE project exists, and
nothing has been compiled or run on real hardware.

## Version roadmap

| Version | Scope |
|---|---|
| V0 | Software only. Synthetic telemetry generated in Python, displayed in ground station. |
| V1 | STM32 reads real sensors, streams telemetry over USB/UART. |
| V2 | Telecommands, acknowledgements, SD logging, FDIR, modes, wireless link. |
| V3 | Verification campaign, fault injection, documentation, demo video. |
| V4 | Optional: custom PCB, FreeRTOS, CAN, redundant sensors, HIL simulation. |
