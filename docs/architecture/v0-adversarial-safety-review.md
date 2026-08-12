# V0 adversarial safety review

**Date:** 2026-08-12 · **Scope:** Phases 0–8, `fdir/` core · **Method:** see §5, which
states plainly what this review is and is not.

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
