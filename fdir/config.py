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
