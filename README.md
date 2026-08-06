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

## Documentation

- Requirements: `docs/requirements/SRS.md`
- Architecture: `docs/architecture/block-diagram.md`, `docs/architecture/mode-diagram.md`
- Interfaces (ICD): `docs/interfaces/telemetry-dictionary.md`, `docs/interfaces/command-dictionary.md`

## Repository layout

```
firmware/           STM32 embedded C/C++
ground-station/     Python mission-control application
simulator/          V0 synthetic telemetry generator
hardware/           Schematics, KiCad files, photographs
docs/requirements/  SRS, traceability matrix
docs/architecture/  Block diagrams, mode state machines
docs/interfaces/    ICD, telemetry dictionary, command dictionary
tests/              Test procedures, results, fault-injection evidence
data/               Captured telemetry logs
media/              Photos, demo video
```

## Version roadmap

| Version | Scope |
|---|---|
| V0 | Software only. Synthetic telemetry generated in Python, displayed in ground station. |
| V1 | STM32 reads real sensors, streams telemetry over USB/UART. |
| V2 | Telecommands, acknowledgements, SD logging, FDIR, modes, wireless link. |
| V3 | Verification campaign, fault injection, documentation, demo video. |
| V4 | Optional: custom PCB, FreeRTOS, CAN, redundant sensors, HIL simulation. |
