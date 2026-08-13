# Case study → V0 traceability

**Status:** V0, pre-hardware. Every "met" below means *met in simulation, verified by
an automated test*. Nothing here has run on hardware, because there is no hardware yet.

This document exists to answer one question without anybody having to read the code:
**which of the requirements derived from real CubeSat failures did V0 actually
implement, and which did it not?**

The requirements are not invented. They come from
[`research/cubesat_failure_case_study.md`](../../research/cubesat_failure_case_study.md)
§18, where each one is derived from a named mission failure. The evidence column there
is the missions; the evidence column here is the code and the tests.

---

## Summary

| | Count |
|---|---|
| Met, verified by test | **11 of 11** |
| Met with a stated limitation | 1 (R8 — see below) |

All eleven are now implemented and test-backed. **R8 carries a limitation that must
travel with it:** the requirement says degraded modes shall be *pre-validated*, and
pre-validated means *measured*. Nothing has been measured, because no hardware exists.
The mechanism, the ladder and the selection logic are built and tested; the power
budgets are **declared engineering estimates**. `fdir/degraded.py` marks every
capability set `declared_only=True`, and a test fails if that is quietly flipped
without updating this document in the same commit.

Read "11 of 11" as *the software is complete against these requirements*, not as
*validated*. V1 substitutes measurements for estimates.

---

## 1. Requirement-by-requirement

### R1 — Every autonomous action passes a deterministic safety gate
**Evidence in case study:** existing project principle, unchallenged by the data.
**Status: MET.**

No recovery action reaches hardware without deterministic authority. `FDIREngine`
emits a `RecoveryIntent`; `RecoveryExecutor` is the only thing that commands a port.
The gate is `RECOVERY_AUTHORITY_FLAGS` in [`fdir/engine.py`](../../fdir/engine.py) —
an explicit allow-list of flags permitted to authorise an action.

- Code: [`fdir/engine.py`](../../fdir/engine.py) (`RECOVERY_AUTHORITY_FLAGS`),
  [`fdir/executor.py`](../../fdir/executor.py), [`fdir/ports.py`](../../fdir/ports.py)
- Tests: `tests/test_recovery_seam.py`, `tests/test_diagnosis.py::test_unknown_anomaly_flag_is_raised_and_no_action_is_taken`

The engine-emits-intents split is what makes this checkable at all: `FDIREngine.tick()`
is a pure function of state, so "could this flag have caused that action" is answerable
by reading one constant instead of tracing call sites.

---

### R2 — Every recovery action has an explicit verification condition
**Evidence in case study:** KySat-2.
**Status: MET.**

Every rung of a recovery campaign carries a `VerifyCondition` that is checked against
*subsequent telemetry*, not against whether the command was accepted. This is the
distinction KySat-2 turned on: a command that is accepted is not a fault that is fixed.

- Code: [`fdir/recovery.py`](../../fdir/recovery.py) (`VerifyCondition`, `Rung`),
  `FDIREngine._advance_campaign`
- Test: `tests/test_recovery_phase5.py::test_verification_is_observed_not_assumed_from_command_acceptance`

`RecoveryExecutor` deliberately does **not** decide success. It reports
`engine.note_action_completed(now, accepted)` and nothing more; the engine decides,
from telemetry, in a later tick.

---

### R3 — A failed recovery action must not be blindly repeated; escalate instead
**Evidence in case study:** KySat-2.
**Status: MET.**

Campaigns are ladders of *distinct* rungs with bounded retries per rung. Exhausting
the ladder raises `RECOVERY_FAILED` rather than looping.

- Code: [`fdir/recovery.py`](../../fdir/recovery.py) (`Campaign`, `CampaignState`),
  `comms_loss_ladder()`
- Tests: `tests/test_recovery_phase5.py::test_escalation_moves_through_distinct_rungs_not_blind_repetition`,
  `::test_unrecoverable_fault_is_bounded_and_ends_in_recovery_failed`

Campaign state is NVM-persisted and resumes at rung *k+1*, so a reset mid-campaign
does not restart the ladder from the bottom — the failure mode that turns a bounded
retry into an unbounded one across reboots.

- Tests: `::test_reset_midcampaign_resumes_at_next_rung_and_remembers_attempts`,
  `::test_restored_campaign_past_its_last_rung_is_exhausted_not_restarted`,
  `::test_unreadable_persisted_state_is_discarded_not_trusted`

---

### R4 — Recovery paths must not depend solely on the subsystem they recover
**Evidence in case study:** CSSWE.
**Status: MET.**

`comms_loss_ladder()` escalates *away* from the radio: reset the radio device →
power-cycle the radio rail → system reset. The final rung does not route through the
radio at all. CSSWE is the case where the correct action existed, was proven to work,
and was never commanded — the ladder's last rung is the one that does not need the
failed subsystem to be alive.

- Code: [`fdir/recovery.py`](../../fdir/recovery.py) `comms_loss_ladder()`
- Test: `tests/test_recovery_phase5.py::test_escalation_moves_through_distinct_rungs_not_blind_repetition`

---

### R5 — Loss of ground contact is itself a fault condition with an autonomous response
**Evidence in case study:** CSSWE.
**Status: MET.**

`COMMS_LOSS` is a first-class detector with recovery authority, and
`COMMS_RECOVERY_TRIGGER_S` (30 s) is deliberately distinct from
`COMMS_LOSS_TIMEOUT_S` (5 s): flagging the condition and acting on it are separate
decisions with separate thresholds, so a brief dropout raises awareness without
commanding a radio power-cycle.

- Code: [`fdir/config.py`](../../fdir/config.py), `FDIREngine.note_link_state()`
- Scenario: `communication_loss` in [`scenarios/runner.py`](../../scenarios/runner.py)
- Tests: `tests/test_safety_review_regressions.py::test_an_open_but_silent_link_is_comms_loss`,
  `::test_the_comms_timeout_is_actually_exercised`

**This claim was weaker than it looked until 2026-08-12.** The safety review (J1/K1)
found that `note_link_state()` short-circuited on a `connected` boolean the transport
computed as "a socket object exists", so a link that was open but silent — the exact
failure this requirement exists for — could never latch `COMMS_LOSS`. The scenario
suite missed it because the harness fed the engine a clean `link_healthy` verdict
rather than the heartbeat a real transport produces. Both are fixed, and the fix is
verified live over the real transport rather than only in the harness: a ground
station that receives but never transmits latches at ~4.9 s against a 5.0 s timeout,
while the same link with a heartbeat stays clean.

---

### R6 — Detectors must distinguish subsystem fault from data-path fault
**Evidence in case study:** Delfi-C3.
**Status: MET.**

This one was a live defect (D3) before Phase 4: the engine diagnosed a dead I²C bus as
a dead IMU, which is precisely the Delfi-C3 misdiagnosis. `_suspect_devices()` /
`_update_data_path()` / `_channel_trusted()` now discriminate by bus membership, and
`diagnose()` ranks `DATA_PATH` above the symptoms beneath it.

- Code: [`fdir/engine.py`](../../fdir/engine.py), [`fdir/diagnosis.py`](../../fdir/diagnosis.py)
- Tests: `tests/test_diagnosis.py::test_bus_failure_is_diagnosed_as_the_path_not_the_devices`,
  `::test_diagnosis_prefers_the_path_over_the_symptoms_beneath_it`,
  `::test_bus_failure_does_not_command_safe`
- Scenario pair: `data_bus_failure` vs. a single-device partner — one device failing
  correctly *is* a device fault, so the pair is what proves isolation rather than
  detection.

---

### R7 — Slow-drift detection must use a fixed reference, not only an adaptive one
**Evidence in case study:** QuakeSat, plus this project's own measured EWMA blind spot.
**Status: MET.**

A reference is captured at commissioning from clean samples, and drift is measured
against *that* rather than against a baseline learned in flight. An adaptive baseline
follows the signal, so a slow enough decline becomes the new normal — measured at 0%
recall on `gradual_drift` in the ML evaluation. A fixed reference cannot be talked
into moving.

- Code: [`fdir/engine.py`](../../fdir/engine.py) `_update_reference_drift()`,
  `export_reference_state()` / `import_reference_state()`
- Config: `REFERENCE_CAPTURE_SAMPLES`, `DRIFT_FROM_REFERENCE_V` (0.25 V = 12.5σ
  against measured 0.02 V noise), `DRIFT_DEBOUNCE_S`
- Tests: `tests/test_r7_r8.py::test_a_drift_the_adaptive_baseline_absorbs_is_still_caught`,
  `::test_the_reference_survives_a_reboot`
- Scenario: `gradual drift` — **detected at 13.60 s, diagnosed `DEGRADATION`**, having
  spent every prior phase as the suite's honest `undetected` row.

**The persistence half is the load-bearing half.** The reference is written to NVM and
restored on boot rather than recaptured. Recapturing would let a reboot part-way
through a drift adopt the drifted value as normal and silence the detector exactly
when it mattered — D2's defect wearing a different hat. The scenario forbids
`UNDERVOLTAGE_CRITICAL` because this drift ends at 4.30 V and never reaches the 4.0 V
threshold: catching it there would mean not catching it.


---

### R8 — Degraded modes must be pre-validated and autonomously selectable
**Evidence in case study:** BIRD, Odin, QuakeSat.
**Status: MET — mechanism built and tested; capability sets DECLARED, not measured.**

`Mode.DEGRADED` sits between NOMINAL and SAFE, because a vehicle whose only options
are "fully working" and "stopped, waiting for the ground" throws away every
mission-hour a reduced configuration could still have earned. That gap, not the
absence of a SAFE mode, is BIRD's actual lesson.

- Code: [`fdir/degraded.py`](../../fdir/degraded.py) (`CapabilitySet`, `LADDER`,
  `select_level`), `FDIREngine._update_degraded_mode()`, `restore_capability()`
- Tests: `tests/test_r7_r8.py` — 14 covering the ladder, selection and authority
- Scenario: `gradual drift` now ends `contained` with **1 action** — the payload rail
  shed autonomously. R7 detects the degradation; R8 responds proportionately.

Three constraints are asserted rather than assumed:

- **The OBC is never shed.** A configuration without the flight computer is not a
  degraded mode, it is an ending.
- **The radio survives to the last rung.** CSSWE: the one asset that must survive is
  the one the ground needs in order to intervene at all.
- **No advisory flag can cause a downgrade.** Shedding a subsystem changes what the
  spacecraft can do, so it goes through a named gate exactly like SAFE and recovery.
  `ADAPTIVE_ANOMALY` and `ML_ANOMALY` appear nowhere in `DEGRADE_TRIGGERS`.

Downgrade is autonomous; **upgrade is not**. The conditions that forced a downgrade
are the ones the vehicle is worst placed to judge resolved, and silently restoring
payload power is how a spacecraft oscillates. An operator calls
`restore_capability()`, which is refused while the cause is still present — the same
evidence discipline as SAFE exit (R9).

**The limitation, stated plainly.** Pre-validated means measured. The budgets
(2.00 / 1.60 / 1.10 W) are estimates. And worth keeping visible: BIRD's degradation
was *ground-authored*. No mission in the studied set selected a degraded configuration
autonomously, so this is research rather than replication — derived from what the
failures suggest, not from a flight-proven pattern.


---

### R9 — Safe mode must have an exit strategy, not only an entry condition
**Evidence in case study:** CAPSTONE.
**Status: MET.**

Exiting SAFE requires **both** that the triggering condition has cleared for a full
evidence window **and** an explicit `RESET_FAULTS`. Clearing the physical condition
alone is not sufficient, and neither is the command alone.

- Code: `FDIREngine.reset_faults()` → returns `(cleared, still_latched)`;
  `_has_cleared()` / `_clean_ticks`
- Tests: `tests/test_fdir.py::test_exit_safe_mode_requires_condition_clear_and_reset_faults`

Verified live in Phase 8: after the injected undervoltage was cleared, the vehicle
stayed in SAFE with `UNDERVOLTAGE_CRITICAL` latched, correctly.

D2 and D4 were both defects against this requirement — `reboot → RESET_FAULTS →
EXIT_SAFE_MODE` used to return a still-faulted vehicle, and `RESET_FAULTS` used to
ACK `ACCEPTED` even when it cleared nothing. Both fixed in Phase 0;
`AckStatus.REJECTED_CONDITION_STILL_ACTIVE` now exists so the ground station is told
the truth.

---

### R10 — The system must represent "cause unknown" explicitly and act conservatively
**Evidence in case study:** 63% of the record — the single largest bucket.
**Status: MET.**

The representation exists and is honest: `diagnose()` returns `Cause.UNKNOWN` rather
than inventing a cause, `UNKNOWN_ANOMALY` latches when something is flagged and no
rule explains it, and `Diagnosis.authorises_action` requires `LIKELY` confidence, so
an unknown cause authorises nothing.

- Code: [`fdir/diagnosis.py`](../../fdir/diagnosis.py), `FDIREngine` (`_UNEXPLAINED_FLAGS`)
- Tests: `tests/test_diagnosis.py::test_advisory_only_evidence_yields_unknown_not_an_invented_cause`,
  `::test_unknown_anomaly_flag_is_raised_and_no_action_is_taken`

**Closed 2026-08-12.** This was the most consequential gap on the page: `unknown_held`
was 0 across every scenario, so "acts conservatively when it does not know" was an
asserted property rather than a demonstrated one — for the requirement tracing to the
*largest* category in the dataset.

The `unexplained transient` scenario now drives it end to end. The injected
perturbation is sized to be **measurable and unexplainable**: 0.15 V is many σ against
the variance the adaptive baseline learns from quiet data, but leaves the bus at
~4.85 V — above the warning threshold, and inside the commissioning-reference band, so
every deterministic rule stays silent. Result: **detected at 0.30 s, no cause named,
`unknown_held`, zero actions.**

The assertion that carries the weight is the forbid list. When the spacecraft cannot
explain what it sees it must take no autonomous action at all, because an invented
diagnosis authorising a wrong action is the Delfi-C3 failure.

---

### R11 — ML must not command irreversible actions
**Evidence in case study:** unchallenged; reinforced by §14.
**Status: MET.** *(The only requirement already satisfied before this work began.)*

`ML_ANOMALY` and `ADAPTIVE_ANOMALY` appear in neither `SAFE_MODE_TRIGGER_FLAGS` nor
`RECOVERY_AUTHORITY_FLAGS`. They can raise a flag, appear in telemetry, and inform an
operator. They cannot change mode and cannot command an action.

- Code: [`fdir/engine.py`](../../fdir/engine.py)
- Tests: `tests/test_fdir.py::test_ml_advisory_never_autonomously_forces_safe`,
  `::test_advisory_only_flag_cannot_command_safe_at_end_of_boot`,
  `tests/test_timeline.py::test_advisory_flags_never_overlap_the_authority_sets`
- Firmware: the exported C header's `isolation_forest_is_anomalous()` carries the
  same constraint in its comment block.
- Ground station: since Phase 8 the dashboard renders flags grouped by *authority*,
  so an advisory anomaly no longer looks identical to a critical undervoltage.

The boundary is enforced in three places and asserted in two test files, deliberately.
Of the five documented FDIR failures in the case study, four were failures of
authority, isolation, or verification — not of detection.

---

## 2. The findings that shaped the architecture, not just the requirements

Three results from the case study changed design decisions rather than adding
requirements. Recorded here because they are invisible in the code:

**63% of NASA-catalogued CubeSat failures have no stated technical cause, and 16% of
missions were never heard from at all.** This bounds what *any* onboard autonomy can
address, and it is why R10 exists and why no percentage-of-failures-solved claim
appears anywhere in this repository.

**Four of five documented FDIR failures were failures of authority, isolation, or
verification — not detection.** This cuts directly against the project's founding
instinct that better detection is the lever. It is why Phases 3–5 (ports, diagnosis,
recovery campaigns) came before Phase 7 (ML), and why ML was scheduled last and
non-blocking.

**CSSWE is one clean, validated opportunity:** the correct recovery action existed,
was proven to work, and was never commanded autonomously. One case is not a trend,
and it is presented as one case.

---

## 3. What V0 deliberately did not build

- **ML #2** — case study §19 concludes it is not justified yet. A clean interface
  seam exists; no implementation. Deliberate, per the brief.
- **Attitude-based radiation mitigation** — rejected as unsupported by §9.
- **ML-driven diagnosis as a primary capability** — deferred, not dismissed (§14).

---

## 4. Verification status of the claims on this page

Everything marked MET has an automated test named above. Suite: **249 passing**.

What that does and does not prove:

- It proves the logic behaves as specified **against a simulator I also wrote**.
  A shared misconception between simulator and engine would not be caught.
- It does not prove any timing number transfers to hardware. Detection latencies
  measured here are simulation latencies.
- The scenario suite's outcomes are in `docs/architecture/v0-scenario-results.md`,
  including the scenarios where the finding *is* that nothing latched.

---

## 5. Open gaps, ranked

| Gap | Requirement | Why it matters |
|---|---|---|
| No scenario drives the UNKNOWN path end-to-end | R10 | Traces to 63% of the dataset — the largest bucket, least demonstrated |
| No fixed-reference drift detection | R7 | Adaptive-only baselines learn slow drift as normal |
| No degraded modes | R8 | Response granularity is still the whole vehicle |
| No independent adversarial pass over the review's own conclusions | — | Both review rounds were one reviewer auditing their own code; 3 of 10 findings only surfaced because fixing something else disturbed them |

*(The adversarial safety review itself is done — two rounds, ten findings, all fixed.
See `docs/architecture/v0-adversarial-safety-review.md`.)*
