"""
Deep-dive case records, hand-authored from primary sources read directly.

PROVENANCE RULE FOR THIS FILE: every `symptom` and `source_text` field below is either
verbatim or a close paraphrase of text in the cited source, which was retrieved and read
during this research session. Fields that the source does not establish are set to
"Unknown / not publicly determined" and are NOT inferred. Analytical fields are tagged
FACT / INFERENCE / HYPOTHESIS.

Primary sources used here:
  [J]  Jacklin, S.A., "Small-Satellite Mission Failure Rates", NASA/TM-2018-220034,
       Appendix A.  https://ntrs.nasa.gov/citations/20190002705
  [LB] Langer, M. & Bouwmeester, J., "Reliability of CubeSats - Statistical Data,
       Developers' Beliefs and the Way Forward", SSC16-X-2, 30th AIAA/USU SmallSat.
       https://repository.tudelft.nl/file/File_a0170151-fe80-4e2d-8182-b2b9ae7a30f2
  [C]  NASA CAPSTONE mission blog, 7 Oct 2022.
       https://www.nasa.gov/blogs/missions/2022/10/07/capstone-team-stops-spacecraft-spin-clearing-hurdle-to-recovery/
  [T]  Thomsen et al., NASA Langley, CubeSat shielding / proton SEE.
       https://ntrs.nasa.gov/citations/20230007190
"""

UNKNOWN = "Unknown / not publicly determined"

CASES = [
    {
        "mission": "CSSWE (Colorado Student Space Weather Experiment)",
        "operator": "University of Colorado Boulder",
        "country": "USA",
        "launch_year": "2012",
        "size": "3U",
        "final_status": "Recovered after ~3 months of total comms loss; mission continued",
        "primary_subsystem": "Communications (radio)",
        "secondary_subsystems": "EPS (battery drain), Radiation-induced",
        "symptom": "Communications lost 6 months after commissioning.",
        "source_text": ("\"Communication with the satellite was lost 6 months after commissioning due to a "
                         "latch up event in the radio. Fortunately, a battery draining anomaly 3 months later "
                         "caused the entire system to power cycle. This cleared the latch up in the radio and "
                         "communications were reestablished.\" [J]"),
        "root_cause": "Single-event latch-up in the radio",
        "root_cause_confidence": "Stated as cause by source",
        "sudden_or_gradual": "Sudden",
        "precursor": UNKNOWN,
        "fdir_behaviour": ("No autonomous recovery documented. The latch-up persisted for ~3 months. "
                            "Recovery occurred incidentally when an unrelated battery-drain anomaly "
                            "power-cycled the whole system."),
        "fdir_worked": "No - no autonomous action cleared the fault",
        "recovery_attempted": "Not autonomously; recovery was accidental",
        "recovery_outcome": "Full recovery (by accident, after ~3 months)",
        "what_ended_mission": "Nothing - mission resumed",
        "opportunity_class": "A - clearly recoverable with existing spacecraft capability",
        "opportunity_reasoning": (
            "FACT: the physical action that cleared the fault was a power cycle, and the spacecraft "
            "demonstrably survived it. INFERENCE: an autonomous rule of the form 'if no ground contact "
            "for N hours, power-cycle the radio' would have applied the same corrective action in hours "
            "rather than months. This is the strongest single piece of evidence in the dataset for the "
            "project's hypothesis: the correct recovery action existed, was within the spacecraft's "
            "capability, was eventually proven to work, and was simply never commanded."),
        "would_ml_have_helped": (
            "No, and this matters. Diagnosis was not the bottleneck - there was no telemetry at all to "
            "analyse, because the radio was the failed element. A learned model has nothing to work with "
            "in a total comms blackout. What was needed was a deterministic timeout-triggered reset, "
            "which is trivially cheap and requires no inference."),
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "High for the facts; the counterfactual recovery timing is INFERENCE",
    },
    {
        "mission": "BIRD",
        "operator": "DLR",
        "country": "Germany",
        "launch_year": "2001",
        "size": "92 kg microsat",
        "final_status": "Partial failure; mission continued in degraded mode",
        "primary_subsystem": "ADCS",
        "secondary_subsystems": "EPS (consequential)",
        "symptom": "Failure of 3 of 4 reaction wheels plus failure of the gyroscope.",
        "source_text": ("\"Failure of 3 of 4 reaction wheels occurred, plus failure of the gyroscope. New "
                         "control software was uploaded to use magnetic torque coil control as a back-up to "
                         "enable the mission to continue with suboptimal direction control. Solar panels not "
                         "always aligned to sun, leading to incomplete charging of power system.\" [J]"),
        "root_cause": "Reaction wheel and gyroscope hardware failures (mechanism not stated)",
        "root_cause_confidence": "Stated as failure; underlying mechanism " + UNKNOWN,
        "sudden_or_gradual": UNKNOWN,
        "precursor": UNKNOWN,
        "fdir_behaviour": ("No autonomous reconfiguration documented. Recovery was achieved by uploading new "
                            "control software from the ground that used magnetic torque coils instead."),
        "fdir_worked": "No autonomous FDIR documented; ground-commanded software reconfiguration succeeded",
        "recovery_attempted": "Yes - ground-uploaded new control law",
        "recovery_outcome": "Partial recovery - mission continued with suboptimal pointing",
        "what_ended_mission": "Mission continued; degraded pointing caused incomplete battery charging",
        "opportunity_class": "C - potentially containable / mission continued in degraded mode",
        "opportunity_reasoning": (
            "FACT: this is a documented case of graceful degradation preserving a mission after near-total "
            "loss of the primary attitude actuator set. It directly supports the project's "
            "FAILURE -> CONTAINMENT -> DEGRADED OPERATION thesis over FAILURE -> SAFE MODE. "
            "INFERENCE: the reconfiguration required a ground-authored new control law, which is well beyond "
            "what a CubeSat could safely synthesise autonomously. The realistic autonomous version is far "
            "weaker: pre-validated fallback control MODES selected autonomously, not new control laws "
            "invented in flight."),
        "would_ml_have_helped": (
            "Not for the recovery itself. Selecting among pre-validated control modes is a deterministic "
            "decision. HYPOTHESIS: an anomaly detector might have flagged degrading wheel behaviour earlier "
            "if wheel current/speed telemetry existed, but the source does not say whether it did."),
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "Medium - the NASA entry is a summary, not a full anomaly report",
    },
    {
        "mission": "QuakeSat",
        "operator": "Stanford University / QuakeFinder",
        "country": "USA",
        "launch_year": "2003",
        "size": "3U",
        "final_status": "Partial failure; mission continued on solar power only",
        "primary_subsystem": "EPS (battery)",
        "secondary_subsystems": "Thermal",
        "symptom": "Both batteries lost 6 months after launch.",
        "source_text": ("\"6 months into launch, both batteries were lost, allowing the mission to continue on "
                         "solar power only. Loss of batteries thought due to high battery temperatures (120 "
                         "degrees Fahrenheit) which may have caused the electrolyte to bake out since the "
                         "batteries were not sealed beyond the normal factory packaging.\" [J]"),
        "root_cause": "Suspected electrolyte loss from sustained high battery temperature (~120 F)",
        "root_cause_confidence": "Suspected ('thought due to', 'may have caused')",
        "sudden_or_gradual": "Gradual (thermal degradation over ~6 months)",
        "precursor": ("Battery temperature was elevated. FACT: the source identifies high temperature as the "
                       "suspected mechanism. Whether that temperature was visible in downlinked telemetry "
                       "before battery loss is " + UNKNOWN),
        "fdir_behaviour": UNKNOWN,
        "fdir_worked": UNKNOWN,
        "recovery_attempted": "No repair possible - batteries physically degraded",
        "recovery_outcome": "Not recoverable, but mission continued sunlit-only",
        "what_ended_mission": "Mission continued in degraded mode",
        "opportunity_class": "B - potentially recoverable with a reasonable additional mechanism",
        "opportunity_reasoning": (
            "INFERENCE: this is the archetype of a slow thermal precursor. If battery temperature was "
            "telemetered, a sustained-over-threshold thermal rule (not a momentary one) could have "
            "triggered load reduction or duty-cycling to lower battery temperature before electrolyte "
            "loss became irreversible. HYPOTHESIS, and an important caveat: if the root cause was simply "
            "inadequate battery sealing exposed to the normal orbital thermal environment, no operational "
            "response would have prevented it - only a hardware fix would. The source is not specific "
            "enough to distinguish these, so this case must NOT be counted as confidently preventable."),
        "would_ml_have_helped": (
            "Marginally. A fixed high-temperature threshold on a battery is the obvious deterministic "
            "detector and needs no learning. An adaptive baseline would arguably be WORSE here: a slowly "
            "rising temperature is exactly the signal an EWMA absorbs as the new normal - the same "
            "structural blind spot measured in this project's own ML evaluation."),
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "Medium-low - root cause is explicitly hedged in the source",
    },
    {
        "mission": "Delfi-C3",
        "operator": "Delft University of Technology",
        "country": "Netherlands",
        "launch_year": "2008",
        "size": "3U",
        "final_status": "Partial failure; degraded operation",
        "primary_subsystem": "CDHS (command & data handling) / Communications",
        "secondary_subsystems": "ADCS (sun sensor)",
        "symptom": ("Radio transponder failed after 9 months. One of two sun sensors failed. Recurrent bus "
                     "data-transmission faults causing zeros in telemetry, arbitrary subsystem switch-off, "
                     "computer resets, or fallback to a limited back-up mode."),
        "source_text": ("\"The radio transponder failed after 9 months. One of two sun sensors failed, but one "
                         "was enough for mission success. The CDHS design has an inherent flaw that often "
                         "prevented data transmission on the bus, leading to either insertion of zero's in the "
                         "telemetry data, arbitrary switch off of subsystems, a reset of the computer or even a "
                         "fall back to a very limited back-up mode.\" [J]"),
        "root_cause": "Inherent CDHS bus design flaw (transponder failure cause not stated)",
        "root_cause_confidence": "Stated as design flaw by source",
        "sudden_or_gradual": "Recurrent/intermittent",
        "precursor": "Zeros inserted into telemetry data preceded/accompanied subsystem misbehaviour (FACT, per source)",
        "fdir_behaviour": ("The system's own protective responses - subsystem switch-off, computer reset, "
                            "fallback to back-up mode - were being triggered by a bus data fault rather than by "
                            "genuine subsystem faults."),
        "fdir_worked": "No - protective responses fired on a misdiagnosed cause",
        "recovery_attempted": "Redundant sun sensor covered the sensor loss",
        "recovery_outcome": "Partial - mission succeeded despite faults",
        "what_ended_mission": "Transponder failure at 9 months",
        "opportunity_class": "C - potentially containable / mission continued in degraded mode",
        "opportunity_reasoning": (
            "FACT: this is a documented case of fault management being triggered spuriously by a fault in "
            "the data path rather than in the subsystems it switched off. It is direct evidence for two of "
            "this project's proposed requirements: (1) a detector must distinguish 'the sensor is bad' from "
            "'the path carrying the sensor's data is bad', and (2) recovery actions must not be blindly "
            "repeated when the triggering signal is itself suspect. INFERENCE: an all-zeros telemetry field "
            "is a highly detectable signature and could plausibly gate protective action - if the bus reads "
            "exactly zero across multiple independent channels simultaneously, the bus is the more likely "
            "fault than simultaneous failure of every subsystem."),
        "would_ml_have_helped": (
            "Possibly, and this is one of the few cases where it genuinely might. Simultaneous all-zeros "
            "across multiple uncorrelated channels is a multivariate signature that a per-channel threshold "
            "would not catch but an isolation-based detector plausibly would - structurally the same as the "
            "sensor_lockup case that this project's own ML evaluation found to be its single strongest "
            "detection result (frozen values driving rolling variance to zero across channels)."),
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "Medium-high for the FDIR misfire; the ML claim is HYPOTHESIS",
    },
    {
        "mission": "KySat-2",
        "operator": "Kentucky Space Consortium",
        "country": "USA",
        "launch_year": "2013",
        "size": "1U",
        "final_status": "Total failure of nominal operations",
        "primary_subsystem": "EPS / Radiation",
        "secondary_subsystems": "C&DH, Radio",
        "symptom": "Radiation-induced latch-up drained the batteries; C&DH and radio reset every hour.",
        "source_text": ("\"Two months after deployment, KySat-2 encountered a radiation-induced latchup that "
                         "drained the batteries. The loss of power caused a reset of the C&DH and radio every "
                         "hour. This ended the nominal operation of the satellite.\" [J]"),
        "root_cause": "Radiation-induced latch-up",
        "root_cause_confidence": "Stated as cause by source",
        "sudden_or_gradual": "Sudden onset, then persistent reset loop",
        "precursor": UNKNOWN,
        "fdir_behaviour": ("Repeated resets occurred but did not clear the condition - the system entered a "
                            "sustained reset loop rather than recovering."),
        "fdir_worked": "No - resets recurred without resolving the underlying latch-up/power condition",
        "recovery_attempted": UNKNOWN,
        "recovery_outcome": "Failed - ended nominal operation",
        "what_ended_mission": "Sustained power loss and reset loop",
        "opportunity_class": "B - potentially recoverable with a reasonable additional mechanism",
        "opportunity_reasoning": (
            "INFERENCE: latch-up is cleared by removing power from the affected device, which is precisely "
            "what a current-limiting latch-up protection circuit (LCL) does autonomously in hardware, at "
            "microsecond timescales, before the battery is drained. Compare CSSWE, where an eventual full "
            "power cycle DID clear an analogous radio latch-up. The distinguishing feature here is that the "
            "reset loop consumed the energy reserve rather than isolating the offending load. "
            "IMPORTANT CAVEAT: this is a HARDWARE capability (per-rail current limiting and load switching), "
            "not a software/ML one. No amount of onboard inference substitutes for it."),
        "would_ml_have_helped": (
            "No. Detection was not the problem - an hourly reset loop is unmissable. The missing capability "
            "was the ability to isolate a specific power rail. This case argues for hardware provisioning in "
            "the project's V1 design, not for a smarter algorithm."),
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "Medium-high",
    },
    {
        "mission": "CAPSTONE",
        "operator": "NASA / Advanced Space / Terran Orbital",
        "country": "USA",
        "launch_year": "2022",
        "size": "12U",
        "final_status": "Recovered; reached target lunar orbit",
        "primary_subsystem": "Propulsion",
        "secondary_subsystems": "ADCS, EPS, OBC",
        "symptom": ("During/after trajectory correction manoeuvre 3 (8 Sep 2022) the spacecraft entered an "
                     "uncontrolled tumble exceeding reaction-wheel authority; safe mode entered; OBC "
                     "periodically resetting; power consumption exceeded generation."),
        "source_text": ("Mission team determined \"the most likely cause was a valve-related issue in one of the "
                         "spacecraft's eight thrusters\" - a partially open valve causing thrust whenever the "
                         "propulsion system was pressurised. Recovery command executed 7 Oct 2022; spacecraft "
                         "\"stopped its spin and regained full 3-axis attitude control\". [C]"),
        "root_cause": "Partially open thruster valve",
        "root_cause_confidence": "Stated as most likely",
        "sudden_or_gradual": "Sudden onset during a commanded manoeuvre",
        "precursor": UNKNOWN,
        "fdir_behaviour": ("Spacecraft autonomously entered safe mode. FACT per source: the OBC was also "
                            "periodically resetting and the power balance was negative, i.e. safe mode alone did "
                            "not stabilise the situation."),
        "fdir_worked": "Partially - safing triggered, but did not arrest the tumble or restore power positivity",
        "recovery_attempted": "Yes - ground-developed new propulsive state machine and GNC thruster controller",
        "recovery_outcome": "Full recovery after ~29 days",
        "what_ended_mission": "Nothing - mission continued to lunar orbit insertion",
        "opportunity_class": "C - potentially containable / degraded operation",
        "opportunity_reasoning": (
            "FACT: recovery required authoring new flight control software on the ground over roughly four "
            "weeks. HYPOTHESIS: this is well beyond plausible autonomous action for a CubeSat, and it would "
            "be dishonest to present CAPSTONE as evidence that autonomy could have fixed it. What CAPSTONE "
            "does support is narrower and still useful: a fault whose signature is 'attitude rates growing "
            "whenever the propulsion system is pressurised' is in principle autonomously *containable* by "
            "inhibiting the offending actuator - isolation rather than repair."),
        "would_ml_have_helped": (
            "Unlikely for diagnosis - the correlation between pressurisation and unwanted thrust is a "
            "deterministic relationship an engineer identified from physics, not a subtle statistical "
            "pattern. Detection was never the issue; the spacecraft knew it was tumbling."),
        "sources": ["https://www.nasa.gov/blogs/missions/2022/10/07/capstone-team-stops-spacecraft-spin-clearing-hurdle-to-recovery/"],
        "source_quality": "Primary (NASA mission blog)",
        "confidence_in_analysis": "Medium - blog-level detail, not an anomaly report",
    },
    {
        "mission": "HORYU-2",
        "operator": "Kyushu Institute of Technology",
        "country": "Japan",
        "launch_year": "2012",
        "size": "7.1 kg",
        "final_status": "Partial failure; ~1 month of operations lost",
        "primary_subsystem": "OBC (both microprocessors)",
        "secondary_subsystems": "Radiation",
        "symptom": "Anomaly prevented experimentation for one month.",
        "source_text": ("\"The HORYU-2 nanosatellite suffered an anomaly due to a single event latchup event "
                         "for one month, during which no experimentation could be done. It is believed a single "
                         "event latch-up (SEL) due to radiation was the most probable cause for both "
                         "microprocessors.\" [J]"),
        "root_cause": "Single-event latch-up affecting both microprocessors",
        "root_cause_confidence": "Believed most probable",
        "sudden_or_gradual": "Sudden",
        "precursor": UNKNOWN,
        "fdir_behaviour": UNKNOWN,
        "fdir_worked": UNKNOWN,
        "recovery_attempted": UNKNOWN,
        "recovery_outcome": "Partial - operations resumed after ~1 month",
        "what_ended_mission": "Mission continued",
        "opportunity_class": "B - potentially recoverable with a reasonable additional mechanism",
        "opportunity_reasoning": (
            "INFERENCE: same family as CSSWE and KySat-2 - a latch-up cleared by power removal. Notable "
            "that BOTH microprocessors were affected, which weakens simple processor redundancy as a "
            "mitigation and strengthens the case for per-device power cycling. The one-month duration "
            "suggests no timely autonomous power-cycle existed."),
        "would_ml_have_helped": "No - deterministic timeout-triggered power cycling is the applicable mechanism.",
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "Medium-low - brief entry, several fields unstated",
    },
    {
        "mission": "ASUSat-1",
        "operator": "Arizona State University",
        "country": "USA",
        "launch_year": "2000",
        "size": "5.9 kg",
        "final_status": "Total failure",
        "primary_subsystem": "EPS",
        "secondary_subsystems": "-",
        "symptom": "Lost power 15 hours after deployment.",
        "source_text": ("\"A problem with the power system prevented solar arrays from charging the batteries. "
                         "Satellite lost power 15 hours after deployment in orbit.\" [J]"),
        "root_cause": "Power system fault preventing solar array charging of batteries",
        "root_cause_confidence": "Stated as cause by source",
        "sudden_or_gradual": "Effectively immediate (15 h to depletion)",
        "precursor": ("INFERENCE: a battery state-of-charge trending monotonically down with no charge current "
                       "for 15 hours is in principle observable, but the source does not state what telemetry "
                       "was received."),
        "fdir_behaviour": UNKNOWN,
        "fdir_worked": UNKNOWN,
        "recovery_attempted": UNKNOWN,
        "recovery_outcome": "Failed",
        "what_ended_mission": "Battery depletion",
        "opportunity_class": "E - insufficient information",
        "opportunity_reasoning": (
            "HYPOTHESIS ONLY: if the fault was a configuration or switching problem, early detection of "
            "zero charge current might have permitted a corrective reconfiguration. If it was a wiring, "
            "deployment or hardware defect, no autonomy would have helped. The source does not distinguish "
            "these, so this case is deliberately classified E rather than being counted as an opportunity. "
            "Classifying it optimistically would be exactly the bias this study is meant to avoid."),
        "would_ml_have_helped": "Unknowable from available information.",
        "sources": ["https://ntrs.nasa.gov/citations/20190002705"],
        "source_quality": "Primary (NASA TM)",
        "confidence_in_analysis": "Low - deliberately so",
    },
]

# Failure-mode taxonomy counts derived from the reproducible Jacklin classification.
# Regenerate with: python research/analysis/jacklin_appendix_analysis.py
JACKLIN_CLASSIFICATION = {
    "entries_analysed": 198,
    "counts": {
        "No identifiable technical cause stated in source": 125,
        "Failure inferred only from absence of publications (weak evidence)": 35,
        "Never heard from / no usable telemetry": 32,
        "Power / EPS": 27,
        "ADCS": 22,
        "Communications": 21,
        "Software / OBC": 15,
        "Deployment / mechanical (true failures)": 11,
        "Radiation (explicitly attributed)": 5,
        "Thermal": 4,
        "Mission continued in degraded mode": 4,
    },
    "note": ("Categories overlap by design; percentages do not sum to 100. Keyword classification of "
             "PDF-extracted prose - approximate. SOURCE-DERIVED INFERENCE, not statements by the source."),
}

SOURCES = [
    {
        "id": "J",
        "citation": "Jacklin, S.A., 'Small-Satellite Mission Failure Rates', NASA/TM-2018-220034, NASA Ames, March 2019",
        "url": "https://ntrs.nasa.gov/citations/20190002705",
        "type": "Primary - NASA Technical Memorandum",
        "used_for": "198-mission failure list (Appendix A); headline failure rates 2000-2016",
        "key_figures": "41.3% failed or partially failed; 24.2% total, 11% partial, 6.1% launch vehicle",
    },
    {
        "id": "LB",
        "citation": "Langer, M. & Bouwmeester, J., 'Reliability of CubeSats - Statistical Data, Developers' Beliefs and the Way Forward', SSC16-X-2, 30th AIAA/USU Conference on Small Satellites, 2016",
        "url": "https://repository.tudelft.nl/file/File_a0170151-fe80-4e2d-8182-b2b9ae7a30f2",
        "type": "Primary - peer-reviewed conference paper",
        "used_for": "CubeSat Failure Database statistics; subsystem contribution over time; DOA analysis",
        "key_figures": ("178 CubeSats to 30/06/2014, 70 failures. Reliability 87.09-75.62% immediately (DOA), "
                        "73.24-58.94% at 100 days, 65.49-48.49% at 2 years (95% CI). EPS >40% of failures after "
                        "30 days; COM ~30% after 90 days; ADCS+PL+STR <10%. ~half of DOA cases unknown cause. "
                        "Developer optimism bias: 48.98% vs 16.53% mean estimated failure likelihood."),
    },
    {
        "id": "C",
        "citation": "NASA, 'CAPSTONE Team Stops Spacecraft Spin, Clearing Hurdle to Recovery', NASA Missions Blog, 7 Oct 2022",
        "url": "https://www.nasa.gov/blogs/missions/2022/10/07/capstone-team-stops-spacecraft-spin-clearing-hurdle-to-recovery/",
        "type": "Primary - NASA mission blog",
        "used_for": "CAPSTONE anomaly and recovery timeline",
        "key_figures": "Partially open thruster valve; tumble beyond wheel authority; ~29 days to recovery",
    },
    {
        "id": "T",
        "citation": "Thomsen, L. et al., NASA Langley Research Center, CubeSat proton SEE / shielding studies",
        "url": "https://ntrs.nasa.gov/citations/20230007190",
        "type": "Primary - NASA technical presentation",
        "used_for": "Radiation shielding physics; assessment of attitude-based mitigation",
        "key_figures": ("CubeSat walls 0.204-0.254 cm, 0.550-0.686 g/cm2. 3U thin-wall 0.907 g/cm2 -> 36.2 MeV "
                        "proton threshold. Shields-1 21.3 g/cm2 -> ~151 MeV. SEE-relevant protons >=100 MeV."),
    },
]
