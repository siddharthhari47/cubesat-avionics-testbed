"""
R8: pre-validated, autonomously selectable degraded modes.

WHAT THIS IS FOR. BIRD survived the loss of most of its attitude-control system
because the ground had *already worked out* a reduced way to operate and could
put the spacecraft into it. Odin and QuakeSat show the same shape. The lesson is
not "have a SAFE mode" -- this project already had one -- it is that the
response granularity between "fully working" and "sitting in SAFE doing nothing"
was missing entirely. A vehicle that can only run or stop throws away every
mission-hour that a reduced configuration could still have earned.

HONEST LIMITATION, STATED FIRST BECAUSE IT BOUNDS EVERYTHING BELOW.
R8 says degraded modes must be **pre-validated**, and pre-validated means
*measured*. The power budgets in this file are DECLARED, not measured: no
hardware exists yet, so nobody has put a meter on a rail and confirmed that
MINIMAL actually closes its energy balance. Until V1 does that, this module
provides the mechanism and the selection logic, and the numbers are engineering
estimates carrying exactly as much authority as that description implies.
`declared_only=True` is on every set, and a test asserts it stays there, so this
cannot quietly be forgotten and later read as a measured result.

WHAT THE CASE STUDY DOES NOT SUPPORT, also stated plainly: BIRD's degradation was
GROUND-AUTHORED. No mission in the studied set demonstrated autonomous selection
of a degraded configuration. So this is research rather than replication, and it
should be presented that way -- the requirement is derived from what the failures
suggest, not from a flight-proven pattern.

WHY SELECTION IS DETERMINISTIC. Choosing to shed a subsystem is an action in the
sense that matters: it changes what the spacecraft can do. So it goes through the
same gate everything else does -- a named set of flags, checked in one place. No
advisory detector may cause a downgrade, for the same reason none may command
SAFE.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import FaultFlag, Rail  # noqa: E402


@dataclass(frozen=True)
class CapabilitySet:
    """
    One pre-validated way to operate the spacecraft.

    `rails_powered` is the configuration; `budget_w` is what it is believed to
    cost. Frozen because a capability set that can be edited at runtime is not
    pre-validated by any definition.
    """

    name: str
    level: int                      # 0 = most capable; higher = more degraded
    rails_powered: Tuple[int, ...]
    budget_w: float
    loses: str                      # what the mission gives up at this level
    declared_only: bool = True      # NOT measured -- see module docstring

    def powers(self, rail: int) -> bool:
        return int(rail) in self.rails_powered


# The ladder, most capable first. OBC is on every rung: a configuration that
# removes power from the flight computer is not a degraded mode, it is an
# ending, and the hardware-safety constraint is that nothing in this file may
# ever propose it.
FULL = CapabilitySet(
    name="FULL", level=0,
    rails_powered=(int(Rail.OBC), int(Rail.RADIO), int(Rail.SENSORS),
                   int(Rail.ADCS), int(Rail.PAYLOAD)),
    budget_w=2.00, loses="nothing",
)

REDUCED = CapabilitySet(
    name="REDUCED", level=1,
    rails_powered=(int(Rail.OBC), int(Rail.RADIO), int(Rail.SENSORS),
                   int(Rail.ADCS)),
    budget_w=1.60,
    loses="payload operations; housekeeping, attitude and comms all retained",
)

MINIMAL = CapabilitySet(
    name="MINIMAL", level=2,
    rails_powered=(int(Rail.OBC), int(Rail.RADIO)),
    budget_w=1.10,
    loses="payload and attitude control; keeps the flight computer alive and "
          "the radio listening, which is the configuration from which a ground "
          "station can still do something",
)

# Claims nothing, because nothing is guaranteed. Reached when not even MINIMAL's
# rails are all powered -- which should not happen, since the ladder never sheds
# OBC or RADIO, but "should not happen" is not a property. capability_for()
# previously returned MINIMAL in that case, which CLAIMS OBC and RADIO: an
# over-claim in exactly the state where the vehicle can least afford one, and a
# direct contradiction of the property test written beside it.
BELOW_MINIMAL = CapabilitySet(
    name="BELOW_MINIMAL", level=3,
    rails_powered=(),
    budget_w=0.0,
    loses="more than any validated configuration accounts for; the vehicle is "
          "in a state the ladder does not describe and nothing may be assumed "
          "about what still works",
)

LADDER: Tuple[CapabilitySet, ...] = (FULL, REDUCED, MINIMAL)

# The last rung keeps the radio deliberately. CSSWE is the reason: the one asset
# that must survive a degradation is the one the ground needs in order to
# intervene at all. Shedding the radio to save power would be optimising the
# energy balance of a spacecraft nobody can reach.

# Conditions that may cause an autonomous DOWNGRADE, and the level each argues
# for. Deterministic flags only -- ADAPTIVE_ANOMALY and ML_ANOMALY appear
# nowhere here, exactly as they appear in neither authority set in engine.py.
DEGRADE_TRIGGERS: Dict[int, int] = {
    # A rail eating more than its share: shed non-essential load rather than
    # wait for the battery to reach a voltage threshold.
    int(FaultFlag.RAIL_OVERCURRENT): 1,
    # Measured degradation against the commissioning reference (R7). This is
    # the pairing that makes R7 worth having: a slow decline that no fixed
    # threshold catches now produces a proportionate response instead of
    # being merely reported.
    int(FaultFlag.DRIFT_FROM_REFERENCE): 1,
    # Both together mean the decline is real and something is actively
    # draining -- go further down.
    int(FaultFlag.RAIL_OVERCURRENT | FaultFlag.DRIFT_FROM_REFERENCE): 2,
}


def select_level(fault_flags: FaultFlag) -> int:
    """
    Which rung the current evidence argues for. 0 means no degradation needed.

    Deliberately monotonic in evidence: more corroborating conditions select a
    more degraded set, never a less degraded one. Nothing here can select a
    level from an advisory flag, because no advisory flag appears in
    DEGRADE_TRIGGERS.
    """
    level = 0
    for trigger, want in DEGRADE_TRIGGERS.items():
        if int(fault_flags) & trigger == trigger:
            level = max(level, want)
    return min(level, len(LADDER) - 1)


def set_for_level(level: int) -> CapabilitySet:
    return LADDER[max(0, min(level, len(LADDER) - 1))]


def capability_for(rails_on) -> CapabilitySet:
    """
    The most capable set whose rails are ALL actually powered.

    This is how capability stops being a claim and starts being an observation.
    Round 5 and round 6 both found the engine asserting a configuration the
    hardware was not in -- once by advancing optimistically, once by leaving
    capability at FULL after a partial shed that a refused rollback could not
    undo. Deriving the answer from what is powered makes both unrepresentable
    rather than separately guarded.

    Falls to BELOW_MINIMAL if not even the last rung is satisfied. Returning
    MINIMAL there would claim OBC and RADIO are powered on the strength of them
    being the least this vehicle needs -- which is an assumption, not an
    observation, and over-claiming is the direction that gets acted on.
    """
    on = {int(r) for r in rails_on}
    for cs in LADDER:                       # most capable first
        if all(r in on for r in cs.rails_powered):
            return cs
    return BELOW_MINIMAL


def rails_to_shed(current: CapabilitySet, target: CapabilitySet) -> List[int]:
    """
    Rails powered in `current` and not in `target`.

    Returned rather than acted on: this module decides WHAT the configuration
    should be, and the executor is still the only thing that commands hardware.
    Same engine-proposes/executor-acts split as recovery.
    """
    return [r for r in current.rails_powered if not target.powers(r)]
