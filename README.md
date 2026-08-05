# CubeSat-Inspired Avionics & Mission Control Testbed

**Status: V0, Week 1 — repository scaffolding only. No firmware, no simulator, no
requirements written yet.**

## Mission

This project is a benchtop, CubeSat-inspired avionics testbed built to demonstrate the
full chain of spacecraft flight-computer functions on the bench: acquiring real sensor
data, packaging it into structured telemetry, streaming it to a ground-station
dashboard, executing telecommands sent back from the operator, logging mission data,
and autonomously detecting and responding to faults through a dedicated SAFE mode. It
is not flight hardware — there is no vibration, thermal, or EMI qualification — and its
purpose is to produce a verifiable, requirements-driven engineering artifact rather
than a working demo alone.

System requirements and architecture diagrams are the next deliverables and will
replace this note as they're written.

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
