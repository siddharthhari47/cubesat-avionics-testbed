# System Requirements Specification (SRS)

**Status:** V0, initial draft. Requirements below are targets the system is designed
against; they are not yet verified against real hardware. Timing values marked
*(target, TBD)* are placeholders set before any hardware exists and must be revisited
once V1 hardware is available to characterize actual behavior.

## Conventions

- **ID prefixes:** `SYS` (system), `FSW` (flight software), `COM` (communications),
  `GS` (ground station), `FDIR` (fault management).
- **Verification methods:** Inspection, Analysis, Demonstration, Test — per requirement.
- Requirements marked **(Planned)** describe capability not yet implemented and are
  included for traceability, not claimed as done.

## System (SYS)

| ID | Requirement | Verification |
|---|---|---|
| SYS-001 | The system shall consist of five subsystems: flight computer, sensor suite, electrical health monitoring, communications link, and ground-station software. | Inspection |
| SYS-002 | The system shall be documented throughout as a ground-based testbed with no flight qualification. | Inspection |
| SYS-003 | The flight computer shall complete boot and reach NOMINAL mode within 5 s of power-on, given no faults. *(target, TBD)* | Demonstration |
| SYS-004 | The system shall report power draw (bus voltage × current) per operating mode, with NOMINAL and SAFE mode draw characterized and compared. | Test / Analysis |

## Flight Software (FSW)

| ID | Requirement | Verification |
|---|---|---|
| FSW-001 | Flight software shall generate telemetry packets at a configurable rate, default 1 Hz, adjustable 0.5-10 Hz via `SET_TELEMETRY_RATE`. | Test |
| FSW-002 | Flight software shall maintain mission elapsed time from boot with 1 s resolution. | Test |
| FSW-003 | Flight software shall implement four operating modes — BOOT, NOMINAL, SAFE, TEST — with documented transition conditions. | Demonstration |
| FSW-004 | Flight software shall transition to SAFE mode within 500 ms of a fault meeting SAFE-mode criteria. *(target, TBD)* | Test |
| FSW-005 | Flight software shall sample each configured sensor at its assigned rate within +/-10% jitter. | Test |
| FSW-006 | Flight software shall reject malformed commands (unknown ID or failed checksum) with an explicit rejection response, never a silent drop. | Test |

## Communications (COM)

| ID | Requirement | Verification |
|---|---|---|
| COM-001 | Every telemetry packet shall contain packet ID, timestamp, sequence number, payload length, data fields, and an integrity check. | Inspection of ICD, Test |
| COM-002 | Every telecommand shall receive an acknowledgement or explicit rejection within 1 s of receipt. | Test |
| COM-003 | The system shall flag a communications-loss fault after 5 s of missed heartbeat. *(target, TBD)* | Test |
| COM-004 | Corrupted packets (failed integrity check) shall be counted in a telemetry field, not silently dropped. | Test |

## Ground Station (GS)

| ID | Requirement | Verification |
|---|---|---|
| GS-001 | The ground station shall display live telemetry with no more than one packet of display latency. | Demonstration |
| GS-002 | The ground station shall plot temperature, acceleration, angular rate, and bus voltage/current as live time-series. | Demonstration |
| GS-003 | The ground station shall log all received telemetry to a timestamped CSV file. | Test |
| GS-004 | The ground station shall provide a command console showing sent commands and their acknowledgement/rejection. | Demonstration |
| GS-005 | The ground station shall display current operating mode and active fault flags. | Demonstration |

## Fault Management (FDIR)

| ID | Requirement | Verification |
|---|---|---|
| FDIR-001 | Every fault shall be specified — detection condition, severity, persistence rule, operator indication, autonomous response, recovery condition, log entry — before implementation. | Inspection |
| FDIR-002 | Sensor non-response (I2C/SPI timeout) shall be detected within 200 ms of onset, counting only conditions that persist continuously for at least 50 ms (debounce, to reject single-sample glitches) before the fault flag is raised. *(target, TBD)* | Test |
| FDIR-003 | The system shall detect undervoltage at two severity thresholds (warning, critical); a critical reading shall persist continuously for at least 100 ms before the system enters SAFE mode. *(persistence window target, TBD)* | Test |
| FDIR-004 | All fault events shall be logged to microSD with timestamp, fault ID, and system state at time of fault. | Test |
| FDIR-005 | `EXIT_SAFE_MODE` shall require an explicit operator command; the system shall never exit SAFE mode automatically. | Test |
| FDIR-006 | For each telemetry channel, the system shall maintain a continuously-updated running mean and variance (or equivalent adaptive filter) and flag deviations exceeding a configurable number of standard deviations, in addition to fixed thresholds; a deviation shall persist across at least 3 consecutive samples before being declared a fault. *(persistence count target, TBD)* | Test |
| FDIR-007 **(Planned)** | The system shall run an offline-trained anomaly-detection model (e.g. Isolation Forest or lightweight autoencoder), trained on logged nominal and fault-injected telemetry and deployed as static on-device inference, to flag multi-channel anomalies not captured by FDIR-002/003/006. Persistence/debounce rules for this layer follow the same principle as FDIR-002/003/006 and will be defined alongside its implementation, not deferred indefinitely. This applies the approach described in [Anomaly Detection Using Deep Learning Respecting the Resources on Board a CubeSat](https://arc.aiaa.org/doi/10.2514/1.I011232) (AIAA) and [CubeSat on-board computer state-of-the-art](https://ieeexplore.ieee.org/document/10597570/) (IEEE) to this project's own hardware and fault-injection data — not a novel algorithm; the contribution is the requirements-traced implementation and measured comparison against FDIR-002/003/006. | Test (detection latency, false-positive rate vs. threshold-only baseline) |
| FDIR-008 | The system shall not enter SAFE mode due to a false fault detection more than once per 6 h of continuous nominal operation. *(target, TBD — verified by soak test in V3)* | Test |
