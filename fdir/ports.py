"""
The actuation seam: what FDIR is allowed to ask the hardware to do.

DESIGN DECISION (audit option (b), adopted in the V0 gap analysis as C5):
FDIREngine does NOT hold references to these ports and never calls them. It
emits `RecoveryIntent` objects; a separate RecoveryExecutor owns the ports and
carries them out. Two reasons, both load-bearing:

  * The engine's whole premise is being a near-pure function of
    (sample, time, advisory) -> (mode, flags). Calling actuators from inside
    tick() breaks that, and every existing test that constructs a bare
    FDIREngine() would suddenly need a fake.
  * An intent is directly assertable. "exactly one radio power-cycle was
    proposed, and not before T" is a better piece of fault-injection evidence
    than a mock call count, and it survives the executor being swapped out.

It also satisfies the project rule that no GPIO/I2C/SPI detail may appear in
FDIR: the engine names a device and an action, nothing more.

SCOPE, DELIBERATELY NARROW: only PowerPort and ResetPort exist here, because
only those two have a real caller today. CurrentSensePort and DeviceProbePort
were considered and rejected for now -- `RawSample` already carries
`rail_current_a`, `imu_responded` and `radio_responded`, so the sample IS the
sensing path and a second interface for it would be ceremony. WatchdogPort and
RecoveryStorePort wait for Phase 5, which is when a watchdog and persistent
recovery state first have anything to do. Six empty protocols sitting above a
400-line engine is exactly the "documentation outgrowing working code" failure
CLAUDE.md warns about.

C-PORTABILITY CONSTRAINT: these signatures are meant to become a
`struct fdir_platform { ... }` of function pointers when FDIR is ported to
firmware. So: integer device ids (IntEnum -> uint8_t), bool/float returns
(-> status codes), no keyword arguments, and no exceptions as control flow. A
port that cannot do what was asked returns False; it does not raise.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol


class RecoveryAction(IntEnum):
    """
    The complete set of actions FDIR may propose. Deliberately tiny.

    Every entry must be bounded and reversible. There is no DEORBIT, no
    ERASE, no FIRMWARE_UPDATE, and nothing that cannot be undone by the next
    action -- the failure research found recovery actions that made things
    worse, and the cheapest defence is an action vocabulary too small to
    contain one.
    """

    NONE = 0
    POWER_CYCLE = 1      # remove power for a bounded interval, then restore
    RESET_DEVICE = 2     # assert a device's reset line without cutting power


@dataclass
class RecoveryIntent:
    """
    A request, not an instruction. The executor may refuse it.

    `reason` carries the fault flag(s) that justified the request, so a refused
    or failed intent can be attributed after the fact rather than guessed at.
    """

    action: RecoveryAction
    target: int              # icd.Rail value; uint8_t device id in C
    reason: str
    requested_at: float
    attempt: int = 1


class PowerPort(Protocol):
    """Switch and read back a rail's power state."""

    def set_enabled(self, dev: int, on: bool) -> bool:
        """Returns False if the command could not be carried out."""
        ...

    def is_enabled(self, dev: int) -> bool:
        """Readback -- ideally a power-good pin, not the commanded value."""
        ...


class ResetPort(Protocol):
    """Reset a device or the whole flight computer."""

    def reset_device(self, dev: int) -> bool:
        ...

    def reset_system(self, reason: str) -> None:
        """Does not return on real hardware."""
        ...
