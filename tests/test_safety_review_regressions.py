"""
Regression tests for the V0 adversarial safety review (F1-F6).

One test per finding, each written to FAIL against the code as it was. These
are deliberately kept together rather than scattered into the topic files: the
review found that several of these defects were invisible precisely because the
existing tests asserted a fault SETS and never that it clears, so grouping the
"and then it recovers" half in one place makes the omission harder to repeat.

See docs/architecture/v0-adversarial-safety-review.md for how each was found.
"""

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fdir import config as cfg  # noqa: E402
from fdir.diagnosis import Cause, diagnose  # noqa: E402
from fdir.engine import (  # noqa: E402
    CONDITION_BACKED_FLAGS, EVENT_FLAGS, RECOVERY_AUTHORITY_FLAGS,
    RESETTABLE_FLAGS, SAFE_MODE_TRIGGER_FLAGS, EwmaStat, FDIREngine,
)
from fdir.ports import RecoveryAction  # noqa: E402
from fdir.recovery import (  # noqa: E402
    SYSTEM_TARGET, Campaign, CampaignState, Rung, VerifyCondition,
    comms_loss_ladder,
)
from icd import Device, FaultFlag, HealthFlag, Mode, Rail, RawSample  # noqa: E402

NAN = float("nan")
INF = float("inf")


def sample(**kw):
    d = dict(temp_c=25.0, accel_x=0.1, accel_y=0.2, accel_z=9.8,
             gyro_x=0.01, gyro_y=0.02, gyro_z=0.03,
             mag_x=20.0, mag_y=5.0, mag_z=-40.0,
             bus_voltage_v=5.0, bus_current_a=0.40)
    d.update(kw)
    return RawSample(**d)


def moving(i, **kw):
    """A sample that never exactly repeats, so the lockup detector stays quiet."""
    kw.setdefault("accel_x", 0.1 + i * 1e-6)
    kw.setdefault("gyro_x", 0.01 + i * 1e-7)
    return sample(**kw)


def run(engine, n, t0=0.0, dt=0.1, **kw):
    t = t0
    for i in range(n):
        engine.tick(moving(i, **kw), t)
        t += dt
    return t


def nominal_engine():
    e = FDIREngine()
    t = run(e, 30)
    assert e.mode == Mode.NOMINAL
    return e, t


# ---------------------------------------------------------------------------
# F1 / F5 -- flags that could never be cleared
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [
    FaultFlag.DATA_PATH_SUSPECT,
    FaultFlag.UNKNOWN_ANOMALY,
    FaultFlag.RECOVERY_FAILED,
    FaultFlag.SENSOR_INVALID,
])
def test_every_latching_flag_has_a_path_back_to_clear(flag):
    """
    F1/F5. These four were in NEITHER CONDITION_BACKED_FLAGS nor EVENT_FLAGS,
    so reset_faults() never even considered them and nothing else cleared them.
    """
    assert flag & RESETTABLE_FLAGS, f"{flag.name} can never be cleared by anything"


def test_no_flag_is_silently_unclearable():
    """
    The general form, so a NEW flag cannot repeat this. Every flag must either
    be resettable or be a live indicator that maintains itself each tick.
    """
    self_clearing = FaultFlag.COMMS_LOSS | FaultFlag.UNDERVOLTAGE_WARNING
    for f in FaultFlag:
        if f == FaultFlag.NONE:
            continue
        assert f & (RESETTABLE_FLAGS | self_clearing), (
            f"{f.name} is in neither RESETTABLE_FLAGS nor the self-clearing set, "
            f"so once latched it stays set for the life of the vehicle"
        )


def test_data_path_suspect_clears_once_the_bus_recovers():
    """
    F1, the severe one. A transient bus fault used to latch DATA_PATH_SUSPECT
    permanently, and diagnose() checks that flag FIRST by design.
    """
    e, t = nominal_engine()
    zeroed = dict(accel_x=0.0, accel_y=0.0, accel_z=0.0,
                  gyro_x=0.0, gyro_y=0.0, gyro_z=0.0,
                  mag_x=0.0, mag_y=0.0, mag_z=0.0, temp_c=0.0)
    for _ in range(10):
        e.tick(sample(**zeroed), t)
        t += 0.1
    assert e.fault_flags & FaultFlag.DATA_PATH_SUSPECT

    t = run(e, 20, t0=t)                       # bus recovers
    cleared, still = e.reset_faults(t)

    assert cleared & FaultFlag.DATA_PATH_SUSPECT
    assert not (e.fault_flags & FaultFlag.DATA_PATH_SUSPECT)


def test_a_stuck_data_path_flag_does_not_mask_a_real_fault():
    """
    The consequence that made F1 severe rather than cosmetic: because
    DATA_PATH is returned at Confidence.LIKELY with authorises_action True, a
    permanently stuck flag meant a permanently confident WRONG diagnosis -- the
    Delfi-C3 failure mode diagnosis.py exists to prevent.
    """
    e, t = nominal_engine()
    zeroed = dict(accel_x=0.0, accel_y=0.0, accel_z=0.0,
                  gyro_x=0.0, gyro_y=0.0, gyro_z=0.0,
                  mag_x=0.0, mag_y=0.0, mag_z=0.0, temp_c=0.0)
    for _ in range(10):
        e.tick(sample(**zeroed), t)
        t += 0.1
    t = run(e, 20, t0=t)
    e.reset_faults(t)

    assert diagnose(e.fault_flags, sample()).cause == Cause.UNKNOWN

    e.fault_flags |= FaultFlag.UNDERVOLTAGE_CRITICAL
    assert diagnose(e.fault_flags, sample()).cause == Cause.POWER_UNDERVOLTAGE, (
        "a stale path-fault flag must not outrank a live undervoltage"
    )


def test_recovery_failed_can_be_acknowledged_after_a_later_success():
    """F5: RECOVERY_FAILED used to survive a subsequent successful campaign."""
    e, t = nominal_engine()
    e.fault_flags |= FaultFlag.RECOVERY_FAILED
    cleared, still = e.reset_faults(t)
    assert cleared & FaultFlag.RECOVERY_FAILED
    assert not (e.fault_flags & FaultFlag.RECOVERY_FAILED)


# ---------------------------------------------------------------------------
# F2 -- Rail/Device type confusion
# ---------------------------------------------------------------------------

def test_the_comms_ladder_resets_the_radio_and_not_the_magnetometer():
    """
    F2. The ladder took ONE integer and used it for both the RESET_DEVICE
    target (a Device) and the POWER_CYCLE target (a Rail). Called with
    Rail.RADIO = 1, rung 0 "soft reset the radio" issued a reset of
    Device(1) = MAG.
    """
    rungs = comms_loss_ladder()
    assert rungs[0].action == RecoveryAction.RESET_DEVICE
    assert rungs[0].target == Device.RADIO, (
        f"rung 0 targets {Device(rungs[0].target).name}, not the radio"
    )
    assert rungs[1].action == RecoveryAction.POWER_CYCLE
    assert rungs[1].target == Rail.RADIO
    assert rungs[2].target == SYSTEM_TARGET


def test_a_rail_cannot_be_passed_where_a_device_is_required():
    """
    And it must be UNREPRESENTABLE, not merely corrected. A range check cannot
    catch this: Rail ids are 0-4, Device ids are 0-3, so Rail.RADIO (1) is also
    a perfectly valid Device id (MAG). Only the type distinguishes them.
    """
    with pytest.raises(ValueError):
        Rung(RecoveryAction.RESET_DEVICE, Rail.RADIO, 1, VerifyCondition.RADIO_RESPONSIVE)
    with pytest.raises(ValueError):
        Rung(RecoveryAction.POWER_CYCLE, Device.RADIO, 1, VerifyCondition.RADIO_RESPONSIVE)
    with pytest.raises(ValueError):
        # A bare int carries no vocabulary at all and must also be refused.
        Rung(RecoveryAction.RESET_DEVICE, 1, 1, VerifyCondition.RADIO_RESPONSIVE)


def test_persisted_rung_targets_keep_their_vocabulary():
    """
    A bare id cannot be resolved on restore, because the ranges overlap. The
    persisted record has to carry which enum it came from.
    """
    original = Campaign(trigger=int(FaultFlag.COMMS_LOSS), rungs=comms_loss_ladder())
    restored = Campaign.from_dict(original.to_dict())
    assert restored.rungs[0].target == Device.RADIO
    assert isinstance(restored.rungs[0].target, Device)
    assert isinstance(restored.rungs[1].target, Rail)


def test_legacy_v1_state_is_refused_rather_than_guessed():
    """v1 records have untyped targets AND were written by the buggy code."""
    d = Campaign(trigger=int(FaultFlag.COMMS_LOSS), rungs=comms_loss_ladder()).to_dict()
    d["schema_version"] = 1
    engine = FDIREngine()
    engine.import_recovery_state(d, now=0.0)
    assert engine.campaign is None


# ---------------------------------------------------------------------------
# F3 -- NaN accepted as evidence a fault cleared
# ---------------------------------------------------------------------------

def test_nan_voltage_is_not_evidence_that_an_undervoltage_cleared():
    """
    F3, the most dangerous finding. Every comparison with NaN is False, so
    `NaN < UNDERVOLTAGE_CRITICAL_V` took the else branch and recorded the
    reading as POSITIVE EVIDENCE the fault had gone. Measured end to end: a
    vehicle correctly held in SAFE was returned to service on readings that
    carried no information at all.
    """
    e, t = nominal_engine()
    t = run(e, 60, t0=t, bus_voltage_v=3.5)
    assert e.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL
    assert e.mode == Mode.SAFE

    t = run(e, 60, t0=t, bus_voltage_v=NAN)

    assert e._clean_ticks.get(int(FaultFlag.UNDERVOLTAGE_CRITICAL), 0) == 0, (
        "a meaningless reading must not accumulate evidence of clearance"
    )
    cleared, still = e.reset_faults(t)
    assert not (cleared & FaultFlag.UNDERVOLTAGE_CRITICAL)
    assert still & FaultFlag.UNDERVOLTAGE_CRITICAL
    assert e.exit_safe_mode(t) is False
    assert e.mode == Mode.SAFE


def test_an_invalid_channel_is_reported_rather_than_silently_trusted():
    e, t = nominal_engine()
    t = run(e, 5, t0=t, bus_voltage_v=NAN)
    assert e.fault_flags & FaultFlag.SENSOR_INVALID
    assert not (e.health_flags & HealthFlag.POWER_OK)


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_infinities_are_treated_the_same_as_nan(bad):
    """+/-inf defeats threshold comparisons just as thoroughly, in one direction."""
    e, t = nominal_engine()
    t = run(e, 5, t0=t, bus_voltage_v=bad)
    assert e.fault_flags & FaultFlag.SENSOR_INVALID


def test_nan_temperature_is_reported_as_invalid_not_as_a_thermal_anomaly():
    """
    The temperature detector failed CLOSED on NaN while the voltage detector
    failed OPEN -- same language rule, opposite outcomes, decided only by how
    each predicate happened to be written. Latching THERMAL_ANOMALY here would
    also be a wrong diagnosis: the sensor is broken, the spacecraft is not hot,
    and THERMAL_ANOMALY carries SAFE authority.
    """
    e, t = nominal_engine()
    t = run(e, 60, t0=t, temp_c=NAN)
    assert e.fault_flags & FaultFlag.SENSOR_INVALID
    assert not (e.fault_flags & FaultFlag.THERMAL_ANOMALY)
    assert not (e.health_flags & HealthFlag.TEMP_OK)


def test_a_real_thermal_excursion_still_latches():
    """The F3 fix must not have blunted the detector it touched."""
    e, t = nominal_engine()
    t = run(e, 60, t0=t, temp_c=cfg.THERMAL_CRITICAL_HIGH_C + 10.0)
    assert e.fault_flags & FaultFlag.THERMAL_ANOMALY
    assert e.mode == Mode.SAFE


def test_sensor_invalid_carries_no_authority():
    """An untrustworthy sensor is not, by itself, grounds to safe or to act."""
    assert not (FaultFlag.SENSOR_INVALID & SAFE_MODE_TRIGGER_FLAGS)
    assert not (FaultFlag.SENSOR_INVALID & RECOVERY_AUTHORITY_FLAGS)


def test_nan_imu_does_not_latch_a_lockup():
    """
    Repeated NaN inside a tuple compares equal by object identity, so "is this
    window all identical?" would have been answered by accident.
    """
    e, t = nominal_engine()
    for _ in range(cfg.LOCKUP_WINDOW_SAMPLES + 5):
        e.tick(sample(accel_x=NAN, accel_y=NAN, accel_z=NAN,
                      gyro_x=NAN, gyro_y=NAN, gyro_z=NAN), t)
        t += 0.1
    assert e.fault_flags & FaultFlag.SENSOR_INVALID
    assert not (e.fault_flags & FaultFlag.SENSOR_LOCKUP)


# ---------------------------------------------------------------------------
# F4 -- one NaN permanently disabled the adaptive baseline
# ---------------------------------------------------------------------------

def test_one_nan_does_not_permanently_poison_the_baseline():
    """
    F4. NaN propagated into mean and var irreversibly; deviation_sigma() then
    returned NaN forever, and `NaN > ADAPTIVE_K` is False, so the detector
    silently never fired again while looking exactly like a healthy one.
    """
    st = EwmaStat(cfg.EWMA_ALPHA)
    for _ in range(50):
        st.update(5.0)
    st.update(NAN)
    for _ in range(10):
        st.update(5.0)

    assert math.isfinite(st.mean)
    assert math.isfinite(st.var)
    assert st.rejected == 1, "rejections must be counted, not silently swallowed"


def test_a_dead_baseline_is_distinguishable_from_a_quiet_one():
    st = EwmaStat(cfg.EWMA_ALPHA)
    for _ in range(20):
        st.update(NAN)
    assert st.rejected == 20
    assert st.mean is None
    assert st.deviation_sigma(5.0) == 0.0


# ---------------------------------------------------------------------------
# F6 -- persisted state validated for type but not for value
# ---------------------------------------------------------------------------

def _good_state():
    return Campaign(trigger=int(FaultFlag.COMMS_LOSS), rungs=comms_loss_ladder(),
                    rung_index=0, attempts_on_rung=1, total_attempts=1,
                    state=CampaignState.ACTING, started_at=5.0).to_dict()


@pytest.mark.parametrize("mutation", [
    {"rung_index": -5},
    {"rung_index": 99},
    {"attempts_on_rung": -100, "total_attempts": -100},
    {"attempts_on_rung": 9, "total_attempts": 1},
    {"rungs": []},
    {"state": "ACTING"},
    {"schema_version": 999},
])
def test_corrupt_persisted_state_is_discarded(mutation):
    """
    F6. Every one of these used to be ACCEPTED. They all happened to fail safe
    downstream, but safety resting on luck is worth converting into safety
    resting on a check.
    """
    engine = FDIREngine()
    engine.import_recovery_state({**_good_state(), **mutation}, now=0.0)
    assert engine.campaign is None
    assert any("discarded" in msg for _, msg in engine.log)


def test_valid_persisted_state_still_resumes():
    """The validation must not have broken the feature it guards."""
    engine = FDIREngine()
    engine.import_recovery_state(_good_state(), now=0.0)
    assert engine.campaign is not None
    assert engine.campaign.rung_index == 1, "resumes at the NEXT rung (R3)"
    assert engine.campaign.total_attempts == 1, "prior attempts remembered"


def test_corrupt_state_cannot_crash_the_boot():
    """
    An uncaught exception here turns a recoverable data fault into a boot loop,
    which is a far worse failure than discarding one NVM record.
    """
    for junk in ({}, {"schema_version": 2}, {"schema_version": 2, "rungs": "no"},
                 {"schema_version": 2, "rungs": [], "trigger": None}):
        engine = FDIREngine()
        engine.import_recovery_state(junk, now=0.0)
        assert engine.campaign is None


# ---------------------------------------------------------------------------
# Round 2 -- the dimensions the first pass skipped
# ---------------------------------------------------------------------------

# J1 -- a link that is open but silent

def test_an_open_but_silent_link_is_comms_loss():
    """
    J1, the round-2 headline. `connected` used to short-circuit the decision,
    and the transport supplied it as `self.conn is not None` -- a socket OBJECT
    existing. On a half-open link recv blocks forever, so the spacecraft went
    on believing it had ground contact indefinitely, and COMMS_LOSS -- the only
    flag that can open the comms recovery ladder -- could not latch during the
    exact failure that ladder exists for.
    """
    e, t = nominal_engine()
    for _ in range(50):
        e.tick(sample(), t)
        e.note_link_state(t, link_established=True, seconds_since_contact=10_000.0)
        t += 0.1
    assert e.fault_flags & FaultFlag.COMMS_LOSS, (
        "a socket that exists is not evidence anyone is on the other end"
    )


def test_a_brief_transport_dropout_is_not_comms_loss():
    """
    The other direction, which is why the heartbeat and not `link_established`
    is the deciding evidence. A TCP reconnect or radio handover drops the
    transport while contact is fine; latching on that would trade J1 for a
    false positive.
    """
    e, t = nominal_engine()
    e.note_link_state(t, link_established=False,
                      seconds_since_contact=cfg.COMMS_LOSS_TIMEOUT_S - 0.1)
    assert not (e.fault_flags & FaultFlag.COMMS_LOSS)


def test_contact_evidence_decides_in_both_directions():
    e, t = nominal_engine()
    e.note_link_state(t, link_established=False, seconds_since_contact=None)
    assert e.fault_flags & FaultFlag.COMMS_LOSS, "never heard from = no contact"
    e.note_link_state(t, link_established=False, seconds_since_contact=0.0)
    assert not (e.fault_flags & FaultFlag.COMMS_LOSS), (
        "just heard from the ground -- the socket's state does not override that"
    )


# K1 -- the harness must feed the engine the same KIND of evidence

def test_environment_reports_contact_age_not_a_verdict():
    """
    K1. The environment used to report seconds_since_ground_contact as None
    whenever the link was healthy -- a pre-decided verdict, not the heartbeat
    a real transport produces. That is why the scenario suite could not express
    "link open but silent" at all, and why J1 survived Phase 6.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "simulator"))
    from environment import SpacecraftEnvironment

    env = SpacecraftEnvironment(seed=5)
    s1, _ = env.step(0.1)
    assert s1.seconds_since_ground_contact is not None, (
        "a healthy link must still report HOW LONG since contact"
    )
    assert s1.seconds_since_ground_contact == pytest.approx(0.0, abs=1e-9)

    env.link_healthy = False
    for _ in range(100):
        s2, _ = env.step(0.1)
    assert s2.seconds_since_ground_contact > cfg.COMMS_LOSS_TIMEOUT_S


def test_the_comms_timeout_is_actually_exercised():
    """
    Found while fixing K1: because last_ground_contact_t was never advanced,
    seconds_since_ground_contact was really "seconds since boot" and was
    already past the timeout before any fault was injected. COMMS_LOSS latched
    instantly on every link drop and COMMS_LOSS_TIMEOUT_S was never exercised
    by any test. This pins that the debounce genuinely runs.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "simulator"))
    from environment import SpacecraftEnvironment

    env = SpacecraftEnvironment(seed=6)
    e = FDIREngine()
    for _ in range(40):                        # boot with a healthy link
        s, _t = env.step(0.1)
        e.tick(s, env.t)
        e.note_link_state(env.t, link_established=env.link_healthy,
                          seconds_since_contact=s.seconds_since_ground_contact)
    assert not (e.fault_flags & FaultFlag.COMMS_LOSS)

    env.link_healthy = False
    latched_at = None
    drop_t = env.t
    for _ in range(200):
        s, _t = env.step(0.1)
        e.tick(s, env.t)
        e.note_link_state(env.t, link_established=env.link_healthy,
                          seconds_since_contact=s.seconds_since_ground_contact)
        if e.fault_flags & FaultFlag.COMMS_LOSS and latched_at is None:
            latched_at = env.t
    assert latched_at is not None
    assert latched_at - drop_t == pytest.approx(cfg.COMMS_LOSS_TIMEOUT_S, abs=0.2), (
        f"latched {latched_at - drop_t:.2f}s after the drop; the configured "
        f"debounce is {cfg.COMMS_LOSS_TIMEOUT_S}s"
    )


# G1/G2 -- enum conversion on unvalidated wire data

def test_an_unknown_ack_status_is_rendered_not_raised():
    """G1: this raised ValueError and killed the reader thread outright."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "ground-station"))
    from link import _status_name
    from simulator.protocol import AckStatus

    assert _status_name(int(AckStatus.ACCEPTED)) == "ACCEPTED"
    assert _status_name(0x08) == "UNKNOWN_STATUS(0x08)"
    assert _status_name(0xFF) == "UNKNOWN_STATUS(0xFF)"


def test_an_out_of_range_mode_does_not_crash_the_timeline():
    """G2: Mode(99) raised ValueError and took the whole timeline with it."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "ground-station"))
    from timeline import build_timeline
    from simulator.protocol import TelemetryPacket

    def pkt(mode):
        return TelemetryPacket(
            seq_num=0, timestamp_ms=0, mode=mode, fault_flags=0, health_flags=15,
            temp_c=25.0, accel_x=0.0, accel_y=0.0, accel_z=9.8, gyro_x=0.0,
            gyro_y=0.0, gyro_z=0.0, mag_x=0.0, mag_y=0.0, mag_z=0.0,
            bus_voltage_v=5.0, bus_current_a=0.4, uptime_s=0, cmd_rx_count=0,
            cmd_accept_count=0, cmd_reject_count=0, corrupted_rx_count=0)

    events = build_timeline([pkt(99)])
    assert events and "99" in events[0].label
