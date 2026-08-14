"""
Reproducible fault-injection scenarios with measured outcomes.

This is the evidence-producing part of V0. Everything before it built the
capability; this measures what the capability actually does, per fault type,
with the numbers the project committed to reporting rather than asserting.

TWO DESIGN RULES, both taken from the failure research:

1. SCENARIOS COME IN DISCRIMINATION PAIRS. A scenario on its own proves
   detection. Only a pair proves ISOLATION -- two faults with the same
   presenting symptom that must reach different diagnoses. Four of five
   documented FDIR failures were failures of isolation, authority or
   verification, not of missed detection, so a suite of single scenarios would
   measure the thing that was already working.

2. NEGATIVE ASSERTIONS ARE MANDATORY. Every scenario states which flags must
   NOT latch and which actions must NOT fire. A suite with only positive
   assertions cannot catch a wrong-action failure, which is the failure mode
   that actually killed spacecraft.

WHAT THE NUMBERS MEAN, AND DO NOT MEAN. The denominator here is OUR INJECTED
FAULT SET -- nine fault types we chose and modelled. It is not CubeSat failure
in general. The case study bounds that population hard: 63% of the NASA record
has no identifiable cause and 16% was never heard from at all, so no result
from this suite can speak to the "what fraction of real failures is
recoverable" question. The report states that alongside every table rather
than leaving it to be misread.

Run: python scenarios/runner.py
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))

from environment import SpacecraftEnvironment  # noqa: E402
from hardware_sim import SimulatedPowerPort, SimulatedResetPort  # noqa: E402
from fdir import config as cfg  # noqa: E402
from fdir.diagnosis import Cause  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402
from fdir.executor import RecoveryExecutor  # noqa: E402
from fdir.recovery import CampaignState  # noqa: E402
from icd import FaultFlag, Mode, Rail  # noqa: E402

DT = 0.1


# ---------------------------------------------------------------------------
# Outcome classification -- deliberately distinguishes four different things
# that are easy to blur together.
# ---------------------------------------------------------------------------

class Outcome:
    RECOVERED = "recovered"          # fault cleared and verified
    CONTAINED = "contained"          # not cleared, but isolated / vehicle safe
    DETECTED_ONLY = "detected_only"  # seen and correctly diagnosed, no action available
    UNDETECTED = "undetected"        # the honest failure case
    UNKNOWN_HELD = "unknown_held"    # detected, no diagnosis, held safely (R10)
    CLEAN = "clean"                  # nominal control: nothing fired, as required


@dataclass
class ScenarioResult:
    name: str
    injected: str
    detected: bool = False
    detection_latency_s: Optional[float] = None
    diagnosis: Optional[str] = None
    diagnosis_correct: Optional[bool] = None
    diagnosis_latency_s: Optional[float] = None
    recovery_attempted: bool = False
    recovery_attempts: int = 0
    recovery_verified: bool = False
    recovery_latency_s: Optional[float] = None
    final_mode: str = ""
    outcome: str = Outcome.UNDETECTED
    forbidden_violations: List[str] = field(default_factory=list)
    # EVERY cause the system named after injection, not just the first.
    #
    # `diagnosis` records the FIRST known cause, which is fine when the injected
    # fault is the only thing wrong -- and wrong the moment anything else is
    # already latched. Out of ground contact, COMMS_LOSS latches ~5 s after boot
    # and every scenario recorded GROUND_LINK_LOST as "the diagnosis" while the
    # system went on to correctly conclude POWER_UNDERVOLTAGE, THERMAL,
    # SENSOR_FROZEN and so on. The measurement was wrong, not the system.
    #
    # The final cause is no better as a headline: a successfully recovered fault
    # ends with nothing wrong, so "what did it settle on" reads as UNKNOWN for
    # the runs that went best. What the correctness column should mean is DID IT
    # EVER REACH THE RIGHT CONCLUSION, which needs the whole set.
    causes_seen: set = field(default_factory=set)
    notes: str = ""

    @property
    def clean(self) -> bool:
        return not self.forbidden_violations


@dataclass
class Scenario:
    name: str
    inject: Optional[str]
    expect_flags: int                      # must latch (0 = none expected)
    forbid_flags: int = 0                  # must NOT latch
    expect_cause: Optional[Cause] = None
    forbid_actions: bool = False           # no recovery action may be commanded
    seed: int = 1
    latch_clears: bool = True
    duration_s: float = 120.0
    inject_at_s: float = 5.0
    per_rail_sensing: bool = True          # False = blind the diagnosis layer
    # False = no ground station in view, independent of whether the radio works.
    # This is the NORMAL state of a CubeSat for most of every orbit, and the
    # suite had never modelled it: the harness always supplied link_healthy=True
    # unless a scenario specifically injected comms loss. Rounds 7 and 8 both
    # found defects that live exactly in that gap, so the gap is now a dimension
    # rather than an assumption.
    ground_contact: bool = True
    obc_reset_at_s: Optional[float] = None
    note: str = ""


class Harness:
    def __init__(self, scenario: Scenario):
        self.sc = scenario
        self.env = SpacecraftEnvironment(seed=scenario.seed,
                                         latch_clears_on_power_cycle=scenario.latch_clears)
        self.engine = FDIREngine()
        self.executor = RecoveryExecutor(SimulatedPowerPort(self.env),
                                         SimulatedResetPort(self.env))

    def tick(self):
        sample, truth = self.env.step(DT)
        if not self.sc.per_rail_sensing:
            # Simulate a spacecraft WITHOUT per-rail current sensing. This is
            # the counterfactual that turns the hardware question into a
            # measurement instead of an opinion.
            sample.rail_current_a = None
        now = self.env.t
        self.engine.tick(sample, now)
        if self.sc.ground_contact:
            link_established = self.env.link_healthy
            since_contact = sample.seconds_since_ground_contact
        else:
            # Out of view. The radio may be perfectly healthy; there is simply
            # nobody listening, which is what an orbit looks like between
            # passes. Deliberately NOT modelled by breaking the radio -- that
            # would be a different fault.
            link_established = False
            since_contact = now
        self.engine.note_link_state(now, link_established=link_established,
                                    seconds_since_contact=since_contact)
        self.executor.step(self.engine, now)
        return sample, truth


def run_scenario(sc: Scenario) -> ScenarioResult:
    h = Harness(sc)
    r = ScenarioResult(name=sc.name, injected=sc.inject or "none (nominal control)")

    injected_at = None
    boot_ticks = int((cfg.BOOT_DURATION_S + 1.0) / DT)
    for _ in range(boot_ticks):
        h.tick()

    total_ticks = int(sc.duration_s / DT)
    for i in range(total_ticks):
        t = h.env.t
        if sc.inject and injected_at is None and t >= sc.inject_at_s:
            h.env.inject(sc.inject)
            injected_at = t
        if sc.obc_reset_at_s is not None and abs(t - sc.obc_reset_at_s) < DT / 2:
            saved = h.engine.export_recovery_state()
            h.env.obc_reset()
            h.engine = FDIREngine()
            h.engine.watchdog_reset(t)
            h.engine.import_recovery_state(saved, t)

        h.tick()

        if injected_at is not None:
            if not r.detected and (h.engine.fault_flags & sc.expect_flags):
                r.detected = True
                r.detection_latency_s = round(h.env.t - injected_at, 3)
            if h.engine.diagnosis.is_known:
                r.causes_seen.add(h.engine.diagnosis.cause.name)
                if r.diagnosis is None:
                    r.diagnosis = h.engine.diagnosis.cause.name
                    r.diagnosis_latency_s = round(h.env.t - injected_at, 3)

        if h.engine.fault_flags & sc.forbid_flags:
            bad = FaultFlag(h.engine.fault_flags & sc.forbid_flags)
            msg = f"forbidden flag latched: {bad!r}"
            if msg not in r.forbidden_violations:
                r.forbidden_violations.append(msg)

    if sc.forbid_actions and h.executor.history:
        r.forbidden_violations.append(
            f"{len(h.executor.history)} recovery action(s) commanded when none were permitted")

    # --- outcome -----------------------------------------------------------
    r.recovery_attempts = len(h.executor.history)
    r.recovery_attempted = r.recovery_attempts > 0
    c = h.engine.campaign
    if c is not None:
        # A SUCCEEDED campaign is not evidence that THE INJECTED FAULT was
        # recovered -- only that some campaign achieved its verification
        # condition. Out of ground contact the comms ladder opens and succeeds
        # for every scenario (power-cycling a healthy radio "works"), so
        # `undervoltage` reported RECOVERED with UNDERVOLTAGE_CRITICAL still
        # latched and the battery still sagging.
        #
        # That is the worst category of defect this project can have: a measured
        # result that means something other than what it says, in the document
        # that exists to be the evidence. The campaign's trigger must relate to
        # the fault under test.
        relates = bool(int(c.trigger) & sc.expect_flags) if sc.expect_flags else False
        r.recovery_verified = c.state == CampaignState.SUCCEEDED and relates
        if c.state == CampaignState.SUCCEEDED and not relates:
            r.notes = (f"a campaign succeeded but its trigger "
                       f"({FaultFlag(c.trigger)!r}) is unrelated to the injected "
                       f"fault; not counted as recovery")
        if r.recovery_verified and injected_at is not None:
            r.recovery_latency_s = round(h.env.t - injected_at, 3)
    r.final_mode = Mode(h.engine.mode).name

    # Captured BEFORE the display placeholder below overwrites r.diagnosis --
    # the outcome classification needs the fact, not the rendered string.
    named_a_cause = r.diagnosis is not None

    if sc.expect_cause is Cause.UNKNOWN:
        # "Correct" here means the spacecraft never talked itself into a named
        # cause. r.diagnosis only ever records a KNOWN diagnosis, so absence is
        # the success condition -- and the suite could not express this at all
        # until R10 got a scenario, which is why the gap survived so long.
        r.diagnosis_correct = r.diagnosis is None
        r.diagnosis = r.diagnosis or "UNKNOWN (held)"
    elif sc.expect_cause is not None:
        # Did it EVER reach the right conclusion, not merely say it first.
        r.diagnosis_correct = sc.expect_cause.name in r.causes_seen
        if r.diagnosis_correct and r.diagnosis != sc.expect_cause.name:
            r.notes = (r.notes or
                       f"reached {sc.expect_cause.name} after first reporting "
                       f"{r.diagnosis}")
            # A results table reading "GROUND_LINK_LOST | correct: yes" is
            # precisely the kind of evidence R9-1 was about -- the number is
            # right and the row says something else. Show both causes so the
            # column and the correctness verdict describe the same event.
            r.diagnosis = f"{r.diagnosis}->{sc.expect_cause.name}"

    if sc.inject is None:
        # A control run has no fault to recover from. Labelling it "recovered"
        # would inflate the recovery count with a run that never had anything
        # wrong -- exactly the kind of flattering arithmetic this suite exists
        # to avoid.
        r.outcome = Outcome.CLEAN if not r.detected else Outcome.UNDETECTED
        r.notes = "nominal control run"
    elif not r.detected:
        r.outcome = Outcome.UNDETECTED
    elif r.recovery_verified:
        r.outcome = Outcome.RECOVERED
    elif (h.engine.fault_flags & FaultFlag.UNKNOWN_ANOMALY
          and h.engine.diagnosis.cause == Cause.UNKNOWN
          and not named_a_cause):
        # THREE conditions, and each was added because the previous version
        # mislabelled a run.
        #
        # The flag alone is not enough: UNKNOWN_ANOMALY latches, so a run that
        # briefly could not explain itself was labelled by that moment even
        # after identifying the cause.
        #
        # The final diagnosis alone is not enough either. The rail-overcurrent
        # run names RAIL_OVERCURRENT, sheds the offending rail, and the
        # overcurrent genuinely goes away -- at which point diagnose() rightly
        # declines to assert anything and an advisory flag from the load-shed
        # transient is all that remains. Labelling THAT "unknown_held" reports
        # a successful containment as a failure to understand.
        #
        # So: unknown_held means the spacecraft never named a cause at any
        # point in the run. `r.diagnosis` records the first KNOWN diagnosis, so
        # its absence is the honest test.
        r.outcome = Outcome.UNKNOWN_HELD
    elif (h.engine.fault_flags & FaultFlag.RECOVERY_FAILED
          or h.engine.mode in (Mode.SAFE, Mode.DEGRADED)):
        r.outcome = Outcome.CONTAINED
    else:
        r.outcome = Outcome.DETECTED_ONLY

    r.notes = r.notes or sc.note
    return r


# ---------------------------------------------------------------------------
# The suite. Pairs are adjacent and share a comment explaining what the pair
# discriminates -- that relationship IS the test.
# ---------------------------------------------------------------------------

def build_suite() -> List[Scenario]:
    return [
        # -- control -------------------------------------------------------
        Scenario("nominal control", None, expect_flags=0,
                 forbid_flags=int(FaultFlag.UNDERVOLTAGE_CRITICAL | FaultFlag.THERMAL_ANOMALY
                                  | FaultFlag.SENSOR_LOCKUP | FaultFlag.DATA_PATH_SUSPECT),
                 forbid_actions=True, seed=101, duration_s=180.0,
                 note="no fault injected; nothing may latch and nothing may act"),

        # -- PAIR 1: the hardware measurement ------------------------------
        # Same presenting symptom (comms dead). Different cause, different
        # correct action. Separable only by per-rail current.
        Scenario("radio latch-up (per-rail sensing)", "radio_latchup",
                 expect_flags=int(FaultFlag.COMMS_LOSS | FaultFlag.RAIL_OVERCURRENT),
                 expect_cause=Cause.RADIO_LATCHUP,
                 seed=11, duration_s=180.0,
                 note="CSSWE mechanism; recoverable by power removal"),
        Scenario("radio unresponsive (per-rail sensing)", "radio_unresponsive",
                 expect_flags=int(FaultFlag.COMMS_LOSS), expect_cause=Cause.GROUND_LINK_LOST,
                 seed=12, duration_s=180.0,
                 note="control for the above: identical link symptom, nominal current"),

        # -- PAIR 1b: the SAME pair, blinded -------------------------------
        # Identical scenarios with per-rail sensing removed. The difference
        # between these two rows and the two above is the purchase argument.
        Scenario("radio latch-up (NO per-rail sensing)", "radio_latchup",
                 expect_flags=int(FaultFlag.COMMS_LOSS | FaultFlag.RAIL_OVERCURRENT),
                 expect_cause=Cause.RADIO_LATCHUP,
                 seed=11, duration_s=180.0, per_rail_sensing=False,
                 note="same fault, aggregate bus current only"),
        Scenario("radio unresponsive (NO per-rail sensing)", "radio_unresponsive",
                 expect_flags=int(FaultFlag.COMMS_LOSS), expect_cause=Cause.GROUND_LINK_LOST,
                 seed=12, duration_s=180.0, per_rail_sensing=False,
                 note="same control, aggregate bus current only"),

        # -- PAIR 2: Delfi-C3 ----------------------------------------------
        # Several channels invalid at once vs one channel invalid. The path
        # must be blamed in the first case and the device in the second.
        Scenario("data bus failure", "data_bus_failure",
                 expect_flags=int(FaultFlag.DATA_PATH_SUSPECT), expect_cause=Cause.DATA_PATH,
                 forbid_flags=int(FaultFlag.SENSOR_LOCKUP),
                 seed=21, duration_s=60.0,
                 note="Delfi-C3: the path failed, the devices are fine"),
        Scenario("single sensor corrupt", "sensor_corruption",
                 expect_flags=int(FaultFlag.SENSOR_IMPLAUSIBLE),
                 expect_cause=Cause.SENSOR_CORRUPT,
                 forbid_flags=int(FaultFlag.DATA_PATH_SUSPECT),
                 seed=22, duration_s=60.0,
                 note="one device is a DEVICE fault, and must not read as a path fault"),

        # -- KySat-2 -------------------------------------------------------
        Scenario("recovery that cannot succeed", "radio_latchup",
                 expect_flags=int(FaultFlag.COMMS_LOSS | FaultFlag.RAIL_OVERCURRENT),
                 expect_cause=Cause.RADIO_LATCHUP,
                 seed=31, latch_clears=False, duration_s=200.0,
                 note="KySat-2: action executes correctly and achieves nothing"),
        Scenario("OBC reset mid-recovery", "radio_latchup",
                 expect_flags=int(FaultFlag.COMMS_LOSS | FaultFlag.RAIL_OVERCURRENT),
                 expect_cause=Cause.RADIO_LATCHUP,
                 seed=32, latch_clears=False, duration_s=200.0, obc_reset_at_s=45.0,
                 note="KySat-2: the reset must not restart the ladder"),

        # -- power ---------------------------------------------------------
        # This scenario used to expect UNDETECTED, and that was the honest
        # finding at the time: no overcurrent detector existed, and the KySat-2
        # mechanism is precisely that a rail can eat the battery while every
        # fixed VOLTAGE threshold stays satisfied.
        #
        # FDIR-011 closes it. The assertion is now inverted, and the forbid on
        # UNDERVOLTAGE_CRITICAL is the load-bearing half: catching this as a
        # voltage problem would mean catching it far too late, which is the
        # whole failure. It must be caught on CURRENT, before the sag.
        Scenario("rail overcurrent", "rail_overcurrent",
                 expect_flags=int(FaultFlag.RAIL_OVERCURRENT),
                 expect_cause=Cause.RAIL_OVERCURRENT,
                 forbid_flags=int(FaultFlag.UNDERVOLTAGE_CRITICAL),
                 seed=41, duration_s=120.0,
                 note="KySat-2 drain: caught on CURRENT, before any voltage threshold moves"),
        Scenario("undervoltage", "undervoltage",
                 expect_flags=int(FaultFlag.UNDERVOLTAGE_CRITICAL),
                 expect_cause=Cause.POWER_UNDERVOLTAGE, seed=42, duration_s=60.0),
        Scenario("thermal excursion", "thermal",
                 expect_flags=int(FaultFlag.THERMAL_ANOMALY), expect_cause=Cause.THERMAL,
                 seed=43, duration_s=60.0),

        # -- sensors -------------------------------------------------------
        Scenario("sensor frozen", "sensor_lockup",
                 expect_flags=int(FaultFlag.SENSOR_LOCKUP), expect_cause=Cause.SENSOR_FROZEN,
                 seed=51, duration_s=60.0),
        Scenario("sensor not responding", "sensor_timeout",
                 expect_flags=int(FaultFlag.SENSOR_TIMEOUT),
                 expect_cause=Cause.SENSOR_NOT_RESPONDING, seed=52, duration_s=60.0),

        # -- the one that must NOT produce a confident answer ---------------
        # R7. This scenario spent every previous phase as the honest UNDETECTED
        # row: the EWMA baseline follows the signal, so a slow enough drift is
        # absorbed as the new normal (measured at 0% recall in the ML
        # evaluation, and the flight analogue is QuakeSat). No fixed VOLTAGE
        # threshold is breached either -- this drift ends at 4.30 V, under the
        # 4.5 V warning but never reaching the 4.0 V critical.
        #
        # The fixed commissioning reference is what closes it, and the forbid is
        # the meaningful half: catching this via UNDERVOLTAGE_CRITICAL would
        # mean not catching it at all, since the drift never gets there.
        Scenario("gradual drift", "gradual_drift",
                 expect_flags=int(FaultFlag.DRIFT_FROM_REFERENCE),
                 expect_cause=Cause.DEGRADATION,
                 forbid_flags=int(FaultFlag.UNDERVOLTAGE_CRITICAL),
                 seed=61, duration_s=120.0,
                 note="R7: caught against a FIXED reference the baseline cannot learn away"),

        # -- R10: the largest category in the failure record ------------------
        # 63% of NASA-catalogued CubeSat failures have no stated technical
        # cause. Until now every scenario here injected something the rule set
        # could name, so unknown_held was 0/14 and the R10 path was unit-tested
        # but never exercised end to end -- the most consequential gap in the
        # traceability doc.
        #
        # The assertion that matters is the FORBID: when the spacecraft cannot
        # explain what it sees, it must take no autonomous action at all. An
        # invented diagnosis authorising a wrong action is the Delfi-C3 failure.
        Scenario("unexplained transient", "unexplained_transient",
                 expect_flags=int(FaultFlag.ADAPTIVE_ANOMALY),
                 expect_cause=Cause.UNKNOWN,
                 forbid_flags=int(FaultFlag.UNDERVOLTAGE_CRITICAL
                                  | FaultFlag.DRIFT_FROM_REFERENCE
                                  | FaultFlag.RAIL_OVERCURRENT),
                 seed=71, duration_s=90.0,
                 note="R10: measurable, unexplainable, and correctly acted on by NOT acting"),

        # -- OUT OF GROUND CONTACT ------------------------------------------
        # The normal state of a CubeSat for most of every orbit, and a state
        # this suite never modelled until round 9. Rounds 7 and 8 both found
        # defects living in exactly that gap, so it is a dimension now.
        #
        # These pair with their in-contact twins above: same fault, same seed,
        # differing only in whether anyone is listening.
        Scenario("undervoltage (out of contact)", "undervoltage",
                 expect_flags=int(FaultFlag.UNDERVOLTAGE_CRITICAL),
                 expect_cause=Cause.POWER_UNDERVOLTAGE,
                 ground_contact=False, seed=42, duration_s=60.0,
                 note="an acute fault must not be masked by the silence around it"),
        Scenario("thermal excursion (out of contact)", "thermal",
                 expect_flags=int(FaultFlag.THERMAL_ANOMALY),
                 expect_cause=Cause.THERMAL,
                 ground_contact=False, seed=43, duration_s=60.0,
                 note="pairs with the in-contact run; only the listener differs"),

        # THE DESIGN GAP, recorded as a scenario rather than a footnote.
        #
        # A perfectly healthy vehicle, simply out of view, opens a comms
        # recovery campaign and power-cycles its radio. That is R5 working as
        # specified -- CSSWE says loss of contact is a fault condition with an
        # autonomous response -- but the system has NO WAY TO DISTINGUISH
        # expected silence between passes from anomalous silence. At the
        # test-scale COMMS_RECOVERY_TRIGGER_S (30 s) it fires almost at once;
        # at a flight-scale value (hours) it would not, which MASKS the gap
        # rather than closing it.
        #
        # Deliberately not forbidding the action: it is correct per the
        # requirement. What is missing is a notion of an expected contact gap,
        # and that is a V1 design question, not a bug to patch here.
        Scenario("healthy but out of view", None,
                 expect_flags=0, ground_contact=False, seed=71, duration_s=120.0,
                 note="R5 fires on expected silence; no concept of a pass schedule exists"),
    ]


def main() -> int:
    results = [run_scenario(sc) for sc in build_suite()]

    print(f"{'scenario':<42}{'det':>5}{'lat_s':>8}{'diagnosis':>38}{'ok':>4}{'outcome':>16}{'act':>5}")
    print("-" * 118)
    for r in results:
        det = "yes" if r.detected else "NO"
        lat = f"{r.detection_latency_s:.2f}" if r.detection_latency_s is not None else "-"
        diag = r.diagnosis or "-"
        ok = "-" if r.diagnosis_correct is None else ("yes" if r.diagnosis_correct else "NO")
        print(f"{r.name:<42}{det:>5}{lat:>8}{diag:>38}{ok:>4}{r.outcome:>16}{r.recovery_attempts:>5}")

    violations = [(r.name, v) for r in results for v in r.forbidden_violations]
    print()
    if violations:
        print("NEGATIVE-ASSERTION VIOLATIONS (these are failures):")
        for name, v in violations:
            print(f"  {name}: {v}")
    else:
        print("negative assertions: all clean (no forbidden flag latched, no forbidden action)")

    faults = [r for r in results if r.injected != "none (nominal control)"]
    print()
    print("OUTCOME DISTRIBUTION over OUR INJECTED FAULT SET "
          f"(n={len(faults)}) -- NOT a statement about CubeSat failure in general:")
    for outcome in (Outcome.RECOVERED, Outcome.CONTAINED, Outcome.DETECTED_ONLY,
                    Outcome.UNKNOWN_HELD, Outcome.UNDETECTED, Outcome.CLEAN):
        n = sum(1 for r in faults if r.outcome == outcome)
        print(f"  {outcome:<16}{n:>3}/{len(faults)}")

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
