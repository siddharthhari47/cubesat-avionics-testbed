# V0 adversarial safety review

**Date:** 2026-08-12 · **Scope:** Phases 0–8, `fdir/` core · **Method:** see §5, which
states plainly what this review is and is not.

> **STATUS: all six fixed, same day.** Each fix was verified by re-running the exact
> probe that found the defect, and each is pinned by a regression test in
> `tests/test_safety_review_regressions.py` (32 tests). Suite: 249 → **281 passing**.
> Fixing F2 exposed a seventh problem — a test that had been passing for the wrong
> reason. See §6 for what changed and what it cost.
>
> **ROUND 2 (§7) has since run the five dimensions §5 skipped.** Four more findings,
> including one HIGH that defeats R5 outright: the spacecraft cannot notice a silent
> link failure, so the entire CSSWE recovery ladder is gated behind a flag that never
> latches. Two of my own earlier concerns were **refuted by measurement** and are
> recorded as such. **Round 2 is now fixed too — ten findings, all ten closed, 288
> tests.** Fixing K1 then revealed that `COMMS_LOSS_TIMEOUT_S` had never once been
> exercised by any test or scenario; see §7.

Six defects found, all reproduced by running code rather than by reading it. Three are
HIGH. Two of the three are latent in V0 — they produce no wrong behaviour in the
simulator and would produce wrong behaviour on hardware, which is the category most
worth finding before hardware arrives.

The headline is uncomfortable and worth stating first: **the single most severe finding
is a permanently confident wrong diagnosis — the exact Delfi-C3 failure mode `fdir/diagnosis.py`
was written to prevent.** The module prevents it in the case it was designed against and
reintroduces it through a latch that cannot clear.

---

## Summary

| # | Severity | Defect | V0 impact | Hardware impact |
|---|---|---|---|---|
| F1 | **HIGH** | `DATA_PATH_SUSPECT` can never be cleared, and masks every later diagnosis | Yes | Yes |
| F2 | **HIGH** | Rail/Device confusion: rung 0 resets the magnetometer, not the radio | Latent | Yes |
| F3 | **HIGH** | NaN telemetry counts as positive evidence a fault cleared | Latent | Yes |
| F4 | MEDIUM | One NaN permanently and silently disables the adaptive baseline | Latent | Yes |
| F5 | MEDIUM | `UNKNOWN_ANOMALY` and `RECOVERY_FAILED` are also permanently unclearable | Yes | Yes |
| F6 | LOW | `import_recovery_state` validates types but not value ranges | Yes | Yes |

F1 and F5 share one root cause. F3 and F4 share another.

---

## F1 — `DATA_PATH_SUSPECT` latches permanently and masks all subsequent diagnosis

**Severity: HIGH.** Violates R6 and R10.

`RESETTABLE_FLAGS = CONDITION_BACKED_FLAGS | EVENT_FLAGS`, and `DATA_PATH_SUSPECT`
([`fdir/engine.py:63-72`](../../fdir/engine.py)) is in neither. `_update_data_path()`
only ever ORs the bit in — it never clears it. `reset_faults()` iterates
`CONDITION_BACKED_FLAGS` and clears `EVENT_FLAGS`, so it never even considers this flag.
Nothing else clears it. Not `start_boot()`, not `watchdog_reset()`.

The flag is *observed* correctly — `_observe(FaultFlag.DATA_PATH_SUSPECT, path_fault)`
at line 354 dutifully accumulates clean evidence — but nothing ever reads that evidence
for this flag. The mechanism to clear it exists and is wired to nothing.

Then `diagnose()` checks `DATA_PATH_SUSPECT` **first**, by design, because a suspect
shared path explains away the per-device symptoms under it. Correct when the path is
actually suspect. Permanent once the flag is stuck.

**Reproduced.** One transient bus glitch, then a fully healthy vehicle:

```
bus zeroed:         FaultFlag.SENSOR_LOCKUP|DATA_PATH_SUSPECT
300 healthy ticks:  FaultFlag.SENSOR_LOCKUP|DATA_PATH_SUSPECT
RESET_FAULTS ->     cleared=FaultFlag.NONE
DATA_PATH_SUSPECT still set? True
_clean_ticks[DATA_PATH_SUSPECT] = 300     <- evidence accumulated, never consulted

diagnose() on a healthy vehicle = DATA_PATH, confidence=LIKELY, authorises_action=True
```

And it masks real faults. With `UNDERVOLTAGE_CRITICAL` genuinely latched:

```
flags     = UNDERVOLTAGE_CRITICAL|SENSOR_LOCKUP|DATA_PATH_SUSPECT
diagnose() = DATA_PATH        (should be POWER_UNDERVOLTAGE)
```

A watchdog reset does not help — after reset and 40 healthy ticks the flag is still set.

**Why this matters more than it looks.** `authorises_action` is `True`, because
`DATA_PATH` is returned at `Confidence.LIKELY`. This is not a cosmetic stuck bit in
telemetry: it is a permanently confident wrong diagnosis, at the confidence level the
system reserves for justifying autonomous action. `fdir/diagnosis.py`'s own docstring
says *"a confident wrong diagnosis is worse than no diagnosis, because it authorises the
wrong action."* That is precisely the state this produces.

**Why the tests miss it.** `tests/test_fdir.py:636` asserts the flag is *set* on a bus
fault; `:662` asserts it is *not* set for a single device. Neither asserts it ever
clears. No test in the suite drives a fault-then-recovery cycle on this flag.

**Fix.** Add `DATA_PATH_SUSPECT` to `CONDITION_BACKED_FLAGS` — the `_observe` call and
the evidence counter are already correct, so this is a one-line change plus a
clear-path test. ~30 min including tests.

---

## F2 — The comms ladder's first rung resets the magnetometer, not the radio

**Severity: HIGH (latent in V0).** Violates R4's intent.

`comms_loss_ladder(radio_rail)` ([`fdir/recovery.py:139-163`](../../fdir/recovery.py))
takes one integer and uses it as the target for **both** rung 0 (`RESET_DEVICE`) and
rung 1 (`POWER_CYCLE`). It is called as `comms_loss_ladder(int(Rail.RADIO))`
([`fdir/engine.py:657`](../../fdir/engine.py)).

But `RESET_DEVICE` and `POWER_CYCLE` index different enums. The executor proves it:
`self._power.set_enabled(intent.target, ...)` treats the target as a **Rail**;
`self._reset.reset_device(intent.target)` treats it as a **Device**
([`fdir/executor.py:84,107`](../../fdir/executor.py)).

`Rail.RADIO = 1`. `Device.RADIO = 3`. `Device(1) = MAG`.

**Reproduced:**

```
rung 0: RESET_DEVICE  target=1   -> Device(1) = MAG      | "soft reset the radio"
rung 1: POWER_CYCLE   target=1   -> Rail(1) = RADIO      | "power-cycle the radio rail"
rung 2: RESET_DEVICE  target=-1  -> whole system         | "full system reset"
```

Rung 1 is correct. Rung 0 commands a reset of the magnetometer while its own description
says "soft reset the radio".

**Why V0 hides it.** `SimulatedResetPort.reset_device()` returns `False` — device reset
is honestly not modelled. So rung 0 always reports unavailable, the ladder escalates, and
nothing observable differs. The honesty that made `hardware_sim.py` return `False`
instead of lying is exactly what conceals this. On real hardware rung 0 succeeds, resets
the wrong device, fails verification, and the ladder escalates having disturbed an
innocent subsystem.

**Why the tests miss it.** `test_escalation_moves_through_distinct_rungs_not_blind_repetition`
asserts the ladder moves through distinct *actions*. No test asserts which *device* any
rung targets.

**Fix.** Give the ladder two explicit parameters (`radio_device`, `radio_rail`) rather
than one overloaded integer, and add a test asserting rung 0 targets `Device.RADIO`.
Better still, make `RecoveryIntent.target` carry its enum type so the confusion is
unrepresentable. ~1 h for the parameter fix; the type change is a V1 refactor.

---

## F3 — NaN telemetry is accepted as positive evidence that a fault cleared

**Severity: HIGH (latent in V0).** Defeats the D2 fix.

In Python every comparison with NaN is `False`. `_update_undervoltage` tests
`sample.bus_voltage_v < cfg.UNDERVOLTAGE_CRITICAL_V`, which is `False` for NaN, so it
takes the `else` branch and calls `_observe(UNDERVOLTAGE_CRITICAL, False)` — recording
the reading as **evidence the condition is gone**.

`reset_faults()` was deliberately rewritten (D2) to require positive evidence rather than
absence of evidence, specifically so a reboot could not manufacture clearance. NaN
manufactures it just as effectively.

**Reproduced, end to end:**

```
after real undervoltage:      mode=SAFE  flags=UNDERVOLTAGE_WARNING|UNDERVOLTAGE_CRITICAL
RESET_FAULTS while still low: cleared=NONE  still=UNDERVOLTAGE_CRITICAL   <- correct
after 60 NaN-voltage ticks:   _clean_ticks[UNDERVOLTAGE_CRITICAL] = 60  (threshold 3)
RESET_FAULTS now:             cleared=UNDERVOLTAGE_CRITICAL
exit_safe_mode() accepted=True   mode=NOMINAL
```

A vehicle correctly held in SAFE is returned to service on the strength of readings that
carry no information at all. The failure is in the unsafe direction.

**Worth noting what does work:** the thermal detector handles NaN *safely* by accident.
`not (LOW <= NaN <= HIGH)` is `not False` = `True`, so a NaN temperature latches
`THERMAL_ANOMALY`. Same language rule, opposite outcome, purely because of how each
predicate is written. That inconsistency is the finding as much as the undervoltage case
is — neither detector decided anything about NaN; the behaviour fell out of expression
form.

**Grep result:** zero occurrences of `isnan`, `isfinite`, or any finiteness guard across
`fdir/`, `icd/`, and `simulator/`.

**Fix.** Validate finiteness at the ICD boundary — `RawSample` is the right place, since
it is the one chokepoint every detector reads through. A non-finite reading should mark
the channel untrusted (the `_channel_trusted` machinery already exists for exactly this
shape of problem), never silently count as nominal. ~2 h including tests.

---

## F4 — One NaN permanently and silently disables the adaptive baseline

**Severity: MEDIUM.** Advisory detector, so no authority is lost — but it dies silently.

`EwmaStat.update()` ([`fdir/engine.py:120-131`](../../fdir/engine.py)) has no guard. One
NaN poisons `mean` and `var` irreversibly:

```
after 50 clean:       mean=5.0000  var=0
after ONE NaN:        mean=nan     var=nan
after 500 more clean: mean=nan     var=nan
deviation_sigma(...) = nan  ->  'nan > ADAPTIVE_K' = False, forever
```

The detector never fires again, and reports exactly what a healthy detector reports.
This is the observability failure the project cares about most: a dead detector and a
detector saying "all clear" are indistinguishable on the wire.

**Fix.** Reject non-finite input in `EwmaStat.update()`; count rejections so a silently
dead channel is visible. Subsumed by F3's boundary validation. ~30 min.

---

## F5 — `UNKNOWN_ANOMALY` and `RECOVERY_FAILED` are also permanently unclearable

**Severity: MEDIUM.** Same root cause as F1.

Full audit of which flags `reset_faults()` can clear:

| Flag | Resettable | Correct? |
|---|---|---|
| `SENSOR_TIMEOUT`, `UNDERVOLTAGE_CRITICAL`, `THERMAL_ANOMALY`, `SENSOR_LOCKUP`, `ADAPTIVE_ANOMALY`, `ML_ANOMALY` | yes | ✅ |
| `WATCHDOG_RESET`, `CORRUPTED_PACKET` | yes | ✅ event flags |
| `UNDERVOLTAGE_WARNING` | no | ✅ self-clears each tick |
| `COMMS_LOSS` | no | ✅ self-clears on contact, documented |
| `RECOVERY_FAILED` | **no** | ❌ nothing clears it, ever |
| `DATA_PATH_SUSPECT` | **no** | ❌ F1 |
| `UNKNOWN_ANOMALY` | **no** | ❌ nothing clears it, ever |

**Reproduced:** after the advisory flag that caused it is cleared and 200 quiet ticks
pass, `UNKNOWN_ANOMALY` remains set with no path to clear it.

R10's own flag can never be lowered: once the spacecraft says "I do not know", it says so
for the rest of the mission. `RECOVERY_FAILED` likewise survives a later *successful*
campaign, so "autonomy stood down" is permanently displayed on a vehicle whose autonomy
subsequently worked.

**Fix.** Same one-line class as F1. Decide deliberately for each: `UNKNOWN_ANOMALY`
should be condition-backed (it tracks a live state); `RECOVERY_FAILED` is arguably an
event flag, cleared on acknowledgement. ~1 h with tests.

---

## F6 — `import_recovery_state` validates types but not value ranges

**Severity: LOW.**

`Campaign.from_dict()` correctly rejects a wrong `schema_version`, a missing key, and a
wrong type. It accepts semantically impossible values:

| Input | Result |
|---|---|
| `rung_index = -5` | accepted → `EXHAUSTED`, permanent `RECOVERY_FAILED` |
| `rung_index = 99` | accepted → `EXHAUSTED`, permanent `RECOVERY_FAILED` |
| `attempts_on_rung = -100` | accepted, negative counter retained in `total_attempts` |
| `max_attempts = -1` | accepted → escalates immediately |
| empty rung list | accepted → `EXHAUSTED` |

**Stated honestly: every case failed safe.** I specifically tried to produce unbounded
retry from corrupt NVM and could not — 400 ticks from a `max_attempts=-1` campaign
terminated normally. The bound holds.

The finding is therefore narrow: validation is type-level, not range-level, so safety
here rests on downstream `current_rung` bounds checking rather than on rejecting bad
input at the boundary. The one real consequence is that corrupt NVM can synthesise a
permanent `RECOVERY_FAILED` (via F5).

**Fix.** Range-check in `from_dict` and raise `ValueError`, which the existing
discard-on-unreadable path already handles correctly. ~30 min.

---

## What held up

Stated as plainly as the failures, because it is the more important half.

- **The ML advisory boundary (I1/R11) held under every probe.** `SAFE_MODE_TRIGGER_FLAGS`
  and `RECOVERY_AUTHORITY_FLAGS` are genuinely separate gates, and both `_propose()` and
  `_start_campaign()` check authority before queueing anything. I found no path by which
  an advisory flag reaches `self.mode` or produces an intent. Note F1 does *not* breach
  it: a stuck `DATA_PATH_SUSPECT` corrupts diagnosis, but diagnosis does not itself carry
  action authority.
- **Verification is genuinely observed, not assumed.** `RecoveryExecutor` reports only
  completion; `note_action_completed()` starts the window and does not decide success.
  The KySat-2 conflation is absent.
- **Campaign bounding holds** under normal operation and under the corrupt-state attack.
- **SAFE exit correctly refuses** while a triggering condition is genuinely active
  (demonstrated — the refusal only fails under F3's NaN case).
- **Seeded reproducibility is correct.** Every random source goes through
  `random.Random(seed)` at `simulator/environment.py:159`; no global `random` call exists
  in `simulator/`, `scenarios/`, or `ml/`.

---

## 5. What this review is, and what it is not

This was intended to run as a 10-dimension multi-agent adversarial review with
severity-scaled independent verification. **That run failed** — all 12 agents terminated
on a monthly spend limit after consuming ~818k tokens and returning nothing. It was the
third such failure. The review above was then done directly, by one reviewer.

That difference matters and should not be papered over:

- **Every finding above is reproduced by executing code**, not inferred from reading. The
  probe transcripts are quoted inline. On that axis this is stronger evidence than an
  agent report would have been.
- **But it is a single reviewer with no independent verification**, so there is no
  refutation pass, and no second opinion on severity.
- **Most importantly, this reviewer wrote most of the code under review.** A shared
  misconception between the implementation and this review is invisible here, by
  construction. That is the exact blind spot the planned `test-integrity` dimension
  existed to probe, and it went unprobed.

**Dimensions not covered at all**, and which should not be assumed clean: wire-protocol
parsing and framing (`simulator/protocol.py`, `ground-station/link.py`), the threading
and locking model in `simulator/run_simulator.py`, the ML pipeline internals, the
scenario suite's own correctness, and long-run resource growth (`self.log` in
`FDIREngine` grows without bound and nothing trims it — noted in passing, not
investigated).

Absence of findings in those areas is absence of review, not evidence of correctness.

---

## 6. Resolution

All six fixed. Every fix verified by re-running the probe that found the defect, not by
the fix "looking right".

| # | Fix | Verified by |
|---|---|---|
| F1 | `DATA_PATH_SUSPECT` added to `CONDITION_BACKED_FLAGS` — the `_observe()` evidence counter was already correct and simply wired to nothing | flag clears after recovery; healthy vehicle now diagnoses `UNKNOWN`; a real undervoltage is no longer masked |
| F2 | `Rung.target` now carries its **enum type**, not a bare int; `comms_loss_ladder()` takes device and rail separately | rung 0 targets `Device.RADIO`; passing a `Rail` where a `Device` belongs raises at construction |
| F3 | Finiteness validated at the ICD boundary (`RawSample.invalid_devices()` / `power_valid`); non-finite readings record **no evidence in either direction** | `_clean_ticks` stays 0 through 60 NaN ticks; `RESET_FAULTS` refuses; `exit_safe_mode()` refuses; vehicle stays in SAFE |
| F4 | `EwmaStat.update()` rejects non-finite input and **counts** rejections | baseline survives a NaN and recovers; `rejected == 1` |
| F5 | `UNKNOWN_ANOMALY` → condition-backed; `RECOVERY_FAILED` → event flag (acknowledgeable) | both clear; clearing `RECOVERY_FAILED` cannot re-arm autonomy, which is held in the campaign state machine |
| F6 | `Campaign.from_dict()` range-checks values, not just types; `import_recovery_state` also catches `TypeError` | all seven corrupt-state cases discarded; valid state still resumes at rung *k+1* |

### F2 needed a second attempt, and that is the interesting part

The first fix range-checked the target: reject a `RESET_DEVICE` target that is not a
valid `Device` id. **It did not work, and could not have.** `Rail` ids are 0–4 and
`Device` ids are 0–3, so every rail except `PAYLOAD` is *simultaneously a valid device
id*. `Rail.RADIO` is 1; so is `Device.MAG`. A value check can never separate them.

So the target had to become the enum member itself, with the *type* checked. That forced
a persistence change — a bare integer in NVM cannot be resolved back to the right
vocabulary — so records now carry `target_kind` and the schema went to version 2. Old v1
records are refused rather than guessed at, which is correct on its own merits: every v1
record was written by the code that had this defect.

Worth keeping as a general lesson: **when two id spaces overlap numerically, validation
by value is theatre.** Only the type carries the information.

### Fixing F2 exposed a test passing for the wrong reason

`test_escalation_moves_through_distinct_rungs_not_blind_repetition` asserted R4 — that a
recovery ladder must not depend solely on the subsystem it recovers — like this:

```python
non_radio = [r for r in h.executor.history if r.intent.target != int(Rail.RADIO)]
```

That was only ever correct *while F2 made rung 0 target a rail too*. With rung 0
correctly targeting `Device.RADIO` (3), a rail-only comparison counts the radio device
reset as "non-radio", and the assertion passes **even if the ladder never escalates off
the radio at all**. The test was quietly load-bearing on the bug.

It now checks both vocabularies and additionally asserts the escape rung is the
whole-spacecraft reset. This is exactly the self-fulfilling-test category the planned
`test-integrity` review dimension was meant to hunt, found by accident instead. One
sample is not a rate, but it is a reason to run that dimension properly.

### Regression coverage

`tests/test_safety_review_regressions.py`, 32 tests, one group per finding. Two are
deliberately general rather than specific:

- `test_no_flag_is_silently_unclearable` walks **every** `FaultFlag` and requires it to
  be either resettable or self-clearing, so a newly added flag cannot repeat F1/F5.
  Checked against the pre-fix flag sets, it fails on four flags — it is not vacuous.
- `test_corrupt_state_cannot_crash_the_boot` exists because an uncaught exception in NVM
  restore turns a recoverable data fault into a boot loop, which is worse than the fault.

### What did not change

The ML advisory boundary, the executor/engine split, campaign bounding, and the
detector thresholds are all untouched. `SENSOR_INVALID` (bit 13) is the only new wire
bit and carries no authority. The scenario suite's outcome distribution is unchanged and
its negative assertions are still clean, so none of these fixes moved a measured result.

**Still not reviewed:** protocol parsing, threading, ML internals, scenario-suite
correctness, and unbounded growth of `FDIREngine.log`. Unchanged from §5 — fixing six
findings in the areas that *were* examined says nothing about the areas that were not.

*(Round 2 below closes exactly this list.)*

---

## 7. Round 2 — the dimensions §5 skipped

Same method: probes that execute, not code that is read. Four new findings, one of them
the most consequential of either round, plus two of my own earlier concerns **refuted by
measurement** — recorded because a review that only ever confirms its own suspicions is
not a review.

| # | Severity | Defect | Reachable in V0 |
|---|---|---|---|
| G1 | **HIGH** | One undefined `AckStatus` byte permanently kills the ground station's reader thread — while it still reports `connected` | Yes |
| J1 | **HIGH** | `connected` overrides the contact heartbeat entirely, and means "a socket object exists" | Yes |
| K1 | MEDIUM | The scenario suite drives comms loss through a signal the real transport cannot produce | Yes |
| G2 | MEDIUM | An out-of-range `mode` crashes the timeline and the dashboard | Yes |
| G3 | MEDIUM | The packet-corruption signal is computed and then discarded ground-side | Yes |
| G4 | LOW | `payload_length` is written on send and never checked on receive | Yes |

---

### J1 — The spacecraft can believe it has ground contact indefinitely after losing it

**Severity: HIGH.** Defeats R5, and with it the entire CSSWE recovery ladder.

Two pieces combine.

**First**, `note_link_state()` consults the heartbeat only in its `elif`:

```python
if connected:
    self.fault_flags &= ~FaultFlag.COMMS_LOSS      # unconditional
elif seconds_since_contact is None or seconds_since_contact >= cfg.COMMS_LOSS_TIMEOUT_S:
    self.fault_flags |= FaultFlag.COMMS_LOSS
```

`connected` wins outright. `seconds_since_contact` is never examined when it is True.

**Second**, the transport defines `connected = self.conn is not None`
([`simulator/run_simulator.py:148`](../../simulator/run_simulator.py)) — the existence of
a socket object, not evidence that anything is on the other end. `sim.conn` is cleared
only in `client_handler`'s `finally`, which requires `read_packet()` to return or raise.
On a half-open TCP connection — cable pulled, ground-station machine dies, NAT idle
timeout — `recv` blocks indefinitely and never does either.

And the one signal that *would* prove contact is gone is thrown away:

```python
try:
    conn.sendall(packet.pack())
except OSError:
    pass
```

A failed send is the strongest available evidence that the link is dead. It is swallowed,
`sim.conn` is not cleared, and `last_client_seen` is not consulted.

**Reproduced:**

```
connected=True, seconds_since_contact=10000  (COMMS_LOSS_TIMEOUT_S = 5.0)
  COMMS_LOSS latched : False
  campaign opened    : False

control -- same elapsed time, connected=False
  COMMS_LOSS latched : True
  campaign opened    : True
```

So `COMMS_LOSS` — the only flag that authorises the comms recovery ladder — cannot latch
during the exact failure the ladder was built for. R5 exists because *the ground cannot
fix the radio it would have to talk through*; this is a link failure the spacecraft
cannot notice.

**Fix.** Make `connected` mean *contact observed recently*, not *socket exists*: pass the
heartbeat and let the engine apply the timeout in both branches, and treat a failed
`sendall` as contact lost. ~2 h including a test that drives a half-open link.

---

### K1 — The scenario suite validates a path the real transport cannot reach

**Severity: MEDIUM**, and it is why J1 survived Phase 6.

| | How `connected` is produced |
|---|---|
| `scenarios/runner.py:125` | `connected=self.env.link_healthy` — a clean boolean straight from the physics model |
| `simulator/run_simulator.py:148` | `connected = self.conn is not None` — socket-object existence |

The harness hands the engine exactly the signal that makes the logic work. The deployed
transport computes something different, which a silent failure never sets to False. The
`communication_loss` scenario passes, and R5 is recorded as MET in the traceability doc,
on the strength of a path the real system does not take.

This is the **simulator-and-engine shared-assumption** category named in §5 as the
biggest blind spot — the one the planned `test-integrity` dimension existed to find.
It is now found twice: once here, once when fixing F2 exposed a self-fulfilling R4
assertion. Two instances is not a rate, but it is no longer a hypothetical.

---

### G1 — One undefined status byte permanently kills the ground station

**Severity: HIGH.**

`link.py:95` converts a wire value straight into an enum:

```python
"status": proto.AckStatus(packet.status).name,
```

An undefined status raises `ValueError`. `_run()` catches only `OSError`, so the
exception escapes the thread entirely — despite the comment three lines above stating
*"A dropped link must not silently kill this background thread — that's exactly the kind
of failure a ground station has to survive."*

**Reproduced.** One ack with status `0x08`, then five perfectly good telemetry packets:

```
reader thread alive    : False
telemetry received     : 0   (5 were sent)
link reports connected : True
```

The dashboard shows a healthy connection and never updates again. Silent, permanent, in
the operator's only window into the spacecraft. This is squarely a V1 failure mode:
firmware under development emits a status code the ground station does not know yet, and
the ground station dies rather than displaying "unknown status 0x08".

**Fix.** Never construct an enum from wire data without a fallback, and catch `Exception`
around the read loop so one bad packet costs a reconnect rather than the thread. ~1 h.

---

### G2, G3, G4 — smaller, same root

- **G2 (MEDIUM):** `Mode(pkt.mode)` in both `timeline.build_timeline()` and the
  dashboard. Verified: a CRC-valid packet with `mode=99` unpacks fine, then raises
  `ValueError: 99 is not a valid Mode`. Same fix as G1.
- **G3 (MEDIUM):** `read_packet()` returns `(packet, was_corrupted)` and `link.py:80`
  binds the second to `_corrupted` and drops it. `GroundLink` has no corruption counter
  at all. CLAUDE.md names **"packet loss vs. range"** as one of the five numbers this
  project must produce — the one place that could measure it discards the evidence.
- **G4 (LOW):** `payload_length` is computed on send and read into `_payload_length` on
  receive, then never checked. Harmless while packets are fixed-size and CRC-protected,
  but it is a documented ICD field that the implementation treats as decorative, and
  firmware emitting a wrong value would go unnoticed.

---

### Refuted by measurement

**`FDIREngine.log` does not grow unboundedly in any practical sense.** §5 flagged this
in passing; measurement does not support the concern. The log appends only on state
*changes*:

```
20,000 nominal ticks (~33 min at 10 Hz)  ->  1 log entry
4,000 ticks of a FLAPPING fault          ->  43 entries (0.011/tick)
extrapolated to 24 h at 10 Hz            ->  ~9,300 entries, ~1 MB
```

Worth bounding before it runs on an STM32 with tens of KB of RAM, but it is a V1 sizing
question, not the runaway I implied. Downgraded to LOW.

**`FDIREngine.tick()` really is a pure function of state.** Two engines fed identical
input produced identical flags, mode, and logs. No mutable default arguments anywhere in
the class, and the engine never imports `time` — `now` is always passed in. The one
caveat is encapsulation rather than purity: `engine.log` and `engine.pending_intents`
are public mutable lists, and a caller can append to them directly. Nothing does. LOW.

---

### Also clean

- **Lock ordering.** The only nesting is `self.lock → self.conn_lock` (in `tick()` →
  `_update_comms_loss`). All four other `conn_lock` acquisitions are sequential, not
  nested inside `self.lock`. No inversion exists, so no deadlock path does either.
- **`xfail` markers: none exist** anywhere in the suite, so there is nothing silently
  passing under one and no XPASS to audit.
- **Mocks: one file** (`test_recovery_seam.py`). Every other test drives real objects
  against the real engine.
- **ML `_sample_std`** returns 0.0 for n < 2 rather than dividing by `n-1 = 0`, matching
  `features.py`'s `fillna(0.0)`. No division-by-zero path.

---

### Round 2 resolution — all fixed

| # | Fix | Verified by |
|---|---|---|
| J1 | The heartbeat, and only the heartbeat, decides contact. `link_established` is recorded for the log message but does not gate the decision | live A/B over the real transport (below) |
| K1 | The environment reports contact *age* always, instead of `None` whenever the link was healthy — the same evidence `last_client_seen` produces | a scenario can now express "link open but silent" at all |
| G1 | `_status_name()` renders an unknown status instead of raising; the read loop catches `Exception`, so one bad packet costs a reconnect not the thread | `_status_name(0x08) == "UNKNOWN_STATUS(0x08)"` |
| G2 | `_mode_name()` in `timeline.py`, and the dashboard falls back to `UNKNOWN (99)` | `build_timeline` survives a mode-99 packet |
| G3 | `GroundLink` counts corrupted frames and decode errors, surfaced in the dashboard | counters in `snapshot()` |
| G4 | `payload_length` is validated on receive | mismatched length rejects the packet |

**Live A/B, both ends real:**

```
ground station RECEIVING but never transmitting (the J1 case):
  packets: 119   with COMMS_LOSS: 70   -> latched after ~4.9 s of silence
ground station with the 1 Hz heartbeat (normal operation):
  packets: 119   with COMMS_LOSS: 0    -> never latched
```

4.9 s against a configured `COMMS_LOSS_TIMEOUT_S` of 5.0 s, and a healthy link stays
clean. The spacecraft can now tell a live link from a dead one.

#### The heartbeat is a real addition, not just a bug fix

J1 could not be fixed by correcting the transport alone. Telemetry is downlink-only and
the operator sends commands by hand, so **there was no periodic uplink at all** — the
`seconds_since_contact` field existed and nothing ever produced a meaningful value for
it. `GroundLink` now sends a 1 Hz `PING` (already in the command dictionary, no side
effects, acks suppressed from the operator log). Without it, making the engine respect
the heartbeat would have latched `COMMS_LOSS` on every healthy idle link — a worse
failure than the one being fixed.

#### My first attempt at J1 was wrong

I initially wrote `in_contact = link_established and not stale`, which broke
`test_note_link_state_latches_and_clears_using_config_timeout`. The test was right and
I was wrong: `link_established=False` with contact 4.9 s ago must *not* latch, because a
TCP reconnect or radio handover drops the transport while contact is fine. The timeout
*is* the grace period.

That forced the sharper rule: `link_established` is unreliable in **both** directions —
True proves nothing (J1), False does not yet prove loss — so the heartbeat alone decides.
Simpler than what I first wrote, and correct in a case I had not considered.

#### And fixing K1 revealed the comms timeout had never once run

`last_ground_contact_t` was initialised to 0.0 and advanced by nothing —
`note_ground_contact()` existed with no callers. So `seconds_since_ground_contact` was
really *seconds since boot*, already past the 5 s timeout before any fault was injected.
**`COMMS_LOSS` latched instantly on every link drop in every test and every scenario, and
`COMMS_LOSS_TIMEOUT_S` was never exercised by anything.**

It surfaced as `test_csswe_radio_latchup_recovers_autonomously` failing with 0 power
cycles: with the debounce genuinely running, the ladder now starts 5 s later than the
test's tick budget allowed. The budget was raised and
`test_the_comms_timeout_is_actually_exercised` now pins the measured latch delay against
the configured value.

This is the third instance of the same pattern — a test passing because of a defect
rather than in spite of one. F2's R4 assertion, K1's verdict-shaped evidence, and now a
debounce that never ran.

### Standing after both rounds

**Ten findings, all ten fixed.** Every dimension originally planned has been run.
Suite: 249 → **288 passing** (39 regression tests). Scenario outcome distribution
unchanged and negative assertions still clean.

What has *not* been done is an independent adversarial pass over these conclusions —
both rounds were a single reviewer who wrote most of the code, and that limitation from
§5 is unchanged. Three of the ten findings were only found because fixing something
else disturbed them, which is weak evidence that a genuinely independent pass would
still find more.
