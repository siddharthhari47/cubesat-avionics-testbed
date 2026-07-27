# CubeSat-Inspired Avionics & Mission Control Testbed

A benchtop spacecraft avionics testbed — not flight hardware. No vibration, thermal,
or EMI qualification.

**Status: V0, Week 1 — repository scaffolding only. No firmware, no simulator, no
requirements written yet.**

Mission statement, system requirements, and architecture diagrams are the next
deliverables and will replace this section as they're written.

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
