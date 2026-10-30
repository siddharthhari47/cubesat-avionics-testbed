# V1 bench run: real telemetry through FDIR and ML #1

30 Oct 2026. NUCLEO-F446RE, ICM-20948, TMP102, INA226 on a breadboard at rest,
USB serial to a PC at 115200 baud. Logs are in `data/bench/`.

FDIR and ML ran on the PC, driven by the firmware's timestamps. The board only
measures and transmits. No FDIR runs on the STM32 yet, so nothing here measures
onboard FDIR.

## Link

| Measure | Result |
|---|---|
| Packets | 4855 in the main run, 221 in a short earlier run |
| CRC or framing failures after first packet | 0 |
| Lost by `seq_num` gap | 0 |
| Period, commanded 100 ms | mean 100.00, std 0.11, range 98 to 102 |

Wired USB only. This run lasted about 8 minutes, so it is not an endurance figure.

## IMU dropout

The SDA wire was pulled by hand at the IMU. Five pulls were intended and seven
dropouts were logged, lasting 0.7 to 6.7 s. Two are probably contact bounce or
re-seating: one ended about 2 s before the next began. The trial count is
therefore seven dropouts, not seven clean independent pulls.

- Seven of seven latched `SENSOR_TIMEOUT`.
- Latency from the fault to the flag was between 100 and 198 ms. The range exists
  because the pull happened at an unknown moment between two samples. Resolution
  is one 100 ms tick, so this is close to the tick-rate limit by construction.
- TMP102 and INA226 kept reporting through every dropout, so the bus stayed up.
- The mode stayed NOMINAL, as designed: `SENSOR_TIMEOUT` carries no SAFE authority.
- Recovery proposals were not executed. There is no executor on the bench.

## Bench changes to FDIR

Applied in `ground-station/serial_bridge.py`, not in `fdir/`:

- Undervoltage limits moved to the 3.3 V rail at the same ratios as the 5 V design.
- The magnetometer is declared not fitted. It fails at boot and sends zeros, and
  without this every IMU fault also latches a false `DATA_PATH_SUSPECT`.

## ML #1 (advisory only)

| Model | Sample set | Flagged |
|---|---|---|
| `isolation_forest_v1` (simulated data) | all healthy-sensor samples | 4569 / 4569 |
| bench model, fitted to 900 samples | separate 22 s run | 1 / 217 |
| bench model | 324 s after the last dropout | 113 / 3240 (3.5%) |
| bench model | 7 s before the first pull | 32 / 70 |
| bench model | between pulls | 161 / 335 |
| bench model | during dropouts | 282 / 282 |

The v1 model is not usable on this board: it flags everything, because its normal
is a simulated 5 V spacecraft.

The bench model separates dropouts, but that says little. A dropout zeroes the IMU
channels and clears `imu_responded`, which the deterministic check already sees, so
ML added no detection here. Its flags before the first pull and between pulls line
up with `accel_z` dipping to 0.825 g, which is probably a hand on the board. That is
a guess from the data, not a controlled test. Its false-alarm rate on a board at rest
was 3.5% over one session, against about 1% expected from its contamination setting.
The fit covers one board, at rest, in one 90 s window.

## Defects found

- `ADAPTIVE_ANOMALY` latched falsely at 337 s on a steady 3.31 V. The INA226 reads
  in 1.25 mV steps, so the EWMA variance gets small enough for one step to look like
  several sigma. It has no authority, but its limit was tuned on simulated noise.
- `UNKNOWN_ANOMALY` latches whenever ML does, so it inherits ML's false alarms.

## Not shown

- Recovery, SAFE entry from a real fault, or any detection beyond the IMU timeout.
- Magnetometer: still not responding.
- Current reading against a multimeter.
- The stuck-bus recovery path, since the bus never stuck.
