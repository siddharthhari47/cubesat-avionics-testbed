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
| Deterministic FDIR engine (`fdir/`) | **Simulated** | Its own hardware-agnostic package — detectors, diagnosis, bounded recovery campaigns, degraded modes. 437 automated tests. |
| Fault-injection scenarios (`scenarios/`) | **Simulated** | 17 scenarios, run both in ground contact and out of it, each with negative assertions. Every one detected and correctly diagnosed; the numbers are below. |
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

One number moved a lot when I made the evaluation honest. The report used to score the
model without the debounce the deployed system actually applies — so it was measuring a
detector that doesn't exist. With the debounce in, recall drops across the board
(thermal 1.00 → 0.47) and the false-alarm rate falls off a cliff: **100% of episodes
→ 2.5%.** The old recall figures were not achievable by the thing I'd actually built.

Full numbers, including the false-positive rates and a fairly long list of things
synthetic data can't tell me: [`docs/architecture/ml-evaluation-report.md`](docs/architecture/ml-evaluation-report.md).

## The one measurement that decided a purchase

I wanted the hardware list to be justified rather than assumed, so the scenario suite
runs some faults twice — once with per-rail current sensing available to the FDIR
layer, once with it blinded. Same fault, same seed, same everything else:

| | Detection | Diagnosis |
|---|---|---|
| radio latch-up, **with** per-rail current | **0.70 s** | `RADIO_LATCHUP` ✅ |
| radio latch-up, **without** | 5.10 s | `GROUND_LINK_LOST` ❌ |

Seven times faster and correct, versus slow and wrong. A latch-up and a merely quiet
radio look identical on the link itself — the current draw is the only thing that
separates them. That's the argument for the INA219 on the parts list, and I'd rather
have it as a measurement than as an opinion.

The blinded halves of those pairs stay in the suite permanently. Delete them and it
goes back to being an opinion.

## I went looking for bugs in my own work, and found forty-two

The line at the top of this README — that a system is good because you can show it was
checked, not because it works — felt a bit cheap to write and then not act on. So I ran
adversarial passes over the FDIR core, attacking each safety property I'd claimed, with
probes that execute rather than by re-reading code I'd already convinced myself about.

That is now **ten rounds and forty-two defects, all fixed.** The first two rounds found
ten. Three of those were serious:

- **A stuck flag meant a permanently confident wrong diagnosis.** `DATA_PATH_SUSPECT`
  was in neither of the two sets that let a flag be cleared, so one transient bus glitch
  latched it forever — and since the diagnosis layer checks that flag *first* by design,
  every later diagnosis came back "data path fault" at high confidence on a completely
  healthy vehicle, hiding real faults underneath. That's the exact failure mode the
  module was written to prevent, reintroduced through a latch that couldn't clear.
- **NaN counted as proof a fault had cleared.** Every comparison with NaN is false, so a
  NaN bus voltage skipped the undervoltage check and got recorded as evidence the
  condition had gone away. A vehicle correctly held in SAFE could be returned to service
  on readings that meant nothing.
- **The spacecraft couldn't notice a silent link failure.** "Connected" was defined as
  "a socket object exists", which stays true indefinitely when the other end vanishes —
  so the comms-loss flag, the only thing that can trigger the radio recovery ladder,
  could never fire during exactly the failure that ladder was built for.

All ten are fixed and pinned by regression tests. Two things I'd rather say out loud:
fixing one of them exposed a test that had been passing *because* of the bug it was
supposed to catch, and a second uncovered that a 5-second debounce had never once
actually run in any test or scenario. That happened four separate times — a latching
flag being read as if it were live state.

**Eight more rounds followed, and the interesting part is what changed about the
findings rather than how many there were.**

- **A fix is a code change like any other, and mine have a high defect rate.** Round 4
  found three of six defects inside that morning's fixes. Round 5 went four for four.
  Each of those fixes had been verified against the defect it targeted and shipped
  without anyone asking what *else* moved.
- **After two or three instances of the same shape, stop fixing instances.** `capability`
  is no longer asserted anywhere; it's *derived* from confirmed rail state, so the engine
  cannot claim a configuration the hardware isn't in. The test asserts the property, not
  the three cases.
- **The worst defect wasn't logic.** `run_simulator.py` had never constructed a
  `RecoveryExecutor` — autonomous recovery had never once executed on the live path,
  only in the scenario harness. That was the third instance of *built, tested somewhere,
  never wired into the real thing.*
- **Round nine found the harness lying.** Every scenario had silently assumed the
  spacecraft was in ground contact, which is not how a CubeSat spends most of an orbit.
  Running them all out of contact too, ten of fifteen behaved differently and the nominal
  control broke its own negative assertion — including runs reported as **recovered**
  because a comms campaign succeeded while the injected fault was still latched.
- **Then I stopped guessing where to look and measured it.** Coverage said the two
  least-tested files were the transport (17%) and the flight path (35%), and neither had
  ever been a round's subject. The biggest thing in there: **fault detection latency was
  a function of the telemetry downlink rate.** The whole FDIR engine ran from inside the
  telemetry loop, so `SET_TELEMETRY_RATE` — a *comms* command any operator can send — set
  how fast the spacecraft noticed faults. 20× across its legal range, and the shipped
  default was 10× slower than every latency I'd published. Nine rounds of judgement
  walked past that; one coverage report found it.

The write-up, round by round, including the concerns that measurement *refuted*:
[`docs/architecture/v0-adversarial-safety-review.md`](docs/architecture/v0-adversarial-safety-review.md).

What this still doesn't establish: it is mostly my own code reviewed by me. The one
independent pass found six defects in code written that same day, and the honest reading
of ten rounds is that the rate of new findings has not yet gone to zero.

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
fault undervoltage | thermal | sensor_timeout | sensor_lockup | gradual_drift
      | radio_latchup | radio_unresponsive | rail_overcurrent | data_bus_failure
      | sensor_corruption | communication_loss | unexplained_transient | clear
reboot | status | quit
```

That prompt is deliberately *not* on the ground-station link. The link is the spacecraft's
real interface; stdin here is me walking over and unplugging something.

**The tests:**

```bash
pytest
```

**The scenario suite**, which is where the numbers in this README come from:

```bash
python scenarios/runner.py
```

It exits non-zero if any negative assertion is violated, so it can gate a build. The
negative assertions matter more than the positive ones — most of the spacecraft
failures I read about weren't missed detections, they were the system confidently doing
the wrong thing.

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
fdir/               Deterministic FDIR engine. No sockets, no threads, no ML
scenarios/          Fault-injection scenarios with measured outcomes
ml/                 Dataset generation, features, training, evaluation, embedded export
ground-station/     Python mission-control dashboard
firmware/           STM32 C. Planning and protocol headers only, nothing built
tests/              437 tests
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
- The degraded modes are the clearest case of that. The requirement says capability sets
  should be *pre-validated*, and pre-validated means measured — nobody has put a meter on
  a rail and confirmed the minimal configuration actually closes its energy balance. The
  mechanism works and is tested; the power budgets are estimates, flagged as such in code
  with a test that fails if anyone quietly upgrades them to facts.
- Autonomous degradation is also the one thing here I can't point at a real mission for.
  BIRD survived losing most of its attitude control by running reduced — but a human on
  the ground decided that. Nothing I read did it by itself. So that part is research
  rather than copying something proven.
- The C header the model exports has never been compiled. No toolchain here, no board yet.
- The CRC32 in the firmware header is documented to match Python's `zlib.crc32` but hasn't
  been cross-checked against a real C implementation. First job when the board arrives.
- The simulator models sensor noise as clean Gaussian. Real sensors are not that polite.
- Whether the ML layer should ever get more authority than "raise a flag" is genuinely
  open. I'm not deciding it on synthetic data.
- Every scenario in the suite is one I chose and modelled. It detects everything it was
  built to detect, which proves the detectors work and proves nothing about whether
  they're sufficient. The research that shaped this project found that 63% of catalogued
  CubeSat failures have no stated technical cause at all — I can't build a scenario for
  those, and I've tried not to let a clean results table imply otherwise.

If something looks unfinished, it probably is.
