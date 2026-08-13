"""
FDIR thresholds and debounce windows, each traced to an SRS requirement ID.

Single source of truth: these used to be scattered module-level constants
inside simulator/run_simulator.py. Every value here is a design TARGET, not a
measured fact -- none of it has been characterized against real hardware yet.
Revisit once V1 hardware exists to time actual sensor/bus behavior.
"""

# --- mode timing -------------------------------------------------------------

BOOT_DURATION_S = 2.0                  # SYS-003 target: <=5s

# --- sensor timeout (FDIR-002) ------------------------------------------------

SENSOR_TIMEOUT_DEBOUNCE_S = 0.05

# --- undervoltage (FDIR-003) --------------------------------------------------

NOMINAL_VOLTAGE_V = 5.0
UNDERVOLTAGE_WARNING_V = 4.5
UNDERVOLTAGE_CRITICAL_V = 4.0
UNDERVOLTAGE_DEBOUNCE_S = 0.10

# --- adaptive/statistical baseline (FDIR-006) ---------------------------------

EWMA_ALPHA = 0.1
ADAPTIVE_K = 4.0                       # flag if |x - mean| exceeds this many std devs
ADAPTIVE_DEBOUNCE_SAMPLES = 3
MIN_ADAPTIVE_SAMPLES = 20              # warm-up before the baseline is trusted at all

# --- ML advisory gate (FDIR-007) ----------------------------------------------
# The debounce that ML_ANOMALY must satisfy before it latches -- identical in
# kind to every deterministic detector's debounce. See fdir/engine.py's
# SAFE_MODE_TRIGGER_FLAGS for the (deliberate) fact that this flag can never by
# itself force a mode transition.

ML_ANOMALY_DEBOUNCE_SAMPLES = 3

# --- communications loss (COM-003) --------------------------------------------

COMMS_LOSS_TIMEOUT_S = 5.0

# --- thermal anomaly (FDIR-009) -----------------------------------------------
# Single critical band for Phase 1 (no separate warning tier yet -- add one if
# a real thermal test campaign shows it's needed; not invented ahead of data).

THERMAL_CRITICAL_LOW_C = -10.0
THERMAL_CRITICAL_HIGH_C = 55.0
THERMAL_DEBOUNCE_S = 0.20

# --- sensor lockup (FDIR-010) --------------------------------------------------
# A "locked up" sensor keeps ACKing (unlike a timeout) but returns an unchanging
# value. Real sensor noise makes an exact repeat across many consecutive samples
# vanishingly unlikely in nominal operation, so this is a legitimate, cheap,
# ground-truth-free detector -- not a simulation shortcut.

LOCKUP_WINDOW_SAMPLES = 5

# --- SAFE-mode false-positive budget (FDIR-008) -------------------------------

MAX_FALSE_SAFE_ENTRIES_PER_6H = 1

# --- autonomous recovery trigger (CSSWE / R5) ---------------------------------
# DELIBERATELY DISTINCT from COMMS_LOSS_TIMEOUT_S, and conflating the two would
# be a real bug. That one (5 s) is a link heartbeat: it decides when to *flag*
# loss of contact. This one decides when to *act* on it. The case study's CSSWE
# inference is "no ground contact for N hours" -- reusing the 5 s heartbeat as
# the action trigger would power-cycle the radio every five seconds.
#
# This value is a TEST-SCALE default so scenarios run in seconds. A flight value
# would be hours, and the recovery logic must not be tuned to either -- the
# scenario suite runs the same ladder at test and flight-plausible timings to
# prove it isn't.
COMMS_RECOVERY_TRIGGER_S = 30.0

# How long power must be removed for a latch-up to clear. Mirrors the physical
# constant in simulator/environment.py; on real hardware this is a property of
# the part, to be measured rather than assumed.
POWER_CYCLE_OFF_TIME_S = 0.150

# After an action completes, how long to OBSERVE before deciding whether it
# achieved anything. A recovery is not verified by the port accepting the
# command -- that conflation is the KySat-2 failure. Long enough for a
# device to re-enumerate and for telemetry to reflect reality.
RECOVERY_VERIFY_WINDOW_S = 2.0

# A rail is considered back in its expected band below this draw. Coarse on
# purpose -- it separates "latched" from "nominal", not fine gradations.
RAIL_NOMINAL_CURRENT_CEILING_A = 0.25

# --- reset evidence (D2) -------------------------------------------------------
# RESET_FAULTS may only clear a latched flag on POSITIVE evidence that the
# condition has gone away -- this many consecutive non-breaching observations --
# never on mere absence of evidence.
#
# Why this exists: reset_faults() used to infer "cleared" from a debounce timer
# being None. start_boot() sets those timers to None, so reboot -> RESET_FAULTS
# -> EXIT_SAFE_MODE returned a still-faulted vehicle to service, defeating the
# very requirement (FDIR-005) the guards were written to enforce. Counting
# clean observations cannot be satisfied by erasing state.
RESET_EVIDENCE_SAMPLES = 3

# FDIR-011: how long a rail must draw above RAIL_NOMINAL_CURRENT_CEILING_A
# before RAIL_OVERCURRENT latches. Short, because unlike a voltage sag there is
# no benign transient that holds a rail at several times its nominal draw --
# and because the whole point of catching this is to act before the battery is
# gone, which is the margin KySat-2 did not have.
RAIL_OVERCURRENT_DEBOUNCE_S = 0.5

# FDIR-012: consecutive samples a single device must return implausible values
# before SENSOR_IMPLAUSIBLE latches. Matched to LOCKUP_WINDOW_SAMPLES so that a
# corrupt channel and a frozen one are held to the same standard of evidence --
# a one-sample glitch is not a failed sensor.
IMPLAUSIBLE_DEBOUNCE_SAMPLES = 5
