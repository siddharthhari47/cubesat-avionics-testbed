"""
Regression wrapper around the fault-injection suite.

The suite's own value is the measured numbers in
docs/architecture/v0-scenario-results.md. What is asserted here is narrower and
more durable: the NEGATIVE properties must never regress, and the discrimination
pairs must keep discriminating.

Positive detection numbers are deliberately NOT pinned here -- they are reported
by the runner and will move as detectors are added. Pinning them would turn
every improvement into a test failure.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))

import pytest  # noqa: E402

from runner import Outcome, build_suite, run_scenario  # noqa: E402


@pytest.fixture(scope="module")
def results():
    return {sc.name: run_scenario(sc) for sc in build_suite()}


def test_no_negative_assertion_is_ever_violated(results):
    """
    The assertion that matters most. Four of five documented FDIR failures were
    wrong-ACTION failures, not missed detections, so this guards the property a
    positive-only suite structurally cannot.
    """
    violations = {name: r.forbidden_violations
                  for name, r in results.items() if r.forbidden_violations}
    assert not violations, f"forbidden flags or actions occurred: {violations}"


def test_nominal_control_stays_quiet(results):
    """FDIR-008 in miniature: a healthy spacecraft must not trip anything."""
    r = results["nominal control"]
    assert r.outcome == Outcome.CLEAN
    assert not r.detected
    assert r.recovery_attempts == 0


def test_per_rail_sensing_changes_the_diagnosis(results):
    """
    The hardware-purchase measurement. With per-rail current the latch-up is
    diagnosed correctly; without it, it is misdiagnosed as a quiet link. If this
    ever stops being true, the argument for the sensing hardware has changed and
    somebody should notice.
    """
    with_sensing = results["radio latch-up (per-rail sensing)"]
    without = results["radio latch-up (NO per-rail sensing)"]

    assert with_sensing.diagnosis_correct is True
    assert without.diagnosis_correct is False
    assert with_sensing.diagnosis != without.diagnosis


def test_delfi_c3_pair_reaches_different_diagnoses(results):
    """A bus fault blames the path; one bad device does not."""
    bus = results["data bus failure"]
    device = results["single sensor corrupt"]

    assert bus.diagnosis == "DATA_PATH"
    assert bus.recovery_attempts == 0, (
        "a path fault must not trigger per-device recovery -- that is the "
        "Delfi-C3 error"
    )
    assert device.diagnosis != "DATA_PATH"


def test_unrecoverable_faults_are_contained_not_looping(results):
    """KySat-2: bounded, escalating, and it stops."""
    for name in ("recovery that cannot succeed", "OBC reset mid-recovery"):
        r = results[name]
        assert r.outcome == Outcome.CONTAINED
        assert r.recovery_attempts <= 10, (
            f"{name}: {r.recovery_attempts} actions -- recovery must be bounded"
        )


def test_reset_midcampaign_does_not_restart_the_ladder(results):
    """
    A reset must not reset the attempt counter. If it did, the run with a reset
    would show the SAME number of actions as the one without (both restarting
    from rung 0 forever); instead it shows more, because it resumed and then
    exhausted.
    """
    plain = results["recovery that cannot succeed"]
    with_reset = results["OBC reset mid-recovery"]
    assert with_reset.recovery_attempts >= plain.recovery_attempts


def test_suite_is_deterministic():
    """Same seeds, same results -- otherwise none of the above means anything."""
    suite = build_suite()[:4]
    first = [run_scenario(sc) for sc in suite]
    second = [run_scenario(sc) for sc in suite]
    for a, b in zip(first, second):
        assert (a.detected, a.detection_latency_s, a.diagnosis, a.outcome) == \
               (b.detected, b.detection_latency_s, b.diagnosis, b.outcome)
