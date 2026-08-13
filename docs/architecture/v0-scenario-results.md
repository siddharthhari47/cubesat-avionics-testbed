# V0 Fault-Injection Scenario Results

**Status: Simulated.** Every number here comes from `scenarios/runner.py` driving
`simulator/environment.py` against `fdir/`. Nothing has run on hardware.
Reproduce with:

```bash
python scenarios/runner.py
```

## What the denominator is, and is not

The outcome distribution below is over **our injected fault set** — nine fault
types we chose and modelled. **It is not a statement about CubeSat failure in
general, and cannot be turned into one.** The failure research bounds that
population hard: 63% of the NASA record has no identifiable technical cause and
16% was never heard from at all. No result from this suite can speak to "what
fraction of real failures is recoverable".

What this suite *can* say is narrower and still useful: given a fault we
understand well enough to model, what does the architecture actually do about it.

## Results

| Scenario | Detected | Latency (s) | Diagnosis | Correct | Outcome | Actions |
|---|---|---|---|---|---|---|
| nominal control | no | — | — | — | **clean** | 0 |
| radio latch-up *(per-rail sensing)* | yes | **0.70** | `RADIO_LATCHUP` | ✅ | recovered | 2 |
| radio unresponsive *(per-rail sensing)* | yes | 5.10 | `GROUND_LINK_LOST` | ✅ | contained | 4 |
| radio latch-up *(**no** per-rail sensing)* | yes | 5.10 | `GROUND_LINK_LOST` | ❌ | recovered | 2 |
| radio unresponsive *(**no** per-rail sensing)* | yes | 5.10 | `GROUND_LINK_LOST` | ✅ | contained | 4 |
| data bus failure | yes | 0.10 | `DATA_PATH` | ✅ | detected_only | 0 |
| single sensor corrupt | yes | **0.50** | `SENSOR_CORRUPT` | ✅ | detected_only | 0 |
| recovery that cannot succeed | yes | **0.70** | `RADIO_LATCHUP` | ✅ | contained | 4 |
| OBC reset mid-recovery | yes | **0.70** | `RADIO_LATCHUP` | ✅ | contained | 8 |
| rail overcurrent | yes | **0.70** | `RAIL_OVERCURRENT` | ✅ | contained | 0 |
| undervoltage | yes | 0.30 | `POWER_UNDERVOLTAGE` | ✅ | contained | 0 |
| thermal excursion | yes | 0.40 | `THERMAL` | ✅ | contained | 0 |
| sensor frozen | yes | 0.50 | `SENSOR_FROZEN` | ✅ | contained | 0 |
| sensor not responding | yes | 0.20 | `SENSOR_NOT_RESPONDING` | ✅ | detected_only | 0 |
| gradual drift | **no** | — | — | — | undetected | 0 |

**Correction, 2026-08-12.** Every comms-driven latency above was previously
recorded as **0.10 s** and is now **5.10 s**. The old figure was wrong, and
not by a rounding error — it was an artifact.

`SpacecraftEnvironment.last_ground_contact_t` was initialised to 0.0 and
advanced by nothing (`note_ground_contact()` existed with no callers), so
`seconds_since_ground_contact` was really *seconds since boot*. By the time any
fault was injected it was already past `COMMS_LOSS_TIMEOUT_S`, so `COMMS_LOSS`
latched on the very first tick of every link drop. **The 5 s debounce was never
exercised by this suite or by any test.** Found while fixing K1 in the
adversarial safety review; see `v0-adversarial-safety-review.md` §7.

5.10 s is the honest number: 5.0 s of configured debounce plus one 0.1 s tick.
The detector is doing exactly what `fdir/config.py` says it should — it simply
had never been made to prove it. Non-comms latencies (undervoltage 0.30,
thermal 0.40, lockup 0.50, sensor timeout 0.20, data bus 0.10) are unchanged,
as are every outcome and the whole negative-assertion column.

**Two detectors added, 2026-08-12 (FDIR-011, FDIR-012).** `single sensor corrupt`
and `rail overcurrent` had both been reported here as **undetected** for several
phases, and in each case that was the honest state: the data the detector needed was
already in `RawSample` and nothing consumed it. Both now detect and diagnose
correctly, and **undetected falls from 3/14 to 1/14** — only `gradual drift` remains,
which is R7 and deliberately out of V0 scope.

The `rail overcurrent` row carries a forbid on `UNDERVOLTAGE_CRITICAL`, and that is
the load-bearing half of the assertion: catching this as a voltage problem means
catching it after the battery has drained, which is the KySat-2 failure rather than a
fix for it. It must be caught on current, before the sag.

**The sharpest result is the latch-up pair.** Both halves now assert the same flags,
so the measured difference isolates exactly one variable — whether per-rail current
sensing exists:

| | Detection | Diagnosis |
|---|---|---|
| radio latch-up, **with** per-rail current | **0.70 s** | `RADIO_LATCHUP` ✅ |
| radio latch-up, **without** per-rail current | 5.10 s | `GROUND_LINK_LOST` ❌ |

7× faster and correct, versus slow and wrong. That is the argument for the INA219/226
on the hardware shortlist, stated as a measurement rather than an opinion — and it is
the one number this suite produces that directly justifies a purchase.

**Negative assertions: all clean.** No forbidden flag latched; no forbidden
action fired. This matters more than the positive column — four of five
documented FDIR failures were wrong-action failures, and a suite with only
positive assertions cannot catch those.

**Outcome distribution (n=14 injected faults):** recovered 2, contained 8,
detected-only 3, undetected 1, unknown-held 0.

---

## Finding 1 — the per-rail sensing measurement

This is the hardware-purchase question, answered as a measurement rather than an
opinion. Same fault, same seed, same everything except whether per-rail current
is available to the diagnosis layer:

| | Diagnosis | Correct? |
|---|---|---|
| radio latch-up, **with** per-rail current | `RADIO_LATCHUP` | ✅ |
| radio latch-up, **without** per-rail current | `GROUND_LINK_LOST` | ❌ |

**Without per-rail current sensing, a latch-up is misdiagnosed as a quiet ground
link.** The two faults are indistinguishable on the link itself; the only channel
that separates them is the radio rail's current draw.

**The honest caveat, which matters.** The blinded run still shows `recovered` —
because the recovery ladder's rung 1 is a power cycle regardless of what the
diagnosis said. The wrong diagnosis cost nothing *here*, purely because the
ladder is currently generic. That will stop being true the moment diagnosis
selects the action rather than merely annotating it, which is the natural next
step. Reporting this as "sensing doesn't matter, we recovered anyway" would be
reading the table backwards.

## Finding 2 — a fault diagnosed by its consequence, not its cause *(CLOSED)*

**This finding is resolved. It is kept because how it was resolved is the point.**

`rail_overcurrent` used to be **undetected**, with the diagnosis layer confidently
reporting `THERMAL`. Both halves were real. Undetected was the honest state — no
overcurrent detector existed, and the KySat-2 mechanism is precisely that a rail
drains the battery while every fixed voltage threshold stays satisfied (measured in
Phase 2: 1.55 A draw at 4.85 V, above both thresholds). The `THERMAL` diagnosis was a
genuine defect: the extra current heats the structure node, the thermal detector fires
on the *consequence*, and the diagnosis layer named it — while the actual cause went
unnamed. A smaller cousin of Delfi-C3: a correct detector firing on a real symptom
that is not the root cause.

FDIR-011 closes both. Detection at **0.70 s** on rail current, diagnosis
`RAIL_OVERCURRENT`, and the rule sits **above** `UNDERVOLTAGE_CRITICAL` in
`diagnose()` — because the drain and the sag are one fault, and diagnosing the sag
treats a symptom while the cause keeps draining. The scenario now forbids
`UNDERVOLTAGE_CRITICAL` outright: being caught on voltage would mean being caught too
late, which is the failure rather than a fix for it.

Note what did **not** change: the detector requires per-rail current, so it cannot
exist on the blinded runs. Finding 1's argument is unaffected and, if anything,
stronger.

## Finding 3 — gaps the suite exposes rather than hides

| Gap | Why it is undetected | Honest status |
|---|---|---|
| ~~`single sensor corrupt`~~ | ~~No per-channel plausibility check exists.~~ | **CLOSED** by FDIR-012. Detected at 0.50 s, diagnosed `SENSOR_CORRUPT`, and still correctly *not* a path fault. The signal was always there — `_suspect_devices()` had to identify the channel in order to count devices per bus — but a lone suspect device latched nothing. The asymmetry was never intentional. |
| `gradual_drift` | The EWMA adaptive baseline absorbs slow change as the new normal — measured at 0% recall in the ML evaluation, and the flight analogue is QuakeSat. | Known and previously documented. R7 (fixed-reference trending) is the fix and is not built. |
| `unknown_held` = 0 | No scenario produced an UNKNOWN diagnosis, because the only detectors that yield one (adaptive/ML) did not fire in any scenario. | The R10 path is implemented and unit-tested but **not exercised end-to-end by any scenario.** Worth adding a scenario that genuinely defeats every enumerated rule. |

## Finding 4 — the KySat-2 pair behaves correctly

`recovery that cannot succeed` and `OBC reset mid-recovery` both end
**contained**, not looping:

- The action executes, the port accepts it, verification fails, the ladder
  escalates, and the campaign exhausts — 4 actions, then a full stop.
- With an OBC reset injected mid-campaign, the count reaches 8 rather than
  restarting from zero, because the campaign state survives the reset and
  resumes at the next rung. Without persistence this is the infinite loop.

## What "contained" means here, and why it dominates

Eight of fourteen outcomes are `contained` rather than `recovered`. That is not a
disappointing result — it is the expected one, and it matches the case study:
most documented spacecraft outcomes were containment or degraded operation, not
repair. `contained` means the fault was detected, correctly diagnosed, and the
vehicle was left in a safe, non-deteriorating state without the fault being
cleared. For faults with no available corrective action (thermal, undervoltage,
a frozen sensor) that is the correct ceiling, not a shortfall.

The two `detected_only` rows are also correct behaviour: a data-path fault and a
non-responding sensor were each diagnosed accurately and **deliberately produced
zero actions**, because acting per-device on a path fault is exactly the
Delfi-C3 error.

## Reproducibility

Every scenario is seeded and every actuator command consumes no RNG draws, so a
replay with recovery actions is byte-identical to one without. The suite exits
non-zero if any negative assertion is violated, so it can gate a build.
