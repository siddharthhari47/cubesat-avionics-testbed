"""
Verification tests for fdir/engine.py's FDIREngine -- the deterministic FDIR
state machine that has final authority over BOOT/NOMINAL/SAFE/TEST mode
transitions.

Everything here talks to FDIREngine directly through its real input types
(fdir.engine.RawSample, fdir.engine.MLAdvisory) -- never through
simulator/environment.py's ground truth, which fdir/engine.py itself is
forbidden from seeing (see that module's docstring). Constructing RawSample
objects by hand here is deliberate, not a shortcut: it's the same information
real firmware would have (a bus voltage reading, whether the IMU ACKed), with
no simulation-only fault-injection flag anywhere in reach of the code under
test.

Architectural invariant under test throughout, and the single most important
assertion in this file (see test_ml_advisory_never_autonomously_forces_safe):
FaultFlag.ML_ANOMALY can latch like any other detected condition, but it is
deliberately excluded from SAFE_MODE_TRIGGER_FLAGS. A learned/statistical
detector may advise; only a human operator or a physically-grounded,
fixed-threshold detector may command SAFE.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "simulator"))

from fdir.engine import FDIREngine, MLAdvisory, RawSample, SAFE_MODE_TRIGGER_FLAGS  # noqa: E402
from fdir import config as cfg  # noqa: E402
from protocol import FaultFlag, HealthFlag, Mode  # noqa: E402
from run_simulator import Simulator  # noqa: E402

# Generous but bounded loop counts for "keep ticking until X latches" tests --
# large enough that a real latch is never missed, small enough that a broken
# detector fails fast instead of hanging the suite.
_SEARCH_BOUND = 50


def make_sample(
    *,
    temp_c: float = 25.0,
    accel: tuple = (0.0, 0.0, 1.0),
    gyro: tuple = (0.0, 0.0, 0.0),
    mag: tuple = (25.0, -8.0, 40.0),
    bus_voltage_v: float = 5.0,
    bus_current_a: float = 0.4,
    imu_responded: bool = True,
    temp_responded: bool = True,
    variant: int = 0,
) -> RawSample:
    """
    A physically-plausible nominal RawSample (value ranges match
    simulator/environment.py's NOMINAL_* constants), with every field
    overridable so each test builds exactly the fault condition it needs
    directly against FDIREngine's real input type.

    `variant` nudges accel/gyro by a tiny, strictly-increasing amount so a run
    of "nominal" ticks doesn't look like a frozen sensor to the lockup
    detector, which fires on exact repeats across consecutive samples --
    ordinary sensor noise makes a real exact repeat vanishingly unlikely, so
    varying it here is what makes these samples "nominal" rather than
    accidentally "locked up". Leave it at the default (or pass the same value
    twice) to deliberately construct a lockup condition instead.
    """
    eps = variant * 1e-5
    return RawSample(
        temp_c=temp_c,
        accel_x=accel[0] + eps, accel_y=accel[1], accel_z=accel[2],
        gyro_x=gyro[0], gyro_y=gyro[1], gyro_z=gyro[2],
        mag_x=mag[0], mag_y=mag[1], mag_z=mag[2],
        bus_voltage_v=bus_voltage_v,
        bus_current_a=bus_current_a,
        imu_responded=imu_responded,
        temp_responded=temp_responded,
    )


def drive_to_nominal(engine: FDIREngine, dt: float = 0.05) -> float:
    """
    Tick clean, non-faulty (but jittered -- see make_sample) samples from a
    fresh BOOT until the engine leaves BOOT on its own. Returns the `now` of
    the tick that completed the transition, so callers can keep advancing the
    clock from there. Asserts the landing mode is NOMINAL, since every test
    that uses this helper needs a clean boot as its starting point, not the
    BOOT->SAFE path (that path gets its own dedicated test).
    """
    now = 0.0
    i = 0
    while engine.mode == Mode.BOOT:
        engine.tick(make_sample(variant=i), now)
        i += 1
        now += dt
    assert engine.mode == Mode.NOMINAL, f"expected a clean boot to reach NOMINAL, got {engine.mode!r}"
    return now


# ---------------------------------------------------------------------------
# BOOT lifecycle
# ---------------------------------------------------------------------------

def test_boot_to_nominal_no_faults():
    engine = FDIREngine()
    assert engine.mode == Mode.BOOT

    now = drive_to_nominal(engine)

    assert engine.mode == Mode.NOMINAL
    assert engine.fault_flags == FaultFlag.NONE
    assert engine.health_flags == HealthFlag.ALL_OK
    assert now >= cfg.BOOT_DURATION_S
    assert any("BOOT -> NOMINAL" in msg for _, msg in engine.log)


def test_boot_to_safe_if_fault_already_present_at_boot_end():
    """
    A fault already breaching by the time the boot self-check window closes
    must land directly in SAFE, not NOMINAL -- SAFE is the conservative
    default when the boot-time self-check finds something already wrong.
    """
    engine = FDIREngine()
    now = 0.0
    dt = 0.05
    low_voltage = make_sample(bus_voltage_v=3.5)  # well under UNDERVOLTAGE_CRITICAL_V

    while engine.mode == Mode.BOOT:
        engine.tick(low_voltage, now)
        now += dt

    assert engine.mode == Mode.SAFE
    assert engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL
    assert any("BOOT -> SAFE" in msg for _, msg in engine.log)


# ---------------------------------------------------------------------------
# Fixed-threshold detectors that DO have autonomous SAFE authority
# ---------------------------------------------------------------------------

def test_undervoltage_forces_safe_after_debounce_not_before():
    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = cfg.UNDERVOLTAGE_DEBOUNCE_S / 3

    for i in range(_SEARCH_BOUND):
        if engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
            break
        assert engine.mode == Mode.NOMINAL  # must not trip before debounce is satisfied
        now += dt
        # variant=i: keep the IMU reading changing tick to tick so this test
        # of the undervoltage detector can't accidentally also trip the
        # lockup detector (5 identical consecutive readings) if the search
        # ever needs more than a couple of iterations -- see make_sample.
        engine.tick(make_sample(bus_voltage_v=3.5, variant=i), now)
    else:
        pytest.fail("UNDERVOLTAGE_CRITICAL never latched")

    assert engine.mode == Mode.SAFE


def test_thermal_anomaly_forces_safe():
    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = cfg.THERMAL_DEBOUNCE_S / 3

    for i in range(_SEARCH_BOUND):
        if engine.fault_flags & FaultFlag.THERMAL_ANOMALY:
            break
        assert engine.mode == Mode.NOMINAL
        now += dt
        # variant=i for the same reason as the undervoltage test above: avoid
        # an incidental lockup trip from repeating the exact same IMU reading.
        engine.tick(make_sample(temp_c=cfg.THERMAL_CRITICAL_HIGH_C + 10.0, variant=i), now)
    else:
        pytest.fail("THERMAL_ANOMALY never latched")

    assert engine.mode == Mode.SAFE


def test_sensor_lockup_forces_safe():
    """
    A frozen IMU still ACKs (imu_responded=True) but stops changing --
    LOCKUP_WINDOW_SAMPLES consecutive identical readings must latch
    SENSOR_LOCKUP and force SAFE; fewer than that must not.
    """
    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = 0.05
    frozen = make_sample()  # same values every call, on purpose

    for i in range(cfg.LOCKUP_WINDOW_SAMPLES):
        now += dt
        engine.tick(frozen, now)
        if i < cfg.LOCKUP_WINDOW_SAMPLES - 1:
            assert not (engine.fault_flags & FaultFlag.SENSOR_LOCKUP)
            assert engine.mode == Mode.NOMINAL

    assert engine.fault_flags & FaultFlag.SENSOR_LOCKUP
    assert engine.mode == Mode.SAFE


# ---------------------------------------------------------------------------
# Detectors that flag but are deliberately denied autonomous SAFE authority
# ---------------------------------------------------------------------------

def test_sensor_timeout_flags_and_clears_health_but_never_forces_safe():
    """
    SENSOR_TIMEOUT is deliberately excluded from SAFE_MODE_TRIGGER_FLAGS (see
    fdir/engine.py): a non-responding IMU degrades attitude knowledge but
    doesn't, by itself, autonomously force SAFE. Confirm the design as
    currently encoded, not just assert what we'd want it to say.
    """
    assert not (FaultFlag.SENSOR_TIMEOUT & SAFE_MODE_TRIGGER_FLAGS)

    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = cfg.SENSOR_TIMEOUT_DEBOUNCE_S / 3
    dead = make_sample(imu_responded=False)

    # First non-responding sample: HealthFlag.IMU_OK drops immediately (not
    # debounced); FaultFlag.SENSOR_TIMEOUT does not (it is debounced).
    now += dt
    engine.tick(dead, now)
    assert not (engine.health_flags & HealthFlag.IMU_OK)
    assert not (engine.fault_flags & FaultFlag.SENSOR_TIMEOUT)

    for _ in range(_SEARCH_BOUND):
        if engine.fault_flags & FaultFlag.SENSOR_TIMEOUT:
            break
        now += dt
        engine.tick(dead, now)
    else:
        pytest.fail("SENSOR_TIMEOUT never latched")

    assert engine.mode == Mode.NOMINAL  # the key assertion: flagged, but still not SAFE

    # Keep hammering it well beyond the debounce window -- persistence alone
    # must not eventually force SAFE either.
    for _ in range(200):
        now += dt
        engine.tick(dead, now)
    assert engine.mode == Mode.NOMINAL


def test_ml_advisory_never_autonomously_forces_safe():
    """
    THE central architectural invariant of this module. ML_ANOMALY latches
    through the same debounce every deterministic detector uses, but it is
    the one flag deliberately left out of SAFE_MODE_TRIGGER_FLAGS: a
    learned/statistical detector may advise that something looks off; it can
    never, by itself, command the deterministic state machine into SAFE. Only
    a human operator (ENTER_SAFE_MODE) or a physically-grounded, fixed-
    threshold detector (undervoltage/thermal/lockup) has that authority.

    This feeds a long run of consecutive anomalous advisories -- far past the
    debounce window -- specifically to prove persistence alone never earns
    the ML advisory the autonomous authority it is architecturally denied.
    """
    assert not (FaultFlag.ML_ANOMALY & SAFE_MODE_TRIGGER_FLAGS)  # the one line that matters

    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = 0.05
    anomalous = MLAdvisory(score=99.0, is_anomalous=True)

    latched_at = None
    for i in range(100):
        now += dt
        engine.tick(make_sample(variant=i), now, ml_advisory=anomalous)
        if latched_at is None and (engine.fault_flags & FaultFlag.ML_ANOMALY):
            latched_at = i
        # No matter how many consecutive anomalous advisories have arrived so
        # far, mode must never have moved off NOMINAL on its own.
        assert engine.mode == Mode.NOMINAL, (
            f"ML advisory alone forced a mode change at iteration {i} -- "
            "this must never happen, see SAFE_MODE_TRIGGER_FLAGS"
        )

    assert latched_at is not None, "ML_ANOMALY should have latched after its debounce"
    assert latched_at + 1 == cfg.ML_ANOMALY_DEBOUNCE_SAMPLES  # sanity: debounce actually engaged
    assert engine.mode == Mode.NOMINAL  # final state, restated for emphasis


# ---------------------------------------------------------------------------
# EXIT_SAFE_MODE / RESET_FAULTS recovery gating (FDIR-005)
# ---------------------------------------------------------------------------

def test_exit_safe_mode_requires_condition_clear_and_reset_faults():
    """
    EXIT_SAFE_MODE must be rejected while the triggering fault flag is still
    latched -- even after the underlying condition has itself stopped
    breaching, since the flag only clears via RESET_FAULTS, and RESET_FAULTS
    only clears it if the condition is no longer breaching at that moment.
    This two-step recovery (operator must explicitly acknowledge, not just
    have the number quietly look fine again) is deliberate.
    """
    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = cfg.UNDERVOLTAGE_DEBOUNCE_S / 3
    variant = 0  # keeps incrementing across the whole test so no two ticks
                 # here ever share an IMU fingerprint -- this test is about
                 # the undervoltage detector and must not accidentally also
                 # trip the (unrelated) lockup detector on repeated samples.

    for _ in range(_SEARCH_BOUND):
        if engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL:
            break
        now += dt
        variant += 1
        engine.tick(make_sample(bus_voltage_v=3.5, variant=variant), now)
    assert engine.mode == Mode.SAFE

    # Still breaching: rejected.
    assert engine.exit_safe_mode(now) is False
    assert engine.mode == Mode.SAFE

    # Condition clears (nominal voltage again), but the latched flag does not
    # auto-clear -- EXIT_SAFE_MODE must still be rejected.
    now += dt
    variant += 1
    engine.tick(make_sample(bus_voltage_v=5.0, variant=variant), now)
    assert engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL, "flag must not self-clear"
    assert engine.exit_safe_mode(now) is False
    assert engine.mode == Mode.SAFE

    # Operator explicitly acknowledges via RESET_FAULTS -- only now, because
    # the condition is genuinely no longer breaching, does the flag clear.
    engine.reset_faults(now)
    assert not (engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL)

    # Now, and only now, EXIT_SAFE_MODE is accepted.
    assert engine.exit_safe_mode(now) is True
    assert engine.mode == Mode.NOMINAL


# ---------------------------------------------------------------------------
# Cold-start false-positive regressions -- each of these was a real bug found
# and fixed once already. They stay here specifically so removing the guard
# that fixes them fails CI instead of quietly shipping a false SAFE entry (or
# a false comms-loss flag) on every power-on.
# ---------------------------------------------------------------------------

def test_adaptive_baseline_warmup_guard_regression():
    """
    Regression for FDIR-006's cold-start bug: a variance estimate built from
    only a handful of samples is nearly zero, which makes ordinary noise (or,
    here, a deliberately large but still nowhere-near-critical jump) look
    like an enormous number of standard deviations away. MIN_ADAPTIVE_SAMPLES
    exists to withhold judgment until the baseline has enough samples to mean
    something. This test spends its entire run inside that warm-up window
    (never reaching MIN_ADAPTIVE_SAMPLES post-boot ticks) and includes a
    voltage jump big enough that, without the warm-up guard, would trip
    ADAPTIVE_DEBOUNCE_SAMPLES worth of breaches on a near-zero variance.
    """
    assert cfg.MIN_ADAPTIVE_SAMPLES > cfg.ADAPTIVE_DEBOUNCE_SAMPLES + 5  # test's own premise holds

    engine = FDIREngine()
    now = drive_to_nominal(engine)
    dt = 0.05

    voltages = (
        [5.00, 5.02, 4.99, 5.01, 4.98]      # small noise -> builds a tiny, nonzero variance
        + [4.85, 4.85, 4.85]                 # a jump that would be many-sigma against that variance
        + [5.00, 5.01, 4.99, 5.00, 5.02, 4.98, 5.00, 5.01, 4.99, 5.00, 5.01]
    )
    assert len(voltages) < cfg.MIN_ADAPTIVE_SAMPLES  # stay inside the warm-up window throughout

    for i, v in enumerate(voltages):
        assert v >= cfg.UNDERVOLTAGE_CRITICAL_V  # isolate this test to the adaptive detector only
        now += dt
        # variant=i: a varying IMU fingerprint keeps this test isolated to the
        # adaptive-baseline detector -- an identical fingerprint reused across
        # 5+ ticks would incidentally trip the unrelated lockup detector too.
        engine.tick(make_sample(bus_voltage_v=v, variant=i), now)
        assert not (engine.fault_flags & FaultFlag.ADAPTIVE_ANOMALY)
        assert engine.mode == Mode.NOMINAL


def test_lockup_boot_guard_regression():
    """
    Regression for the lockup detector's cold-start bug: a fresh boot with a
    stuck/default-valued IMU (any constant reading, including all-zero
    defaults) must not immediately latch SENSOR_LOCKUP before the spacecraft
    has even left BOOT -- _update_lockup's BOOT guard clears the fingerprint
    history every BOOT tick specifically to prevent this.
    """
    engine = FDIREngine()
    now = 0.0
    dt = 0.05
    frozen = make_sample()  # identical values every call, on purpose

    # Feed far more than LOCKUP_WINDOW_SAMPLES identical readings while still
    # safely inside BOOT_DURATION_S -- without the guard this latches almost
    # immediately.
    n_boot_ticks = int(cfg.BOOT_DURATION_S / dt) - 5
    assert n_boot_ticks > cfg.LOCKUP_WINDOW_SAMPLES * 2, "test needs headroom inside BOOT to be meaningful"
    for _ in range(n_boot_ticks):
        engine.tick(frozen, now)
        assert engine.mode == Mode.BOOT
        assert not (engine.fault_flags & FaultFlag.SENSOR_LOCKUP)
        now += dt

    # Finish boot cleanly with varying (non-frozen) samples so the transition
    # itself can't be confused with a fault condition.
    i = 0
    while engine.mode == Mode.BOOT:
        engine.tick(make_sample(variant=i), now)
        i += 1
        now += dt

    assert engine.mode == Mode.NOMINAL
    assert not (engine.fault_flags & FaultFlag.SENSOR_LOCKUP)


def test_comms_loss_boot_guard_regression():
    """
    Regression for run_simulator.Simulator._update_comms_loss's cold-start
    bug: "no ground station has connected yet" is the normal state on every
    power-on, not a fault -- the same class of false positive fixed for the
    adaptive baseline and lockup detectors, and (per that method's own
    docstring) dropped once already during a refactor and deliberately put
    back. This exercises the actual Simulator class rather than FDIREngine in
    isolation, because that is where this particular guard lives.
    """
    sim = Simulator(telemetry_rate_hz=50.0, seed=1234)

    # No ground station ever connects (sim.conn stays None throughout) --
    # exactly the boot-time state that used to trip a false COMMS_LOSS.
    for _ in range(10):
        sim.tick()
        assert sim.engine.mode == Mode.BOOT
        assert not (sim.engine.fault_flags & FaultFlag.COMMS_LOSS)

    # Force boot to look complete without an actual multi-second sleep in the
    # test suite: rewind the recorded boot start rather than the wall clock.
    # (Simulator.tick() reads real time.monotonic() internally, which this
    # test has no other way to control.)
    sim.engine._boot_started_at -= (cfg.BOOT_DURATION_S + 1.0)
    sim.tick()
    assert sim.engine.mode != Mode.BOOT

    # Once out of BOOT, the guard must NOT still be suppressing the flag --
    # still no client connected, so COMMS_LOSS should now be flagged for real.
    assert sim.engine.fault_flags & FaultFlag.COMMS_LOSS
