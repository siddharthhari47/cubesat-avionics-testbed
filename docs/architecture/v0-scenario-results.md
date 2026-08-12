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
| radio latch-up *(per-rail sensing)* | yes | 0.10 | `RADIO_LATCHUP` | ✅ | recovered | 2 |
| radio unresponsive *(per-rail sensing)* | yes | 0.10 | `GROUND_LINK_LOST` | ✅ | contained | 4 |
| radio latch-up *(**no** per-rail sensing)* | yes | 0.10 | `GROUND_LINK_LOST` | ❌ | recovered | 2 |
| radio unresponsive *(**no** per-rail sensing)* | yes | 0.10 | `GROUND_LINK_LOST` | ✅ | contained | 4 |
| data bus failure | yes | 0.10 | `DATA_PATH` | ✅ | detected_only | 0 |
| single sensor corrupt | **no** | — | — | — | undetected | 0 |
| recovery that cannot succeed | yes | 0.10 | `RADIO_LATCHUP` | ✅ | contained | 4 |
| OBC reset mid-recovery | yes | 0.10 | `RADIO_LATCHUP` | ✅ | contained | 8 |
| rail overcurrent | **no** | — | `THERMAL` | — | undetected | 0 |
| undervoltage | yes | 0.30 | `POWER_UNDERVOLTAGE` | ✅ | contained | 0 |
| thermal excursion | yes | 0.40 | `THERMAL` | ✅ | contained | 0 |
| sensor frozen | yes | 0.50 | `SENSOR_FROZEN` | ✅ | contained | 0 |
| sensor not responding | yes | 0.20 | `SENSOR_NOT_RESPONDING` | ✅ | detected_only | 0 |
| gradual drift | **no** | — | — | — | undetected | 0 |

**Negative assertions: all clean.** No forbidden flag latched; no forbidden
action fired. This matters more than the positive column — four of five
documented FDIR failures were wrong-action failures, and a suite with only
positive assertions cannot catch those.

**Outcome distribution (n=14 injected faults):** recovered 2, contained 7,
detected-only 2, undetected 3, unknown-held 0.

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

## Finding 2 — a fault diagnosed by its consequence, not its cause

`rail_overcurrent` is **undetected**, and the diagnosis layer reports `THERMAL`.

Both halves are real:

- **Undetected is correct and expected.** There is no overcurrent detector, and
  the KySat-2 mechanism is precisely that a rail can drain the battery while
  every fixed voltage threshold stays satisfied (measured in Phase 2: 1.55 A
  draw at 4.85 V, above both thresholds). Asserting a flag here would have
  papered over the gap the case study attributes that spacecraft's loss to.
- **The `THERMAL` diagnosis is a genuine defect.** The extra current heats the
  structure node, the thermal detector fires on the *consequence*, and the
  diagnosis layer confidently names it — while the actual cause goes unnamed.
  This is a smaller cousin of the Delfi-C3 problem: a correct detector firing
  on a real symptom that is not the root cause.

**Both point at the same missing piece: an overcurrent detector on per-rail
current.** That is a software change, but it only works if the hardware provides
the channel — which ties back to Finding 1.

## Finding 3 — gaps the suite exposes rather than hides

| Gap | Why it is undetected | Honest status |
|---|---|---|
| `single sensor corrupt` | No per-channel plausibility check exists for the magnetometer. A single device returning zeros is caught only when it is part of a *bus-wide* pattern. | Real gap. Needs a per-channel range/consistency check. |
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

Seven of fourteen outcomes are `contained` rather than `recovered`. That is not a
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
