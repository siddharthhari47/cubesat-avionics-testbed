"""
Simulated implementations of the fdir/ports.py hardware interfaces.

These satisfy PowerPort and ResetPort by driving SpacecraftEnvironment's
actuator entry points. When real hardware arrives, an STM32 driver implements
the same Protocols against GPIO and load switches, and nothing in fdir/ changes
-- that substitutability is the entire reason the ports exist.

Note what these do NOT do: they never read the environment's ground truth
(`active_faults`, `rail_latched`) and never tell FDIR whether an action
"worked". A port reports only what a real driver could report -- whether the
command was accepted, and what a power-good readback says. Whether the fault
actually cleared has to be re-observed through telemetry like any other fact,
which is exactly the discipline the KySat-2 case is about.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import Rail  # noqa: E402


class SimulatedPowerPort:
    """PowerPort backed by SpacecraftEnvironment.set_rail_power()."""

    def __init__(self, env):
        self._env = env

    def set_enabled(self, dev: int, on: bool) -> bool:
        try:
            rail = Rail(dev)
        except ValueError:
            return False          # unknown device: refuse, do not raise
        return self._env.set_rail_power(rail, on)

    def is_enabled(self, dev: int) -> bool:
        try:
            rail = Rail(dev)
        except ValueError:
            return False
        return bool(self._env.rail_powered[rail])


class SimulatedResetPort:
    """ResetPort backed by SpacecraftEnvironment.obc_reset()."""

    def __init__(self, env):
        self._env = env

    def reset_device(self, dev: int) -> bool:
        # A device-level reset line is not modelled yet. Returning False is the
        # honest answer -- an executor must treat "the port could not do it" as
        # a real outcome rather than assuming success, and a stub that silently
        # returned True would teach the recovery logic a lie.
        return False

    def reset_system(self, reason: str) -> None:
        self._env.obc_reset()
