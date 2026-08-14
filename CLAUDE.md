# CubeSat-Inspired Avionics & Mission Control Testbed

## What this project is

A benchtop spacecraft avionics testbed: an STM32 "flight computer" that reads real
sensors, packages structured telemetry, logs to microSD, and streams to a Python
ground-station dashboard that can send telecommands back. The differentiating layer
is FDIR — autonomous fault detection, isolation, and recovery, with a SAFE mode.

It is a **ground testbed**. Nothing flies. No vibration, thermal, or EMI qualification.
Say so plainly in documentation; do not let language drift toward implying flight
hardware.

This is a portfolio project as much as a technical one. The success criterion is that
another engineer can read the repo and determine what the system was meant to do, how
it was architected, which requirements were verified, what failed, and how faults were
handled. A working demo backed by engineering evidence — not just a working demo.

## Current state

**Phase: V0 complete, pre-hardware.** All six original V0 deliverables are done —
mission statement, SRS, block and mode diagrams, telemetry/command dictionaries,
simulator and ground station. Hardware is ordered and has not arrived, so no firmware
is written and nothing here has run on a real board.

Built since, in order (`docs/architecture/v0-gap-analysis-and-plan.md` is the plan
these follow):

- `fdir/` — a hardware-agnostic decision engine. `FDIREngine.tick()` is a pure
  function of state; it holds no sockets, threads, or ports.
- Deterministic detectors with debounced, latching flags, each traced to an SRS ID.
- `fdir/diagnosis.py` — symptom → cause, with `UNKNOWN` as a first-class answer.
- `fdir/recovery.py` + `executor.py` — bounded, verified, escalating recovery
  campaigns whose state survives a reset.
- `simulator/environment.py` — a physical state model (rails, battery, thermal
  nodes, bus topology). Signals are derived from state, never scripted.
- `scenarios/runner.py` — 17 fault-injection scenarios including blinded
  discrimination pairs, with negative assertions that gate the build. Every
  scenario runs both in ground contact and out of it; out of contact is the
  normal orbital state and two review rounds hid in the gap where it wasn't
  modelled.
- ML #1 as an advisory anomaly detector, streaming, with an exported C header.
- Ground-station event timeline that renders flags by what authority they carry.
- `fdir/degraded.py` — `Mode.DEGRADED` and a capability ladder (R8), with
  capability **derived** from confirmed rail state rather than asserted.
- Fixed commissioning reference and drift detection, persisted across reset (R7).

**420 tests.** Nine adversarial safety-review rounds are complete
(`docs/architecture/v0-adversarial-safety-review.md`): **38 findings, all fixed**.
Read §14 and the "standing" section before adding a component — the recurring
defects are not novel, and three of them repeat: a latched flag read as live
state, state read before its writer runs this tick, and a component that works,
is tested, and was never wired into the production path.

**The governing principle, and it is enforced in code rather than described:**
ML detects. FDIR decides. Recovery executes. FDIR verifies. Hardware safety
constrains everything. `ML_ANOMALY` and `ADAPTIVE_ANOMALY` appear in neither
`SAFE_MODE_TRIGGER_FLAGS` nor `RECOVERY_AUTHORITY_FLAGS`, and tests fail if either
bit is ever added.

**What is deliberately not built:** ML #2 — the case study concludes it is not
justified on current evidence, so a clean interface seam exists and nothing more.
Recorded in `docs/requirements/case-study-traceability.md`. Do not quietly
implement it without revisiting that reasoning. R7 (fixed-reference drift) and R8
(degraded modes) were the other two gaps and are now closed; all 11 case-study
requirements are met.

**R8's caveat must travel with it:** "pre-validated" means measured, and nothing
here has been measured. Capability power budgets are declared estimates —
`declared_only=True` on every set, with a test that fails if it is flipped
without updating the traceability doc.

**The one known design gap, recorded as a scenario rather than a footnote:**
the system cannot distinguish expected silence between ground passes from
anomalous silence. `healthy but out of view` is a healthy vehicle that opens a
comms recovery campaign, which is R5 working exactly as specified. Closing it
needs a notion of an expected contact gap — a V1 design question, not a patch.

Next milestone is V1, which is blocked on hardware arriving.

## Engineering phases

Layered under the V0-V4 version roadmap below, the software-side work itself
follows Phase 0 (define: requirements, interfaces, architecture) then Phase 1
(build the software, ML, and FDIR foundation, before hardware arrives). Phase
2 is deliberately undefined -- it depends on what Phase 1 actually teaches
once finished, not a plan drafted in advance.
`docs/architecture/phase0-1-engineering-decisions.md` is the canonical record
of why the `fdir/` extraction and the ML architecture decisions were made;
read it before re-deriving them from git history or first principles.

## Version roadmap

| Version | Scope |
|---|---|
| V0 | Software only. Synthetic telemetry generated in Python, displayed in ground station. |
| V1 | STM32 reads real sensors, streams telemetry over USB/UART. |
| V2 | Telecommands, acknowledgements, SD logging, FDIR, modes, wireless link. |
| V3 | Verification campaign, fault injection, documentation, demo video. |
| V4 | Optional: custom PCB, FreeRTOS, CAN, redundant sensors, HIL simulation. |

Each version must work before the next layer is added. Do not skip ahead.

## The five numbers this project must produce

Every measurable claim on the CV traces to one of these. Test campaigns are designed
to produce them; if a design decision makes one of them unmeasurable, that is a
problem worth raising.

1. **Fault detection latency** — time from injected fault to flag raised (ms)
2. **Telemetry rate accuracy** — commanded vs. actual rate, and jitter
3. **Packet loss vs. range** — percentage at measured distances, wired and wireless
4. **Power consumption** — draw per mode (NOMINAL vs. SAFE)
5. **Endurance** — longest continuous run without fault or reset

Do not write unquantified capability claims into the README or CV. "Implemented fault
detection" is worthless; "detected sensor dropout within 250 ms across 40 injection
trials, zero false positives over 6 h" is the whole point.

## Conventions

**Requirement IDs** — `SYS-###` (system), `FSW-###` (flight software), `COM-###`
(communications), `GS-###` (ground station), `FDIR-###` (fault management). One
testable statement each, with a number in it wherever a number is possible. Every
requirement maps to a verification method: inspection, analysis, demonstration, or test.

**Telemetry packets** — every packet carries: packet ID, timestamp, sequence number,
payload length, data fields, integrity check. Documented in the ICD before firmware
implements it. Protocol is defined in V0 and hardware conforms to it, not the reverse.

**Faults** — each fault is specified before it is implemented: detection condition,
severity, persistence rule, operator indication, autonomous response, recovery
condition, log entry. SAFE mode behavior is documented, never improvised.

**Commits** — small, frequent, honest. Commit at the end of every work session even
when nothing works. `WIP: IMU returns garbage on I2C read, suspect pull-ups` is
valuable evidence. Never squash the development history into a clean final commit;
the chronological trail of debugging is a deliverable, not clutter.

**Documentation proportionality** — docs must not outgrow working code. Fourteen
polished documents wrapped around 800 lines of firmware reads as paperwork, not
engineering. If a doc describes something not yet built, mark it clearly as planned.

## Repository layout

```
firmware/           STM32 embedded C/C++
ground-station/     Python mission-control application
simulator/          V0 synthetic telemetry generator
fdir/               Deterministic FDIR decision engine, hardware-agnostic (no sockets/threads)
hardware/           Schematics, KiCad files, photographs
docs/requirements/  SRS, traceability matrix
docs/architecture/  Block diagrams, mode state machines
docs/interfaces/    ICD, telemetry dictionary, command dictionary
tests/              Test procedures, results, fault-injection evidence
data/               Captured telemetry logs
media/              Photos, demo video
```

## Hardware (ordered, not yet arrived)

STM32 Nucleo-class board · ICM-20948-class IMU · LIS3MDL-class magnetometer ·
digital temperature sensor · INA219/INA226 power monitor · SPI microSD module ·
USB-UART adapter (3.3 V) · logic analyzer · multimeter. Wireless module and battery
system selected only after V1 is validated.

The power monitor is the one part with a measured justification rather than an
assumed one. Same fault, same seed, differing only in whether per-rail current is
available to the FDIR layer:

| | Detection | Diagnosis |
|---|---|---|
| radio latch-up, **with** per-rail current | 0.70 s | `RADIO_LATCHUP` ✅ |
| radio latch-up, **without** | 5.10 s | `GROUND_LINK_LOST` ❌ |

7× faster and correct, versus slow and wrong. `docs/architecture/v0-scenario-results.md`
has the run. Keep the blinded halves of those scenario pairs — they are what makes
this a measurement rather than an assertion.

## Software stack

Embedded C/C++ with STM32CubeIDE and HAL/LL drivers. Python for the simulator,
ground station, parsing, and analysis. Git/GitHub. KiCad for schematics. Optionally
FreeRTOS at V4.

## Telecommand set

`PING`, `GET_STATUS`, `SET_TELEMETRY_RATE`, `ENTER_SAFE_MODE`, `EXIT_SAFE_MODE`
(with safeguards), `RESET_FAULTS`, `REQUEST_LOG`, `ENABLE/DISABLE` test functions.
Every command returns an acknowledgement or an explicit rejection.

## Working notes for Claude

- The author is an ECE undergraduate learning embedded systems as this is built.
  Explain reasoning rather than only producing code; the point is that he can defend
  every line in an interview.
- Prefer teaching the debugging approach over silently fixing things.
- Push back if a suggestion skips a version stage, adds hardware prematurely, or
  produces a claim that cannot be measured.
