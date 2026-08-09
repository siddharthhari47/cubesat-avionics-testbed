"""
End-to-end FDIR demonstration: normal telemetry -> fault introduced -> anomaly
detected -> fault assessed -> FDIR responds -> recovery -> back to normal.

This is the demonstrable proof of the "immune system" architecture: a
deterministic detector (fdir/engine.py) driven by a seeded, reproducible
spacecraft model (simulator/environment.py), with no sockets and no ML model
involved -- the undervoltage fault is fully detected and recovered by FDIR
alone, per FDIR-003 (docs/requirements/SRS.md). ML plugs into the same
FDIREngine.tick(..., ml_advisory=...) call but is deliberately out of scope
here; see fdir/engine.py's module docstring for why ML can never gate a mode
transition by itself.

Everything asserted below is against **simulated** telemetry (SpacecraftEnvironment),
not hardware -- the debounce/threshold values in fdir/config.py are documented
design targets, not hardware-characterized numbers (no hardware exists yet).

Run `pytest -s tests/test_integration_demo.py` to see the printed transcript;
it reads like the demo the project owner asked for.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))                  # for `fdir` (a real package)
sys.path.insert(0, str(_ROOT / "simulator"))     # for `environment`/`protocol` (not packages)

from environment import SpacecraftEnvironment  # noqa: E402
from protocol import FaultFlag, Mode  # noqa: E402
from fdir import config as cfg  # noqa: E402
from fdir.engine import FDIREngine  # noqa: E402

SEED = 2024
DT = 0.02  # 50 Hz scenario clock -- fine enough to resolve the 100 ms
           # UNDERVOLTAGE_DEBOUNCE_S window (FDIR-003) into several samples
           # rather than a single lucky/unlucky one.

# Safety nets so a regression that breaks a transition fails fast with a
# clear message instead of hanging the test suite in an infinite loop.
MAX_BOOT_TICKS = 1000
MAX_DETECTION_TICKS = 500


def _tick(env: SpacecraftEnvironment, engine: FDIREngine):
    """Advance the environment by one DT and feed the resulting sample to
    FDIR. `env.t` is the single shared clock -- no ML advisory (out of scope
    for this demo, see module docstring)."""
    sample, _truth = env.step(DT)
    engine.tick(sample, env.t)
    return sample


def _dedup_sequence(mode_history):
    """Collapse a per-tick mode history into the sequence of distinct
    transitions, e.g. [BOOT, BOOT, BOOT, NOMINAL, NOMINAL] -> [BOOT, NOMINAL]."""
    sequence = []
    for m in mode_history:
        if not sequence or sequence[-1] != m:
            sequence.append(m)
    return sequence


def test_undervoltage_fault_full_fdir_cycle():
    env = SpacecraftEnvironment(seed=SEED)
    engine = FDIREngine()
    mode_history = []

    # ---- Phase 1: BOOT self-check --------------------------------------
    print("\n=== Phase 1: BOOT self-check ===")
    boot_ticks = 0
    while engine.mode == Mode.BOOT:
        sample = _tick(env, engine)
        mode_history.append(engine.mode)
        boot_ticks += 1
        assert boot_ticks < MAX_BOOT_TICKS, "engine never left BOOT -- self-check stalled"
    print(f"[t={env.t:6.2f}s] boot self-check clean after {boot_ticks} ticks "
          f"-> mode={engine.mode.name}")
    assert engine.mode == Mode.NOMINAL, "a clean boot (no faults present) should reach NOMINAL directly"

    # ---- Phase 2: normal telemetry --------------------------------------
    print("\n=== Phase 2: normal telemetry ===")
    for _ in range(25):
        sample = _tick(env, engine)
        mode_history.append(engine.mode)
        assert engine.mode == Mode.NOMINAL
    print(f"[t={env.t:6.2f}s] nominal: bus_voltage={sample.bus_voltage_v:.2f} V, "
          f"temp={sample.temp_c:.1f} C, mode={engine.mode.name}, faults={engine.fault_flags!r}")

    # ---- Phase 3: fault introduced -> anomaly detected -> FDIR responds ---
    print("\n=== Phase 3: undervoltage fault introduced ===")
    t_fault_injected = env.t
    env.inject("undervoltage")  # ground truth only -- FDIR never reads this call, only raw samples
    print(f"[t={env.t:6.2f}s] undervoltage fault injected into the environment "
          f"(bus target ~{cfg.UNDERVOLTAGE_CRITICAL_V - 0.2:.1f} V, critical threshold "
          f"{cfg.UNDERVOLTAGE_CRITICAL_V:.1f} V)")

    detection_ticks = 0
    while engine.mode != Mode.SAFE:
        sample = _tick(env, engine)
        mode_history.append(engine.mode)
        detection_ticks += 1
        assert detection_ticks < MAX_DETECTION_TICKS, (
            "FDIR never entered SAFE mode despite sustained critical undervoltage"
        )
    t_safe_entered = env.t
    detection_latency_s = t_safe_entered - t_fault_injected
    print(f"[t={env.t:6.2f}s] fault assessed, FDIR responds -> mode={engine.mode.name} "
          f"after {detection_ticks} ticks (detection latency "
          f"{detection_latency_s * 1000:.1f} ms), bus_voltage={sample.bus_voltage_v:.2f} V, "
          f"faults={engine.fault_flags!r}")

    assert engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL

    # FDIR-003: a critical reading must persist >= 100 ms (UNDERVOLTAGE_DEBOUNCE_S)
    # before SAFE mode is entered. Budget allows a few extra ticks of
    # scenario-clock granularity on top of the debounce window itself.
    budget_s = cfg.UNDERVOLTAGE_DEBOUNCE_S + 3 * DT
    assert detection_latency_s <= budget_s, (
        f"FDIR-003 latency budget exceeded: SAFE entry took "
        f"{detection_latency_s * 1000:.1f} ms, budget is {budget_s * 1000:.1f} ms "
        f"({cfg.UNDERVOLTAGE_DEBOUNCE_S * 1000:.0f} ms debounce + tick granularity)"
    )
    # ...and shouldn't be suspiciously instant either -- confirms the debounce
    # is actually filtering, not being bypassed.
    assert detection_latency_s >= cfg.UNDERVOLTAGE_DEBOUNCE_S - DT, (
        "SAFE entry happened faster than the documented 100 ms debounce window allows"
    )

    # ---- Phase 4: SAFE mode -- premature recovery correctly rejected ------
    print("\n=== Phase 4: SAFE mode holds; operator attempts premature recovery ===")
    for _ in range(10):
        sample = _tick(env, engine)
        mode_history.append(engine.mode)
        assert engine.mode == Mode.SAFE  # FDIR-005: never exits SAFE on its own

    accepted = engine.exit_safe_mode(env.t)
    mode_history.append(engine.mode)
    print(f"[t={env.t:6.2f}s] EXIT_SAFE_MODE commanded while fault still active "
          f"-> accepted={accepted}, mode={engine.mode.name}")
    assert accepted is False, "FDIR-005: exit must be rejected while the triggering fault is still active"
    assert engine.mode == Mode.SAFE

    # ---- Phase 5: fault clears, operator commands recovery ----------------
    print("\n=== Phase 5: fault clears, operator commands recovery ===")
    env.clear("undervoltage")
    print(f"[t={env.t:6.2f}s] underlying undervoltage condition cleared (ground truth)")

    for _ in range(15):
        sample = _tick(env, engine)
        mode_history.append(engine.mode)
    print(f"[t={env.t:6.2f}s] bus_voltage={sample.bus_voltage_v:.2f} V restored, but mode is "
          f"still {engine.mode.name} -- latched fault flags persist until an explicit "
          f"RESET_FAULTS (flags don't self-clear, only debounce state does)")
    assert engine.mode == Mode.SAFE, "latched flag must survive the physical condition clearing on its own"
    assert sample.bus_voltage_v > cfg.UNDERVOLTAGE_WARNING_V, "sanity check: condition genuinely cleared"

    engine.reset_faults(env.t)
    print(f"[t={env.t:6.2f}s] RESET_FAULTS commanded -> faults={engine.fault_flags!r}")
    assert not (engine.fault_flags & FaultFlag.UNDERVOLTAGE_CRITICAL)

    accepted = engine.exit_safe_mode(env.t)
    mode_history.append(engine.mode)
    print(f"[t={env.t:6.2f}s] EXIT_SAFE_MODE commanded -> accepted={accepted}, mode={engine.mode.name}")
    assert accepted is True
    assert engine.mode == Mode.NOMINAL

    # ---- Phase 6: system back to normal ------------------------------------
    print("\n=== Phase 6: system back to normal ===")
    for _ in range(15):
        sample = _tick(env, engine)
        mode_history.append(engine.mode)
        assert engine.mode == Mode.NOMINAL
    print(f"[t={env.t:6.2f}s] stable NOMINAL: bus_voltage={sample.bus_voltage_v:.2f} V, "
          f"faults={engine.fault_flags!r}")

    # ---- Overall mode-sequence assertion -----------------------------------
    sequence = _dedup_sequence(mode_history)
    assert sequence == [Mode.BOOT, Mode.NOMINAL, Mode.SAFE, Mode.NOMINAL], (
        f"unexpected mode sequence: {[m.name for m in sequence]}"
    )

    # ---- Transcript: the FDIR engine's own log, timestamped ----------------
    print("\n=== FDIR engine log (full transcript) ===")
    for t, message in engine.log:
        print(f"[t={t:7.3f}s] {message}")
    print(f"\nFinal mode sequence: {' -> '.join(m.name for m in sequence)}")
    print(f"Undervoltage detection latency: {detection_latency_s * 1000:.1f} ms "
          f"(FDIR-003 budget: {budget_s * 1000:.1f} ms)")
