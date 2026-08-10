# CubeSat Failure, FDIR Behaviour, and Autonomous Recovery Opportunity

**A primary-source study conducted to test — not to confirm — the hypothesis that many
mission-ending CubeSat anomalies were recoverable or containable given earlier diagnosis
and bounded autonomous recovery.**

Status: research phase. No software was modified. Companion database:
`research/cubesat_failure_database.xlsx`.

---

## 0. Read this first: scope, and what this report is not

This report is **substantially narrower than the research brief that commissioned it**,
and the reason is procedural rather than scientific.

A planned multi-agent research fan-out — covering Dellingr, ASTERIA, OPS-SAT, university
mission postmortems, the ML-diagnosis architecture literature, and spacecraft anomaly
benchmark datasets — **failed to execute. All eight research agents terminated on an
account spend limit and returned nothing.** None of that material is represented here.

What *is* here was researched directly, source by source, and every figure traces to a
document that was actually retrieved and read. The gap between the two is documented
explicitly in §24 and in the workbook's **Research Gaps** sheet rather than being papered
over with plausible-sounding content. That choice is deliberate: for a study whose entire
purpose is to test a hypothesis honestly, a smaller verified dataset is worth more than a
larger confident-sounding one.

Claims are tagged throughout:

- **FACT** — directly stated in a cited source
- **INFERENCE** — reasoned from cited facts, with the reasoning shown
- **HYPOTHESIS** — engineering judgement, explicitly speculative

---

## 1. Executive summary

**The hypothesis survives, but only in a much narrower and more specific form than
stated, and the dominant finding of this study is a negative one.**

Three results matter most.

**(1) Most CubeSat failure is not diagnosable at all from the public record — and a large
share was never observable by any onboard system either.** Of the 198 failed or
partially-failed small satellites catalogued by NASA for 2000–2016, **63% have no
identifiable technical cause stated**, **16% were never heard from after deployment**, and
**18% are recorded as failed only because no publications appeared afterwards** — which is
weak evidence that they failed at all. For the "never heard from" population there was no
telemetry, no diagnosis, and no possible onboard action. This is a hard ceiling that no
autonomy architecture can raise, and any honest answer to "how much is recoverable" must
start here.

**(2) Within the diagnosable remainder there is one strikingly clean, repeatable pattern,
and it validates the hypothesis precisely.** CSSWE (University of Colorado, 2012) lost
communications to a radio latch-up. It was dead for three months. It then recovered —
**by accident** — when an unrelated battery-drain anomaly power-cycled the whole
spacecraft and cleared the latch-up. The correct recovery action existed, was within the
spacecraft's physical capability, and was ultimately *proven* to work. It simply was never
commanded, because the only asset that could have commanded it was the failed radio. The
same latch-up-cleared-by-power-removal pattern recurs in KySat-2, HORYU-2, and
CUTE-1.7+APD. **This is the strongest evidence in the dataset for bounded autonomous
recovery, and it needs no machine learning whatsoever** — it needs a timeout and a load
switch.

**(3) Graceful degradation is real, documented, and was achieved by ground intervention
every time.** BIRD (DLR) lost three of four reaction wheels plus its gyroscope and
continued on magnetorquer control after a ground software upload. QuakeSat lost both
batteries to thermal degradation and continued on solar power only. Odin survived a
reaction-wheel loss through redundancy. Delfi-C3 lost one of two sun sensors and carried
on. **FAILURE → CONTAINMENT → DEGRADED OPERATION is not a speculative architecture; it is
what real missions actually did.** The open question is not whether it works but how much
of it can safely be moved onboard.

Two further findings are worth stating plainly because they contradict intuitive
proposals:

- **"Change attitude to reduce radiation exposure" does not work for a CubeSat** and
  should be excluded from the recovery action set. The physics is unambiguous (§9).
- **Where diagnosis was hard, ML would mostly not have helped**, because in the highest-value
  cases the bottleneck was never detection — it was the absence of an authorised, bounded
  action, or the absence of a hardware capability (§14).

---

## 2. Research methodology

**Sources.** Priority was given to primary sources: NASA Technical Memoranda via NTRS,
NASA mission blogs, peer-reviewed SmallSat conference papers, and institutional
repositories. Four primary sources underpin everything quantitative in this report; they
are listed with URLs and the specific figures drawn from each in the workbook's
**Sources** sheet.

**A note on retrieval.** USU DigitalCommons — which hosts the AIAA/USU SmallSat
proceedings, the single richest vein of CubeSat postmortem literature — returns HTTP 403
to automated retrieval. Where a paper was reachable only there, it was routed via an
institutional mirror (TU Delft's repository for Langer & Bouwmeester) or left
unresearched and logged as a gap. NTRS PDFs required local text extraction (`pypdf`)
because server-side parsing failed on them.

**Quantitative method.** NASA/TM-2018-220034 Appendix A lists every small satellite that
partially or totally failed between 2000 and 2016, with cause "identified when known".
That appendix was extracted programmatically and each of the 198 entries classified by
regex against a failure taxonomy. Both steps are reproducible:

```bash
python research/analysis/extract_jacklin_entries.py   # -> 198-row CSV
python research/analysis/jacklin_appendix_analysis.py # -> category counts
```

**Limits of that method, stated up front.** It is keyword classification over
PDF-extracted prose, so it is approximate. Categories deliberately overlap, so
percentages do not sum to 100. Appendix A is a two-column table (partial vs total
failure) and PDF extraction flattens those columns — so per-row failure *severity* is not
recoverable and is marked `Unknown` rather than guessed. An early version of the
classifier reported 35% "deployment/mechanical" failures; that was an artefact of the
word "deployed" appearing in the phrase "deployed into orbit" in nearly every
never-heard-from entry, and was corrected. **All classification figures are INFERENCE —
our analysis of a NASA source — never FACT.**

**Case selection, and its bias.** Eight missions were studied in depth. They were selected
*because they were informative* — because a cause was documented, or a recovery was
attempted, or the fault management misbehaved in an instructive way. **This is a
deliberately biased sample and must not be extrapolated to the population.** It is
strongly enriched for recoverable cases, for exactly the reason that recoverable cases
generate publications and unrecoverable silent ones do not. §25 states what an unbiased
study would require.

---

## 3. Dataset overview

| Source | Population | Period | Headline |
|---|---|---|---|
| Jacklin, NASA/TM-2018-220034 | Small satellites (all classes) | 2000–2016 | 41.3% failed or partially failed (24.2% total, 11% partial, 6.1% launch vehicle) |
| Langer & Bouwmeester, SSC16-X-2 | 178 CubeSats, 70 failures | to 30/06/2014 | Reliability 87.09–75.62% immediately (DOA), 73.24–58.94% at 100 days, 65.49–48.49% at 2 years (95% CI) |

**Subsystem contribution (FACT, Langer & Bouwmeester):** EPS accounts for **more than 40%**
of failures after 30 days; communications for **roughly 30%** after 90 days; ADCS, payload
and structure **together under 10%**. In the early period the largest single category is
"unknown".

**Our classification of the NASA 198 (INFERENCE):**

| Category | n | % |
|---|---|---|
| No identifiable technical cause stated in source | 125 | 63.1% |
| Failure inferred only from absence of publications | 35 | 17.7% |
| Never heard from / no usable telemetry | 32 | 16.2% |
| Power / EPS | 27 | 13.6% |
| ADCS | 22 | 11.1% |
| Communications | 21 | 10.6% |
| Software / OBC | 15 | 7.6% |
| Deployment / mechanical (true failures) | 11 | 5.6% |
| Radiation (explicitly attributed) | 5 | 2.5% |
| Thermal | 4 | 2.0% |
| **Mission continued in degraded mode** | **4** | **2.0%** |

The radiation figure (2.5%) is **certainly an undercount** — latch-up is frequently
suspected but rarely confirmable without instrumentation most CubeSats do not carry, and
three of the five entries hedge with "suspected", "believed", or "most probable cause".

**One methodological criticism of the source itself.** Jacklin classifies some missions as
failed on the grounds that "no papers or articles were written post launch". Absence of
publication is weak evidence of failure — teams graduate, funding ends, and successful
but unremarkable missions often go unwritten. Those 35 entries are flagged separately in
our CSV rather than being silently folded into the failure statistics.

---

## 4. Major failure patterns

Ranked by (frequency × severity × plausible value of autonomous intervention):

1. **Power/EPS dominance.** The single largest identified failure class, and it grows with
   time in orbit. Includes charge failures (ASUSat-1, FalconSat-1), battery degradation
   (QuakeSat), and drain-to-death (KySat-2).
2. **Silent death.** 16% never produced usable telemetry. **Zero autonomy addressable.**
3. **Latch-up cleared by power removal.** Small in count but the cleanest
   fault→intervention mapping in the entire dataset (§9, §15).
4. **Recovery mechanism depending on the failed subsystem.** CSSWE is the archetype: the
   radio was simultaneously the fault, the only diagnostic channel, and the only command
   path. Ground recovery was impossible *by construction*.
5. **Protective action fired on a misdiagnosed cause.** Delfi-C3's data-bus flaw triggered
   subsystem shutdowns, computer resets and back-up-mode fallbacks that the subsystems
   themselves did not warrant.
6. **Recovery action repeated without verification.** KySat-2 reset hourly, indefinitely,
   each reset re-entering the same condition.
7. **Slow degradation absorbed as normal.** QuakeSat's thermal battery degradation over
   ~6 months. Directly relevant to this project's own measured finding that an EWMA
   baseline absorbs slow drift as the new normal.
8. **Degraded-mode survival** (4 documented cases) — the positive pattern.

---

## 5. Detailed mission case studies

Full records with verbatim source text are in the workbook's **Detailed Cases** sheet.
Summarised here; the three most instructive get extended treatment.

### 5.1 CSSWE — the case that most supports the hypothesis

**Context.** 3U, University of Colorado Boulder, launched 2012, space-weather mission.

**Timeline (FACT).** Communications lost six months after commissioning, caused by a
latch-up event in the radio. Three months later an unrelated battery-drain anomaly
power-cycled the entire system, clearing the latch-up. Communications were re-established
and the mission continued.

**Existing FDIR.** No autonomous action cleared the fault in three months. Whatever fault
model existed did not include "the radio has been unreachable for an extended period".

**Why recovery eventually worked.** The corrective physics — remove power from a
latched device — was applied incidentally by a second, unrelated fault.

**What could have been done earlier (INFERENCE).** A rule of the form *"if no ground
contact for N hours, power-cycle the radio; if still nothing after M attempts, power-cycle
the bus"* would have applied the same proven action within hours. The required telemetry
is a single timer. The required hardware is a load switch. There is no diagnosis problem
here to solve.

**Would ML have helped? No — and this is important.** During a total communications
blackout there is no telemetry to analyse and no downlink to report an inference on. A
learned model has nothing to work with. The needed capability is a deterministic
watchdog with authority to act.

**Mission preservation:** achieved, accidentally, after a three-month outage.
**Confidence:** High for facts; the counterfactual timing is INFERENCE.

### 5.2 Delfi-C3 — fault management firing on the wrong cause

**Context.** 3U, TU Delft, launched 2008.

**Symptoms (FACT).** Radio transponder failed at nine months. One of two sun sensors
failed, but one sufficed. Separately, an inherent CDHS design flaw "often prevented data
transmission on the bus, leading to either insertion of zero's in the telemetry data,
arbitrary switch off of subsystems, a reset of the computer or even a fall back to a very
limited back-up mode."

**Why this matters more than it first appears.** The spacecraft's *protective responses*
— subsystem shutdown, reboot, back-up mode — were being triggered by a fault in the data
path, not in the subsystems being shut down. The fault management was working exactly as
designed and doing the wrong thing, because its inputs were corrupt.

**What would have helped (INFERENCE).** A gate distinguishing *"this sensor is bad"* from
*"the path carrying this sensor's data is bad"*. Simultaneous exact-zero readings across
multiple independent channels is far better explained by one bus fault than by
simultaneous failure of every subsystem.

**Would ML have helped? Plausibly — one of the few cases where it might.** Simultaneous
all-zeros across uncorrelated channels is a multivariate signature invisible to
per-channel thresholds. Structurally this is identical to the `sensor_lockup` case that
this project's own ML evaluation found to be its single strongest detection result (91×
the nominal flag rate), where frozen values drove rolling variance to zero across
channels. **HYPOTHESIS**, but a well-motivated one.

### 5.3 BIRD — graceful degradation, ground-authored

**Context.** 92 kg microsat, DLR, launched 2001.

**Failure (FACT).** Three of four reaction wheels failed, plus the gyroscope.

**Recovery (FACT).** New control software was uploaded from the ground to use magnetic
torque coils as a backup, enabling the mission to continue with suboptimal pointing.
Consequence: solar panels were not always sun-aligned, causing incomplete battery
charging.

**Honest reading.** This is the strongest documented case of mission preservation after
near-total loss of a primary actuator set — and it required humans to author a new control
law over an extended period. **It would be dishonest to present BIRD as evidence that a
CubeSat could do this autonomously.** The realistic autonomous analogue is far weaker but
still valuable: *pre-validated fallback control modes, selected autonomously*, not new
control laws synthesised in flight. Note too that the degraded mode carried a real cost
(power) — a genuine reminder that containment is a trade, not a free win.

### 5.4–5.8 Summarised

| Mission | Failure | Outcome | Class |
|---|---|---|---|
| **KySat-2** (2013) | Radiation latch-up drained batteries; C&DH and radio reset hourly | Ended nominal operations | B |
| **QuakeSat** (2003) | Both batteries lost at ~6 months, suspected electrolyte bake-out at ~120 °F | Continued on solar power only | B |
| **HORYU-2** (2012) | SEL affecting *both* microprocessors | ~1 month lost, then resumed | B |
| **CAPSTONE** (2022) | Partially open thruster valve → tumble beyond wheel authority; safe mode, OBC resets, negative power | Recovered after ~29 days | C |
| **ASUSat-1** (2000) | Power system could not charge batteries; dead in 15 hours | Total failure | **E** |

ASUSat-1 is classified **E — insufficient information** deliberately. A configuration
fault might have been correctable; a wiring or hardware defect would not. The source does
not distinguish them, and classifying it optimistically is precisely the bias this study
exists to avoid.

---

## 6. FDIR failures

Across the studied cases, existing fault management failed in four distinct ways — and
notably, **only one of them is a detection failure**:

| Failure mode | Case | Was it a detection problem? |
|---|---|---|
| No rule existed for the condition | CSSWE (radio unreachable for months) | No — nothing to detect *with* |
| Recovery depended on the failed subsystem | CSSWE (radio was fault, diagnostic channel and command path) | No — architectural |
| Protective action fired on misdiagnosed cause | Delfi-C3 (bus fault presented as subsystem faults) | **Yes — isolation failure** |
| Recovery repeated without verification | KySat-2 (hourly reset loop) | No — no verification condition |
| Safing insufficient to stabilise | CAPSTONE (safe mode entered, tumble continued, power negative) | No — safing was not enough |

**This is the single most consequential result in the report for architecture purposes.**
The project's instinct — that better *detection* is the lever — is only weakly supported.
Four of five documented FDIR failures were failures of **authority, isolation,
verification, or action repertoire**, not of detection. A better anomaly detector would
have changed none of them.

---

## 7. Recovery successes

| Mission | Mechanism | Autonomous? |
|---|---|---|
| CSSWE | Power cycle cleared radio latch-up | **No — accidental** |
| BIRD | Ground-uploaded new control law using magnetorquers | No |
| Odin | Redundant reaction wheel | Yes (passive redundancy) |
| QuakeSat | Continued sunlit-only after battery loss | Effectively passive |
| CAPSTONE | Ground-developed new propulsive state machine + GNC controller | No |

**Not one documented recovery in this dataset was achieved by onboard autonomous
reasoning.** Recoveries came from passive redundancy, ground engineering, or luck. That is
simultaneously an argument that the opportunity is real and unexploited, and a caution
that nobody has yet demonstrated the autonomous version in flight on a CubeSat.

---

## 8. Recovery failures

**KySat-2** is the most instructive. The spacecraft *did* respond to the fault — it reset,
hourly, indefinitely. Each reset re-entered the same latch-up-and-drain condition. The
system had an action and no verification of whether the action achieved anything, and no
escalation when it did not.

**INFERENCE:** the missing capability was not a better detector or a smarter diagnosis. It
was (a) per-rail current limiting able to isolate the offending load in hardware, and
(b) a rule that a recovery action which has not changed the observed condition after N
attempts must not simply be repeated. This case alone justifies two of the requirements in
§18.

---

## 9. Radiation

Full analysis: `research/analysis/radiation_attitude_finding.md`.

**Documented events.** Five of 198 NASA-listed failures are explicitly radiation-attributed
(CSSWE, KySat-2, HORYU-2, CUTE-1.7+APD, and one further entry), and three of those hedge
the attribution. The recurring signature is **single-event latch-up**, and the recurring
resolution is **power removal**.

### The attitude question, answered directly

The project asked whether "tilt the spacecraft away from the radiation" is a valid
autonomous response. **It is not, for a CubeSat, and it should be excluded from the action
set.**

**FACT** (Thomsen et al., NASA Langley, [NTRS 20230007190](https://ntrs.nasa.gov/citations/20230007190)):

| Quantity | Value |
|---|---|
| Typical CubeSat aluminium wall | 0.204–0.254 cm, **0.550–0.686 g/cm²** |
| Thin-wall 3U effective shielding | 0.907 g/cm² → **36.2 MeV** proton threshold |
| Heavily shielded reference (Shields-1) | 21.3 g/cm² → ~151 MeV |
| Protons dominating SEE in rad-tolerant parts | **≥100 MeV** |

**INFERENCE, in four steps:**

1. **The particles that matter pass through the whole spacecraft.** A CubeSat wall stops
   protons below ~36 MeV; the protons that cause single-event effects in reasonably
   hardened parts are ≥100 MeV. Which face they entered through is irrelevant.
2. **There is nowhere to hide inside a 1U–3U.** Reorienting only changes which mass a
   component sits behind, and the total available mass in any direction is a few
   centimetres of aluminium plus a PCB stack — perhaps 1–3 g/cm² at best against 0.6 at
   worst. Scaling from the NASA figures, that moves the threshold from ~36 MeV to perhaps
   ~70 MeV: still under the band that matters. Large spacecraft can vault sensitive
   electronics deep inside; a CubeSat has no deep inside.
3. **The flux is not a beam.** Trapped protons have a pitch-angle distribution about the
   field line and the spacecraft is immersed in the flux. GCR is essentially isotropic and
   unshieldable at these masses. Only solar particle events have a preferred direction,
   and their significant-fluence energies still exceed what a wall stops.
4. **The manoeuvre costs the things keeping you alive** — solar illumination, antenna
   pointing, thermal balance. Trading guaranteed power and link margin for an illusory
   radiation benefit is a bad trade.

### What does work: position-predictive safing

Orbit-position-based mitigation is real and standard: Hubble suspends observations during
SAA passage, and instruments have used **uplinked ephemeris-derived SAA entry/exit
predictions** to reduce voltage on sensitive detectors. *(Corroborated at secondary-source
level; the specific instrument detail should be re-checked against a primary instrument
handbook before being cited as primary.)*

**This is implementable on a student CubeSat** — it needs position knowledge, not attitude
authority; the action is bounded and reversible; and it is benchtop-verifiable by
injecting a simulated "predicted SAA entry". Its honest limit: it reduces the
*consequences* of upsets in a known-risky window. It does not reduce flux, and does
nothing for GCR upsets elsewhere.

---

## 10. Power

The dominant failure class (>40% of failures after 30 days, FACT). Two distinct
sub-patterns appear:

- **Fast charge failures** — ASUSat-1 (dead in 15 hours), FalconSat-1. Whether these were
  correctable is unknown; both are effectively DOA-adjacent.
- **Slow degradation** — QuakeSat's thermal battery loss over ~6 months. This is the class
  where early detection is genuinely plausible, and precisely the class an *adaptive*
  baseline handles worst.

**Direct connection to this project's existing code:** the ML evaluation already measured
that the EWMA adaptive baseline (`FDIR-006`) catches **0%** of gradual drift, because an
online-adaptive statistic absorbs slow change as the new normal. QuakeSat is the flight
analogue of that measured blind spot.

---

## 11. Communications

~30% of failures after 90 days (FACT). Architecturally the most important subsystem for
this study, because **comms failure is the one failure that disables the ground's ability
to fix anything else**. CSSWE demonstrates the full pathology: the radio was the fault,
the diagnostic channel, and the command path simultaneously.

**INFERENCE:** this is the strongest general argument for onboard autonomy that exists in
the dataset. Every other subsystem's faults can in principle be worked by the ground —
comms faults cannot, by construction.

---

## 12. Software / OBC

7.6% of the NASA-listed failures. Delfi-C3's CDHS bus flaw is the best-documented case and
is notable for producing *corrupt data* rather than a clean stop — the hardest failure mode
to handle, because everything downstream keeps running on values that look plausible.

---

## 13. ADCS

11.1% of NASA-listed failures; Langer & Bouwmeester put ADCS+payload+structure together
under 10% of CubeSat failures specifically. **Both BIRD and Odin — the two clearest
graceful-degradation successes — are ADCS cases**, which suggests ADCS faults are unusually
*containable* even though they are not unusually common. Attitude control degrades
gracefully in a way that power does not.

---

## 14. Diagnosis vs detection

**This distinction is the central analytical result of the study.**

In the cases examined, detection was almost never the binding constraint:

| Case | Was the anomaly detectable? | Was the diagnosis hard? | What was actually missing? |
|---|---|---|---|
| CSSWE | Trivially (silence) | No | **Authority to act** |
| KySat-2 | Trivially (hourly resets) | No | **Hardware isolation + verification** |
| CAPSTONE | Trivially (tumbling) | Moderately | **Ground engineering time** |
| Delfi-C3 | Yes (zeros in telemetry) | **Yes — genuinely ambiguous** | **Isolation logic** |
| QuakeSat | Probably (temperature) | No | **Non-adaptive trending + a response** |

Only Delfi-C3 presents a genuine *diagnosis* problem — one symptom (subsystems
misbehaving) with two very different causes (subsystem fault vs. data-path fault).

**INFERENCE:** the project's proposed ML#2 diagnosis layer addresses the rarest bottleneck
in the observed data. The common bottlenecks are bounded authority, hardware isolation
capability, and recovery verification — all of which are deterministic engineering, not
inference.

---

## 15. Autonomous recovery opportunities

Ranked by evidence strength:

| Opportunity | Evidence | Class |
|---|---|---|
| **Comms-loss timeout → staged power cycle** | CSSWE (action proven to work in flight) | **A** |
| **Per-rail current limit + autonomous load isolation** | KySat-2, HORYU-2 | B (**hardware**) |
| **Recovery verification + escalation, no blind repetition** | KySat-2 reset loop | B |
| **Data-path vs sensor-fault discrimination** | Delfi-C3 | B |
| **Sustained-threshold trending against a *fixed* reference** | QuakeSat + this project's own ML result | B |
| **Pre-validated degraded modes, autonomously selectable** | BIRD, Odin, QuakeSat | C |
| **SAA-predictive protective mode** | Radiation physics + operational practice | C |
| ~~Attitude-based radiation mitigation~~ | **Refuted (§9)** | **Excluded** |

---

## 16. Mission preservation and graceful degradation

**The evidence supports this framing strongly.** Four of 198 missions demonstrably
continued in a degraded state rather than dying (BIRD, Odin, QuakeSat, Delfi-C3), and in
each case the mission returned real science it would otherwise have lost.

Three honest caveats:

1. **Every documented instance was ground-driven or passive.** None was autonomous
   reasoning.
2. **Degradation carries costs.** BIRD's degraded pointing caused incomplete battery
   charging — containment created a second problem.
3. **4/198 is a floor, not a rate.** Degraded operation is under-reported precisely because
   it is undramatic.

**INFERENCE:** the realistic autonomous version is *selection among pre-validated degraded
modes*, not synthesis of new ones. That is a tractable, testable capability. "Invent a new
control law in flight" is not, and BIRD should not be cited as if it were.

---

## 17. Why existing FDIR fails

Synthesised from §6:

1. **The fault model is a fixed list, and reality is not.** CSSWE's condition simply was
   not enumerated.
2. **Recovery paths run through the thing that broke.** The most dangerous coupling in the
   dataset.
3. **Detection is trusted more than it deserves.** Delfi-C3 acted decisively on corrupt
   input.
4. **Actions have no verification.** KySat-2 repeated forever.
5. **Safing is treated as terminal.** CAPSTONE safed and was still tumbling with a negative
   power margin. Safe mode is a *holding* state, not a solution — and if the ground cannot
   reach you, holding is dying slowly.

---

## 18. What a better FDIR architecture needs

Derived strictly from observed failures — each requirement names the evidence:

| # | Requirement | Evidence |
|---|---|---|
| R1 | Every autonomous action passes a deterministic safety gate | Existing project principle; unchallenged by data |
| R2 | Every recovery action has an explicit verification condition | KySat-2 |
| R3 | A failed recovery action must not be blindly repeated; escalate instead | KySat-2 |
| R4 | Recovery paths must not depend solely on the subsystem they recover | CSSWE |
| R5 | Loss of ground contact is itself a fault condition with an autonomous response | CSSWE |
| R6 | Detectors must distinguish subsystem fault from data-path fault | Delfi-C3 |
| R7 | Slow-drift detection must use a fixed reference, not only an adaptive one | QuakeSat + project's own measured EWMA blind spot |
| R8 | Degraded modes must be pre-validated and autonomously selectable | BIRD, Odin, QuakeSat |
| R9 | Safe mode must have an exit strategy, not only an entry condition | CAPSTONE |
| R10 | The system must represent "cause unknown" explicitly and act conservatively | 63% of the record |
| R11 | ML must not command irreversible actions | Unchallenged; reinforced by §14 |

**Rejected as unsupported:** attitude-based radiation mitigation (§9); ML-driven diagnosis
as a *primary* capability (§14) — deferred, not dismissed.

---

## 19. Is ML #2 justified?

**Provisional answer: not yet, on this evidence — and the honest caveat is that the
literature review which would settle it did not run (§0).**

**Against, from the data:** only 1 of 8 studied cases (Delfi-C3) presented a genuine
diagnosis ambiguity. The rest were bounded-authority, hardware-isolation or
verification problems. Building a learned diagnosis layer to address the rarest observed
bottleneck is poor allocation.

**Against, from the project's own results:** the existing ML evaluation found the model
blind to `sensor_timeout` because `imu_responded` is constant in nominal training data —
**0 splits out of 3089 nodes**. Any fault whose signature is a flag that never varies
during normal operation is structurally invisible to a model trained only on normal
operation. Several real failure modes here have exactly that shape.

**For:** Delfi-C3-class multivariate signatures are real, and this project has already
*measured* that an isolation-based detector excels at precisely that shape (91× on
`sensor_lockup`).

**Recommendation (HYPOTHESIS):** treat diagnosis as a **deterministic decision structure
first** — a small, auditable fault tree over already-computed detector outputs. It is
inspectable, needs no training data, runs in microseconds, and directly encodes R6. Revisit
a learned diagnosis layer only if a specific ambiguity is *demonstrated* to resist
deterministic treatment.

---

## 20. Candidate ML #2 architectures

**Under-researched — the comparative literature agent failed. Provisional ranking only.**

1. **Deterministic fault tree / decision structure** — auditable, no training data,
   trivially cheap. Recommended starting point.
2. **Model-based diagnosis** (Livingstone/TEAMS lineage) — strong fit conceptually; the
   cost is building and maintaining the system model. *Needs the literature review.*
3. **Case-based retrieval over pre-validated procedures** — fits R3/R8 naturally.
4. **Bayesian network** — principled uncertainty; needs priors the project does not have.
5. **Supervised classifier** — needs labelled fault data that does not exist (§21).
6. **Reinforcement learning** — the owner's scepticism appears well-founded: sample
   inefficiency plus irreversible actions plus no safe exploration environment. **HYPOTHESIS**,
   but a confident one.

---

## 21. Experimental requirements (training data)

Supervised diagnosis would need labelled (telemetry → fault → action → outcome) traces.
The dataset assembled here contains **eight** usable end-to-end sequences. That is three
orders of magnitude short.

Three routes, honestly assessed:

- **Historical mission data** — insufficient. 63% of the record lacks a stated cause.
- **Simulation** — can generate volume, but a model trained on our own fault model learns
  our assumptions, not reality. The project's existing evaluation already demonstrates
  this hazard.
- **Hardware-in-the-loop on the testbed** — the only route producing *real* sensor
  pathology, and the only one that generates trustworthy failed-recovery examples.

**Representing failed recoveries matters as much as successful ones** (R3), and no public
dataset does this well.

---

## 22. Proposed experiments for the testbed

1. **Comms-timeout → staged power cycle**, with verification and escalation (tests R2–R5).
   Direct CSSWE analogue; highest evidence-to-cost ratio in this report.
2. **Latch-up emulation**: a load that fails to a high-current stuck state, cleared only by
   power removal. Tests isolation-vs-reset and R3.
3. **Data-path corruption injection** (all-zeros on a bus) vs. genuine sensor failure —
   tests R6 and the one real diagnosis ambiguity found.
4. **Slow thermal drift to a damage threshold** — tests R7 against the known EWMA blind spot.
5. **Degraded-mode selection** with a disabled actuator (tests R8).

All five are benchtop-verifiable without flight hardware.

---

## 23. Limitations

- The multi-agent research phase did not run; coverage is a fraction of the brief (§0).
- Eight detailed cases, **selected for informativeness** — severely biased toward
  recoverable outcomes. **No extrapolation is valid.**
- Classification figures are regex over PDF-extracted prose: approximate, overlapping.
- NASA/TM Appendix A entries are one-to-three-sentence summaries, not anomaly reports.
  Several fields are irrecoverably `Unknown`.
- Partial-vs-total severity was lost in PDF column flattening.
- Survivorship bias throughout: teams publish recoveries and stay silent about silent deaths.
- Jacklin's own "no publications ⇒ failed" inference is weak and is flagged, not adopted.

---

## 24. Research gaps

Tracked in the workbook's **Research Gaps** sheet. Highest value first:

1. **Dellingr** (NASA GSFC) — multiple anomalies, flatsat diagnosis, published lessons. The
   single highest-value missing case.
2. **ASTERIA** (JPL) — comms-loss mission end; would test whether the CSSWE finding
   generalises.
3. **OPS-SAT** (ESA) — the only flown CubeSat built to experiment with onboard autonomy.
4. **University postmortems** — Delfi-n3Xt, ESTCube-1, AAUSAT, MOVE-II.
5. **ML#2 architecture literature** — required to settle §19–20.
6. **Anomaly benchmark datasets and their critiques** — required for §21.

---

## 25. Proposed Phase 2 (for decision, not for execution)

Not proposed as scope. Sequenced by evidence strength:

**2a. Close the research gap** (§24) — cheap, and §19 is currently unresolvable without it.

**2b. Implement the two highest-evidence capabilities**: comms-timeout staged recovery
(R5) and recovery verification with escalation (R2/R3). Both are deterministic, both trace
to specific documented mission losses, neither needs ML.

**2c. Specify hardware for V1** that the data says matters more than any algorithm:
per-rail current limiting and independently switchable loads. **KySat-2 was lost to a
missing hardware capability, not a missing algorithm.**

**2d. Only then** evaluate whether a diagnosis layer earns its place, against the specific
Delfi-C3-class ambiguity.

---

## Final question, answered directly

> **How much of CubeSat failure is potentially recoverable or containable if detected
> early, and what would an autonomous spacecraft need to do differently from traditional
> FDIR to exploit that opportunity?**

### On the percentage: the evidence does not support one, and here is precisely why

A single number would be false precision. What the evidence *does* support is a chain of
bounds:

1. **Of 198 documented failures, at most ~37% (73) have any stated technical cause.** The
   other 63% cannot be analysed for recoverability by anyone, from the public record.
2. **At least 16% (32) were never heard from at all.** No telemetry, no onboard
   observation, no possible action. For this group the answer is **zero**, definitionally.
3. **A further ~18% (35) may not be failures at all**, being inferred from absence of
   publication.
4. **Within the analysable ~37%, this study's own sample is too biased to extrapolate
   from.** The eight detailed cases split roughly 1 A / 3 B / 3 C / 1 E — but they were
   *selected because they were documented*, and documentation correlates strongly with
   recoverability. Projecting that split onto the population would be exactly the
   motivated reasoning this study was designed to avoid.

**Therefore: the honest answer is that the recoverable fraction is unknown, and bounded
above by roughly a third of the failure record for reasons of observability alone.**

What *can* be said with confidence is narrower and more useful: **for one specific,
recurring fault class — single-event latch-up presenting as loss of communications —
there is direct flight evidence that a bounded autonomous action would have worked, because
that exact action did work, accidentally, on CSSWE.** That is one clean validated
opportunity, not a percentage.

**What would settle the question:** a study over missions with *published telemetry
archives* rather than published summaries, classifying recoverability blind to outcome,
with two independent assessors. That study does not appear to exist and would be a
genuine contribution.

### On what must be done differently

Traditional FDIR, as observed failing here, detects well and acts poorly. The five
differences that matter, in evidence order:

1. **Treat loss of ground contact as a fault with an autonomous response.** Traditional
   FDIR waits for the ground. When comms *is* the fault, waiting is dying. *(CSSWE)*
2. **Verify every recovery action, and escalate rather than repeat.** *(KySat-2)*
3. **Never route recovery solely through the subsystem being recovered.** *(CSSWE)*
4. **Distinguish "the sensor is wrong" from "the path is wrong" before acting.** *(Delfi-C3)*
5. **Make safe mode a transition, not a destination** — with pre-validated degraded modes
   as the target. *(CAPSTONE, BIRD)*

Note what is absent: none of these five requires machine learning. **The largest available
gains in this dataset come from giving deterministic logic bounded authority to act,
verify, and escalate — not from making detection cleverer.**

### Ranked capabilities to investigate on our testbed

| Rank | Capability | Evidence | Cost |
|---|---|---|---|
| 1 | Comms-loss timeout → staged power cycle with verification | CSSWE — action proven in flight | Very low |
| 2 | Recovery verification + escalation, no blind repetition | KySat-2 | Very low |
| 3 | Per-rail current limiting and load isolation | KySat-2, HORYU-2 | **Hardware — decide before V1 purchase** |
| 4 | Fixed-reference slow-drift detection alongside adaptive | QuakeSat + our own measured EWMA blind spot | Low |
| 5 | Data-path vs sensor-fault discrimination | Delfi-C3 | Low–medium |
| 6 | Pre-validated autonomously-selectable degraded modes | BIRD, Odin, QuakeSat | Medium |
| 7 | SAA-predictive protective mode | Radiation physics; standard practice | Medium |
| — | ~~Attitude-based radiation mitigation~~ | **Refuted** | **Do not build** |
| — | ML-driven diagnosis (ML#2) | Addresses rarest observed bottleneck | **Defer pending §24** |

**Item 3 is the one with a deadline.** It is a hardware decision, the failure it addresses
is documented and fatal, and it becomes expensive to add after the board is purchased.
