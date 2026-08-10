# CubeSat-Inspired Avionics & Mission Control Testbed

A spacecraft flight computer that watches its own health, tells the ground the truth
about it, and puts itself somewhere safe when something goes wrong. Built on a desk,
not in orbit.

## What this actually is

Real satellites have a small computer onboard that spends its entire life checking on
itself. Temperature, power draw, orientation, whether its sensors are still answering.
It streams that down to a team on the ground who watch it on a screen and send commands
back up. And because nobody can fly up and fix it, it has to notice its own problems and
put itself into a safe state until someone works out what happened.

That last part is the bit I actually care about. Anyone can make a sensor print numbers
to a screen. The interesting question is what the thing does at 3am when a sensor stops
answering and there's no one watching.

So this is that system, on a bench. An STM32 plays the flight computer, wired to real
sensors, streaming telemetry to a Python ground station that can command it back. Then I
break it on purpose and see whether it notices.

**It is not flight hardware.** Nothing here goes to space, and nothing has been through
vibration, thermal, or EMI qualification. It's a testbed — a stand-in that lets me build
and defend the same reasoning without needing a rocket. I'd rather say that plainly at
the top than let anyone read further and assume otherwise.

The thing I'm actually trying to prove is the one that took me longest to learn: a system
isn't good because it works. It's good because you can show, afterwards, that it was
checked properly.

## Where it is right now

No hardware yet — the board and sensors are on order. Everything below runs in
simulation, and I've been deliberate about labelling what that does and doesn't prove.

| | State | What that means |
|---|---|---|
| Simulator, ground station, fault injection | **Simulated** | Runs end-to-end over a live TCP link. Telemetry, telecommands, acknowledgements, CSV logging, fault injection, recovery. |
| Deterministic FDIR engine (`fdir/`) | **Simulated** | Pulled out into its own hardware-agnostic package. 154 automated tests covering debounce timing, mode transitions, SAFE-mode recovery gating. |
| Anomaly detection (`ml/`) | **Trained** | An Isolation Forest trained on synthetic nominal telemetry and evaluated against held-out episodes. Synthetic data only. |
| Firmware (`firmware/`) | **Not built** | Planning docs and hand-written C structs matching the wire protocol. Never compiled, never flashed. |
| Anything at all | **Hardware-tested** | No. Not one line. There is no board yet. |

Those four words — Simulated, Trained, Hardware-tested, Experimentally-validated — are
used the same way everywhere in this repo, including as a column in the requirements
spec. If something claims to be tested, you should be able to find the test.

## The FDIR layer, and why the ML doesn't get to drive

There are two things watching for faults here, and they have deliberately unequal
authority.

The **deterministic layer** does the boring, predictable work: fixed thresholds, debounce
windows, a documented state machine. If the bus voltage sits below 4.0 V for longer than
its persistence window, the system goes to SAFE. That's it. No cleverness, and none
wanted — this is the layer I'd have to defend line by line if something went wrong.

The **ML layer** is an advisor. It can raise a flag, that flag shows up in telemetry and
in the logs, and that is the entire extent of its power. It cannot change the
spacecraft's mode. This isn't a policy I wrote down and hoped everyone would remember —
it's one line in `fdir/engine.py`:

```python
SAFE_MODE_TRIGGER_FLAGS = (
    FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY | FaultFlag.SENSOR_LOCKUP
)
```

`ML_ANOMALY` isn't in that list, and there's a test that fails if anyone ever adds it.

I'm interested in where machine learning is genuinely useful here, not in putting "AI" in
front of a project and hoping nobody asks. So I measured it, and the answer is properly
mixed:

- **`sensor_lockup` — the model is excellent.** Flags fault samples at **91×** the nominal
  rate. A frozen IMU drives six rolling standard deviations to exactly zero at once, which
  is a corner of feature space the training data never visits. A fixed threshold would
  never catch this, because every value stays perfectly in range. It just stops moving.
- **`gradual_drift` — real signal, weak.** Worth knowing: the adaptive statistical baseline
  catches **0%** of these, and that's not a bug. An EWMA continuously updates what it thinks
  normal is, so a slow enough drift just gets absorbed as the new normal. A model trained
  once and frozen doesn't have that blind spot. But at 3.6× the nominal rate it's a
  direction, not a detector I'd depend on yet.
- **`sensor_timeout` — the model is completely blind.** Flags it at 0.9× the nominal rate,
  which is to say worse than chance. The reason is worth more than the result: the only
  signature is a flag that is *constant* in nominal training data, so no tree ever splits
  on it. **0 splits out of 3089 nodes.** The model can only be surprised along axes that
  actually varied while it was learning. More trees will not fix this.

That last one is the most useful thing the evaluation told me, and it's an argument *for*
the two-layer design rather than against it. The deterministic layer catches
`sensor_timeout` at 100% in 0.2 s, because checking whether a sensor answered needs no
training data at all. The two layers are blind in different places. That's measured, not
assumed.

The anomaly score is a path-length statistic. It is **not** a probability, and I don't
present it as one anywhere.

Full numbers, including the false-positive rates and a fairly long list of things
synthetic data can't tell me: [`docs/architecture/ml-evaluation-report.md`](docs/architecture/ml-evaluation-report.md).

## Running it

Python 3.10+. `pip install -r requirements.txt`.

**The simulator and ground station** — two terminals:

```bash
python simulator/run_simulator.py
```

```bash
streamlit run ground-station/dashboard.py
```

The dashboard connects over a local TCP socket (127.0.0.1:5555), shows live telemetry and
mode/fault state, sends telecommands, and logs everything to a timestamped CSV in `data/`.
One connection at a time — a second one gets rejected rather than silently stealing the
link, which is how the real thing would behave.

At the simulator's own prompt you can break things:

```
fault undervoltage | thermal | sensor_timeout | sensor_lockup | gradual_drift | clear
reboot | status | quit
```

That prompt is deliberately *not* on the ground-station link. The link is the spacecraft's
real interface; stdin here is me walking over and unplugging something.

**The tests:**

```bash
pytest
```

**The ML pipeline** — no hardware needed, it runs entirely against the simulator:

```bash
python simulator/dataset_gen.py    # 69,000 labelled samples, ~2 seconds
python ml/train.py                 # Isolation Forest, nominal data only
python ml/evaluate.py              # writes the evaluation report
python ml/export_embedded.py       # exports the model to a C header
```

Everything is seeded. Same seed, same telemetry, same numbers — otherwise there'd be no
point scoring a detector against it.

## Layout

```
simulator/          Spacecraft physics, fault injection, telemetry, TCP server
fdir/               Deterministic fault detection engine. No sockets, no threads, no ML
ml/                 Dataset generation, features, training, evaluation, embedded export
ground-station/     Python mission-control dashboard
firmware/           STM32 C. Planning and protocol headers only, nothing built
tests/              154 tests
docs/requirements/  System requirements, each with an ID and a verification method
docs/architecture/  Block diagram, mode state machine, engineering decisions, ML report
docs/interfaces/    Telemetry and command dictionaries — the byte-level contract
data/               Captured telemetry
hardware/           Schematics and photos. Empty, for now
```

Worth reading first, if you're trying to work out what I was thinking:
[`docs/architecture/phase0-1-engineering-decisions.md`](docs/architecture/phase0-1-engineering-decisions.md)
— what got built, what got changed, what got rejected, and the questions I decided not to
answer yet.

## Roadmap

| | |
|---|---|
| **V0** ✅ | Software only. Synthetic telemetry, ground station, FDIR, ML pipeline. |
| **V1** | STM32 reads real sensors and streams real telemetry over UART. |
| **V2** | Telecommands, SD logging, watchdog, wireless link. |
| **V3** | Verification campaign. Fault injection with real evidence, and the numbers to back it. |
| **V4** | Maybe: custom PCB, FreeRTOS, redundant sensors, hardware-in-the-loop. |

Each one has to work before the next gets started. I've skipped ahead on projects before
and it always cost more time than it saved.

## Still open

Because it would be strange to write all of the above and imply it's finished:

- Every threshold and debounce window is a design target I picked. None are measured. They
  get revisited the moment real hardware exists to characterise.
- The C header the model exports has never been compiled. No toolchain here, no board yet.
- The CRC32 in the firmware header is documented to match Python's `zlib.crc32` but hasn't
  been cross-checked against a real C implementation. First job when the board arrives.
- The simulator models sensor noise as clean Gaussian. Real sensors are not that polite.
- Whether the ML layer should ever get more authority than "raise a flag" is genuinely
  open. I'm not deciding it on synthetic data.

If something looks unfinished, it probably is.
