# V0 Autonomous Immune System — Gap Analysis and Implementation Plan

**Status:** planning only. No implementation code has been written or modified. The
154-test suite is untouched and passing.

**Method.** Ten parallel read-only audits were commissioned (architecture, ML, FDIR
capability, simulator, tests, observability, hardware abstraction, case-study
traceability, fault-injection design, adversarial safety). **Nine returned; the
adversarial safety review failed on an account spend limit.** Its role was partly
covered by direct verification: four safety defects below were reproduced by me
personally rather than accepted on an agent's word, and two independent agents
converged on a fifth.

Findings are labelled **VERIFIED** (I reproduced it), **AUDIT** (one agent, unverified),
or **CONVERGENT** (two or more agents independently).

---

## 1. Headline

**The existing system is a well-built detector with an excellent authority boundary, and
almost nothing else V0 requires.** Of the seven capabilities in the target architecture —
detection, validation, diagnosis, isolation, recovery, verification, escalation —
**detection is genuinely mature, one is vestigial, and five are absent.**

The single load-bearing line of autonomous response in the entire codebase is
`fdir/engine.py:168-170`: a bitmask test, a mode assignment, and a log string.

This is consistent with the case study's own most uncomfortable conclusion (§6): *four of
five documented FDIR failures were failures of authority, isolation, verification or
action repertoire — not of detection.* We built the part that was already adequate.

---

## 2. Defects in existing code

Ordered by severity. These are bugs in working, tested code — not missing features.

### D1 — A watchdog reset strands a healthy spacecraft in SAFE **(VERIFIED)**

`fdir/engine.py:154` gates the end-of-boot decision on bare truthiness:

```python
if self.fault_flags:            # any bit at all, including informational ones
    self.mode = Mode.SAFE
```

`WATCHDOG_RESET` (bit 7) records *why you booted*; it is not a fault condition. Verified:
a healthy vehicle + watchdog reset → SAFE, flags=128, with no autonomous exit.

Two things make this serious. It bypasses `SAFE_MODE_TRIGGER_FLAGS`, so advisory-only
flags gain SAFE authority during BOOT that they are explicitly denied in NOMINAL — the
architecture's central boundary has a hole in it. And it is **the CSSWE failure mode
reproduced in our own code**: an autonomous reset becomes a one-way trip into a state
only the ground can leave. If comms is what broke, that is permanent.

**Fix:** gate on `SAFE_MODE_TRIGGER_FLAGS`, not truthiness.

### D2 — `reboot → RESET_FAULTS → EXIT_SAFE_MODE` returns a still-faulted vehicle **(VERIFIED)**

`start_boot()` clears `_sensor_timeout_since`, `_undervoltage_since`, `_thermal_since`,
`_ml_breach_count`, `_imu_history` — the exact state `reset_faults()` inspects as its
evidence that a condition has cleared. After a reset that evidence is gone, so
`reset_faults()` clears every latched flag unconditionally.

Verified with undervoltage physically present throughout: flags cleared, exit accepted.
**This defeats FDIR-005**, the requirement those guards exist to enforce.

Note the asymmetry that causes it: `fault_flags` deliberately survives `start_boot()`, but
the justification for clearing them does not.

**Fix:** `reset_faults()` must require positive evidence of clearance (N clean samples
observed since boot), never absence of evidence.

### D3 — `SENSOR_LOCKUP` reproduces the Delfi-C3 misdiagnosis **(CONVERGENT — two agents)**

`SENSOR_LOCKUP` is in `SAFE_MODE_TRIGGER_FLAGS` and latches when the six-axis IMU
fingerprint repeats across 5 samples. Delfi-C3's documented symptom is *"insertion of
zero's in the telemetry data"* — which produces exactly a repeating fingerprint.

So a data-path fault that zeros the bus will make our engine confidently diagnose *"the
IMU is frozen"* and safe the vehicle. That is the case study's §6 *"protective action
fired on misdiagnosed cause"* — **already present in our code, before we have written any
of the recovery machinery it was supposed to guard.**

**Fix:** every per-channel detector must pass a data-path-health discriminator before it
gets autonomous authority (R6).

### D4 — `RESET_FAULTS` always ACKs `ACCEPTED`, even when it clears nothing **(VERIFIED)**

Verified: fault active, `RESET_FAULTS` → `ACCEPTED`, flags unchanged (14 → 14). The
operator cannot distinguish a successful reset from a refused one. For a project whose
whole thesis is *verify what your actions actually did*, the command interface currently
lies about outcomes.

### D5 — Two distinct `FaultFlag` classes exist at runtime **(VERIFIED)**

`protocol.FaultFlag is simulator.protocol.FaultFlag` → `False`; `isinstance` across the
boundary → `False`. Works today only because `IntFlag` compares by value. Any `isinstance`,
`match`, or pickle across that seam breaks silently. Caused by inconsistent
`sys.path.insert` conventions across 12 sites.

### D6 — Transport code mutates FDIR safety state **(AUDIT, confirmed by inspection)**

`run_simulator.py:83,85,203` write `engine.fault_flags` directly for `COMMS_LOSS` and
`CORRUPTED_PACKET`. Two of ten fault flags have no detector inside `fdir/`, yet
`reset_faults()` clears them — the engine clears flags it cannot observe or re-verify.

`fdir/config.py:42 COMMS_LOSS_TIMEOUT_S` is **dead code**; the live value is hardcoded
`5.0` at `run_simulator.py:84`.

### D7 — Simulator faults do not couple **(VERIFIED)**

- **No fault perturbs `bus_current_a`.** All five leave it at ~0.40 A; differences are RNG
  noise. Overcurrent is the canonical latch-up signature, and `fdir/engine.py` never reads
  current at all.
- **Co-injected faults corrupt ground truth.** `undervoltage` + `gradual_drift` → truth
  claims both, voltage shows only undervoltage. The ML dataset's answer key is wrong for
  any concurrent fault.
- **`sensor_lockup` + `sensor_timeout` is physically impossible**: `imu_responded=False`
  with frozen readings.

### D8 — Reported ML numbers describe a configuration FDIR does not run **(AUDIT)**

`ml/evaluate.py` measures un-debounced per-sample `predict()`, but `_update_ml_advisory`
requires **3 consecutive** anomalous samples. At 3.4–3.8% per-sample rates, three-in-a-row
is a fundamentally different event. **Every ML latency and recall figure for the four
non-lockup faults describes a detector we do not actually operate.** These must not be
quoted as integrated performance until re-measured with the debounce applied.

### D9 — `anomaly_model.h` exports a score with no threshold **(AUDIT)**

The C header returns the Liu/Ting/Zhou score (higher = more anomalous) — the *opposite*
sign to sklearn — and exports no cutoff. Firmware gets a bare float. The missing constant
is `-offset_ = 0.5051816892259796`, and the sign convention must be converted, not assumed.

### D10 — Lesser but real

`HealthFlag.TEMP_OK/MAG_OK/POWER_OK` are set once and never cleared, so the dashboard
reports healthy sensors during a latched thermal fault — a falsehood on the operator
display. `temp_responded` is read by no detector. `MAX_FALSE_SAFE_ENTRIES_PER_6H` is
referenced by zero lines. `engine.log` is unbounded in RAM. Dashboard `t_s` goes negative
after a reboot, exactly when the narrative most needs to be legible.

---

## 3. Doc-vs-code contradictions to resolve

I am resolving these as lead engineer rather than asking.

| # | Conflict | Resolution |
|---|---|---|
| **C1** | `mode-diagram.md:44` says COMMS_LOSS and SENSOR_TIMEOUT trigger SAFE. Neither is in `SAFE_MODE_TRIGGER_FLAGS`. | **Code is right, docs are wrong.** Fix the diagram. |
| **C2** | If comms loss *did* trigger SAFE, FDIR-005 (operator-only exit) makes it a deadlock — the exit command cannot arrive. | **Comms loss triggers a staged recovery ladder, never a terminal SAFE.** This is R4 inside our own design. |
| **C3** | `COMMS_LOSS_TIMEOUT_S = 5.0` is a *link heartbeat*; CSSWE's inference is *"no ground contact for N hours"*. | **Two different timers.** Keep 5 s as link telemetry; add a separate, much longer recovery-ladder timer with its own requirement ID. Reusing the 5 s timer would power-cycle the radio every five seconds. |
| **C4** | FDIR-005 "SAFE never exits on its own" vs R9 "safe mode must have an exit strategy". | **Not in conflict once split:** exit *to NOMINAL* stays operator-gated; onward autonomous transition *to a pre-validated degraded mode* is permitted. SAFE-as-destination is what CAPSTONE refutes. |
| **C5** | A7 proposes ports injected into the engine; A9 assumes an executor. | **Adopt A7's option (b): the engine emits `RecoveryIntent`s; a separate executor owns the hardware ports.** Keeps `FDIREngine` pure, leaves all 154 tests passing unmodified, satisfies "no GPIO/I2C in FDIR", and makes intents directly assertable. |
| **C6** | A6 says 6 spare flag bits suffice; A9 needs 7 new flags (17 total > 16). | **A9 is right — widen now.** `fault_flags` → uint32, `health_flags` → uint16. Packet 78 → 82 bytes. Cheap today, expensive after V1 firmware exists. |

---

## 4. Gap analysis

| Area | Status | Notes |
|---|---|---|
| Deterministic detectors | **Already implemented** | Six, debounced, latched, SRS-traced. The one mature bucket. Leave the structure alone. |
| ML authority boundary | **Already implemented** | `SAFE_MODE_TRIGGER_FLAGS` + a test that fails if violated. The only R# already met (R11). **Do not touch.** |
| Wire protocol / CRC / framing | **Already implemented** | 118 tests. Only change: widen two flag fields (C6). |
| Ground-station link + CSV | **Already implemented** | Reusable as-is. |
| Boot / reset semantics | **Incorrect** | D1, D2. Fix before anything builds on them. |
| Data-path vs sensor discrimination | **Incorrect** | D3 — actively misdiagnoses today. |
| Command outcome reporting | **Incorrect** | D4. |
| Module boundaries / imports | **Needs refactoring** | D5, D6. Shared ICD package; engine methods replacing external flag writes. |
| Simulator physical state | **Missing** | No rails, no battery, no thermal nodes, no bus topology. Everything else depends on this. |
| Diagnosis | **Missing** | No symptom→cause layer at all. |
| Isolation | **Partial (vestigial)** | 1 of 4 health bits maintained; response granularity is the whole vehicle. |
| Recovery actions | **Missing** | Zero autonomous actions. `Mode.SAFE` has no consequences anywhere. |
| Verification | **Missing** | Nothing ever checks whether an action worked. |
| Escalation / bounded retries | **Missing** | Nothing is retried, so nothing is bounded. |
| Persistent recovery state | **Missing** | Total amnesia across restart. |
| Degraded modes | **Missing** | Four modes; none of them degraded. |
| UNKNOWN fault handling | **Missing** | Ten named causes, no UNKNOWN. R10 traces to 63% of the record. |
| Hardware abstractions | **Missing** | Zero. Not a stub. |
| Fault-injection coupling | **Missing** | D7. |
| Observability of the narrative | **Partial** | Most of it is already on the wire and discarded — cheapest win available. |
| ML #2 | **Correctly absent** | Case study §19: not justified yet. Leave the interface seam only. |

---

## 5. Implementation plan

Ordered by dependency. Phases 0–2 are sequential; within later phases, independent work
is marked parallel.

### Phase 0 — Fix what is broken (no new features)

D1, D2, D4, plus regression tests for each. These are corrections to safety logic that
everything else will build on; building recovery machinery on top of a boot path that
strands the vehicle in SAFE would be building on sand. **Add D3's failing test now** —
assert that today's engine misdiagnoses a zeroed bus as `SENSOR_LOCKUP`. That failing
test *is* the Delfi-C3 reproduction and becomes the regression guard after Phase 4.

### Phase 1 — Shared foundations *(unblocks everything; parallel internally)*

- **1a.** Extract ICD types (`Mode`, `FaultFlag`, `HealthFlag`, `RawSample`) into a shared
  package below both `fdir/` and `simulator/`. Kills D5 and the circular import. Add
  `pyproject.toml` + `conftest.py`; delete the 12 `sys.path.insert` sites.
- **1b.** Widen `fault_flags`→uint32, `health_flags`→uint16 (C6). Update ICD docs, C header,
  protocol tests.
- **1c.** Give `FDIREngine` explicit `note_link_state()` / `note_corrupted_packet()` methods
  so transport stops writing flags directly (D6).

### Phase 2 — Simulator physical state *(the long pole)*

Replace per-signal output overrides with a state vector: power rails with per-rail current,
battery (SOC, internal resistance), multi-node first-order thermal, device states, and bus
topology. **`bus_voltage_v` and `bus_current_a` become derived, not injected.** Add
`set_rail_power()` / `obc_reset()` actuator entry points — without a commandable
environment there is no recovery to test. Nominal point stays byte-compatible: 0.40 A
total, 5.06 − 0.40×0.15 = 5.00 V.

The clearing rule that encodes the whole CSSWE/KySat-2 distinction:

```
power removed ≥100 ms  → latch clears      (CSSWE: power removal works)
OBC reset              → latch UNCHANGED   (KySat-2: reset does not)
RESET_FAULTS           → latch UNCHANGED   (flags clear, physics doesn't)
```

### Phase 3 — Hardware ports *(parallel with 4)*

`PowerPort`, `CurrentSensePort`, `DeviceProbePort`, `WatchdogPort`, `RecoveryStorePort`,
`ResetPort` as `typing.Protocol`. Land **one at a time, each with its first real caller and
one fault-injection test** — six empty protocols above a 400-line engine is exactly the
"documentation outgrowing working code" failure CLAUDE.md warns about. Signatures must map
to C function pointers (IntEnum device IDs, no kwargs, no exceptions-as-control-flow).

### Phase 4 — Data integrity and diagnosis *(parallel with 3)*

Telemetry quality states (valid/stale/missing/stuck/impossible/conflicting) and the
data-path discriminator: ≥2 devices on the *same* bus invalid within one debounce window →
suspect the path, not the devices. Then a **deterministic fault tree** over already-computed
detector outputs — not ML (case study §19). Emits `KNOWN_FAULT` or explicitly `UNKNOWN`.

### Phase 5 — Recovery, verification, escalation, persistence

The engine emits `RecoveryIntent`; a `RecoveryExecutor` owns the ports. Every action carries
preconditions, timeout, max attempts, expected result, verification condition, and escalation
target. Attempt counters persist to NVM **before** the action executes. Ladder resumes at
rung k+1 after a reboot, never rung 1.

### Phase 6 — Fault-injection scenarios

Built as **discrimination pairs**, not independent tests — a scenario alone proves detection;
a pair proves isolation, and isolation is what four of five documented FDIR failures lacked.
Priority order: `radio_latchup` + `radio_unresponsive` (the pair), `communication_loss`,
`rail_overcurrent`, `recovery_failure`, `OBC_reset_during_recovery`, `data_bus_failure` +
`sensor_corruption` (the pair), `battery_degradation` + `sensor_drift` (the pair),
`unknown_anomaly`.

Every scenario inherits: determinism under fixed seed, a paired 6 h nominal control run,
detection latency **reported not just asserted**, and **mandatory negative assertions**
(which flags must *not* latch, which actions must *not* fire).

### Phase 7 — ML #1 streaming integration *(deliberately last, and non-blocking)*

Ring buffers replacing groupby/rolling; `ddof=1` preserved exactly; suppress the advisory
until the buffer is full (fixes D9's cold-start burst); export the threshold constant to the
C header. **Re-measure with the 3-sample debounce applied** and regenerate the evaluation
report (D8). Nothing in Phases 0–6 depends on this.

### Phase 8 — Observability

Tier 1 only: reconstruct the event timeline ground-side by diffing consecutive packets. Mode,
fault flags and health flags already arrive in every packet with 1800 buffered — the entire
detection→isolation→recovery→verification narrative is *already on the wire and thrown away*.
Visually distinguish advisory flags from SAFE-triggering ones; that is the cheapest
correction available and needs no wire change.

---

## 6. Two decisions with deadlines

**Per-rail current sensing and independently switchable loads.** The case study attributes
KySat-2's loss to this missing *hardware* capability, not a missing algorithm, and it becomes
expensive after the board is bought. Phase 6's `radio_latchup`/`radio_unresponsive` pair is
designed to put a number on what it buys: run the discrimination with per-rail current
available, then with only aggregate `bus_current_a`. A 50 mV bus sag against σ=0.02 V noise
is ~2.5σ — marginal at best. **That measurement is producible before any hardware is
purchased, and it is the purchase justification.**

**Flag-field widths.** One afternoon now; an ICD + firmware + ground-station change later.

---

## 7. What this plan deliberately does not do

- **No ML #2.** Case study §19: not justified on current evidence, and the literature review
  that would settle it did not run. Interface seam only.
- **No attitude-based radiation mitigation.** Refuted in §9 of the case study as *"plausibly
  harmful"*. Written into the requirements as excluded before V2 adds any ADCS actuator.
- **No new detectors.** The case study's §6 is explicit that better detection is the
  low-value direction. We have six; the gap is authority, isolation, verification and action
  repertoire.
- **No claim that any of this would have saved a specific spacecraft.** The framing stays the
  case study's own: the architecture provides a *recovery/containment pathway* for a
  documented failure mechanism.

## 8. On the 75–90% hypothesis

V0's metrics will report: faults injected, detected, correctly isolated, recovered,
contained, unrecoverable. That denominator is **our injected fault set**, not CubeSat failure
in general. The case study bounds the latter hard — 63% of the NASA record has no stated
cause and 16% was never heard from — so no V0 result can speak to the population figure. The
metrics output will state that distinction explicitly so it cannot be misread later.
