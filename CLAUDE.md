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

**Phase: V0, Week 1.** Repository skeleton exists. No hardware purchased. No firmware
written. Immediate deliverables in order:

1. Mission statement (one paragraph, plain language) → README
2. 15–25 system requirements with IDs → `docs/requirements/`
3. Top-level block diagram → `docs/architecture/`
4. Operating modes and state-transition diagram (BOOT, NOMINAL, SAFE, TEST)
5. Telemetry dictionary → `docs/interfaces/`
6. V0 Python telemetry simulator + basic ground station dashboard

Hardware is ordered only after V0 works.

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

## Planned hardware (not yet purchased)

STM32 Nucleo-class board · ICM-20948-class IMU · LIS3MDL-class magnetometer ·
digital temperature sensor · INA219/INA226 power monitor · SPI microSD module ·
USB-UART adapter (3.3 V) · logic analyzer · multimeter. Wireless module and battery
system selected only after V1 is validated.

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
