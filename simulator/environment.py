"""
Seedable spacecraft environment: physical state + fault injection.

This is deliberately separate from fdir/engine.py. FDIR decides system state
from what it can observe; this module decides what a (possibly faulty)
spacecraft would physically produce. Neither should know about the other --
FDIR must never read this module's ground truth, and this module has no
concept of BOOT/NOMINAL/SAFE/TEST at all, only physics.

PHASE 2 REWRITE -- WHY:

Before this, the environment had no state to perturb. It was four attributes
and a set of per-signal output overrides:

    voltage = NOMINAL_VOLTAGE_V + gauss(0, 0.02)
    if "undervoltage" in self._fault_start:
        voltage = UNDERVOLTAGE_INJECTED_V + gauss(0, 0.02)

Signals were mutually independent by construction: current did not depend on
voltage, temperature did not depend on dissipated power, nothing depended on
load. Measured consequence -- **no injected fault perturbed bus_current_a at
all**, and overcurrent is the canonical latch-up signature.

That makes the interesting scenarios unbuildable, because every one of them is
defined by a coupling rather than by a value:

  * radio_latchup     current up AND voltage sags AND the radio heats AND comms
                      dies AND it survives a reset -- five observables from one
                      state change.
  * rail_overcurrent  a load eats the battery while every fixed voltage
                      threshold stays happy, until hours later it doesn't.
  * data_bus_failure  several channels invalid at once, which is meaningless
                      without a bus topology.
  * recovery_failure  requires the environment to accept an actuator command
                      and legitimately not respond to it.

So state comes first and signals are derived from it. `bus_current_a` is now
the sum of per-rail draws, and `bus_voltage_v` is `v_oc - I*R_internal`. A
latch-up sets one rail's draw multiplier; the current rise, the voltage sag,
the extra heat and the faster battery drain all follow **by construction**
rather than being hand-written into five places.

THE CLEARING RULE (the whole point, in four lines):

    power removed >= LATCH_CLEAR_OFF_TIME_S  -> latch clears
    OBC reset                                -> latch UNCHANGED
    RESET_FAULTS (a flag operation)          -> latch UNCHANGED, it never
                                                reaches this module at all

That asymmetry is the CSSWE / KySat-2 distinction made executable. CSSWE
recovered because something eventually removed power from a latched radio;
KySat-2 died because it reset hourly forever and a reset does not clear a
latch. A per-scenario `latch_clears_on_power_cycle=False` builds the
recovery_failure case, where the action executes correctly and the fault
persists anyway.

Reproducibility: every source of randomness goes through a single
random.Random(seed) instance owned by the environment, never the `random`
module's global state. Same seed + same fault schedule + same actuator
commands = byte-identical telemetry. Actuator commands consume no RNG draws,
deliberately, so issuing one cannot shift the noise stream and break replay.
"""

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icd import Bus, Device, Rail, RawSample, ThermalNode  # noqa: E402

# --- nominal operating point --------------------------------------------------
# Chosen so the derived values reproduce the pre-Phase-2 constants exactly:
#   bus_current_a = sum(rail draws)            = 0.40 A
#   bus_voltage_v = 5.06 - 0.40 * 0.15         = 5.00 V
# Existing tests pin both, so the physics had to land on the old numbers rather
# than the numbers being adjusted to suit the physics.

RAIL_NOMINAL_DRAW_A: Dict[int, float] = {
    Rail.OBC: 0.12,
    Rail.RADIO: 0.10,
    Rail.SENSORS: 0.06,
    Rail.ADCS: 0.08,
    Rail.PAYLOAD: 0.04,
}

BATTERY_V_OC = 5.06
BATTERY_R_NOMINAL_OHM = 0.15
BATTERY_CAPACITY_AH = 2.6

NOMINAL_VOLTAGE_V = 5.0      # derived; retained as a name because tests import it
NOMINAL_CURRENT_A = 0.40     # derived; ditto
NOMINAL_TEMP_C = 25.0
AMBIENT_C = 25.0

# Thermal: first-order lag per node. dT/dt = (P*R_th - (T - ambient)) / tau
THERMAL_R_TH_C_PER_W = 15.0
THERMAL_TAU_S = 45.0

# --- fault magnitudes ---------------------------------------------------------
# undervoltage: a battery open-circuit collapse, not a voltage override.
#   5.06 - 1.20 - 0.40*0.15 = 3.80 V, matching the old injected constant.
UNDERVOLTAGE_V_OC_SAG = 1.20
UNDERVOLTAGE_INJECTED_V = 3.80   # retained name; now a derived expectation

# gradual_drift: rising battery internal resistance -- real battery degradation.
#   5.06 - 0.40*1.90 = 4.30 V at full ramp, matching DRIFT_FLOOR_V.
# Modelling it as resistance rather than a voltage ramp is what makes the sag
# load-dependent: the deviation grows with current draw, which is the signal a
# fixed voltage threshold cannot see and an adaptive baseline absorbs.
DRIFT_R_INTERNAL_FINAL_OHM = 1.90
DRIFT_FLOOR_V = 4.3
DRIFT_RAMP_DURATION_S = 30.0

THERMAL_INJECTED_C = 70.0

# Latch-up: multiplier on a rail's draw, and how long power must be removed.
LATCHUP_DRAW_MULTIPLIER = 4.5
LATCH_CLEAR_OFF_TIME_S = 0.100

FAULT_TYPES = (
    "sensor_timeout",   # IMU stops ACKing entirely
    "sensor_lockup",    # IMU ACKs but returns a frozen value
    "undervoltage",     # battery open-circuit voltage collapses
    "gradual_drift",    # battery internal resistance rises (load-dependent sag)
    "thermal",          # spacecraft thermal excursion
    "radio_latchup",    # SEL: draw up, unresponsive, hot, comms dead, needs power removal
    "rail_overcurrent",  # a load eats the battery while voltage thresholds stay happy
    "communication_loss",  # link down, everything else healthy
    "data_bus_failure",    # Delfi-C3: the PATH fails, the devices are fine
    # The CONTROL for radio_latchup: identical comms symptom, nominal current.
    # Without this pair, radio_latchup is a detection test rather than an
    # isolation test -- and isolation is what four of five documented FDIR
    # failures actually lacked.
    "radio_unresponsive",
    # The single-device partner to data_bus_failure.
    "sensor_corruption",
)


@dataclass
class GroundTruth:
    """
    The answer key. Available to the test harness and dataset generator, never
    to fdir/engine.py or to ml/ inference at deployment time -- only at
    training/evaluation time, where knowing what actually happened is the
    entire point.
    """

    t: float
    active_faults: List[str]
    rail_latched: Dict[int, bool] = field(default_factory=dict)
    battery_soc: float = 1.0


class SpacecraftEnvironment:
    def __init__(self, seed: Optional[int] = None, latch_clears_on_power_cycle: bool = True):
        self.rng = random.Random(seed)
        self.t = 0.0
        self._fault_start: Dict[str, float] = {}
        self._frozen_imu_values: Optional[Dict[str, float]] = None

        # Whether a correctly-executed power cycle actually clears a latch.
        # False builds the recovery_failure case: the action runs, the
        # environment reports the rail as cycled, and the fault persists --
        # physically a latch upstream of the switch, or a failed switch.
        self.latch_clears_on_power_cycle = latch_clears_on_power_cycle

        # --- power -----------------------------------------------------------
        self.rail_powered: Dict[int, bool] = {r: True for r in Rail}
        self.rail_latched: Dict[int, bool] = {r: False for r in Rail}
        self._rail_draw_multiplier: Dict[int, float] = {r: 1.0 for r in Rail}
        self._rail_off_since: Dict[int, Optional[float]] = {r: None for r in Rail}

        self.battery_r_internal_ohm = BATTERY_R_NOMINAL_OHM
        self.battery_soc = 1.0
        self._v_oc_sag = 0.0

        # --- thermal ---------------------------------------------------------
        self.node_temp_c: Dict[int, float] = {n: AMBIENT_C for n in ThermalNode}

        # --- devices ---------------------------------------------------------
        self.device_responsive: Dict[str, bool] = {
            "imu": True, "temp": True, "mag": True, "radio": True,
        }
        # Data paths, separate from the devices on them. Delfi-C3's CDHS flaw
        # "often prevented data transmission on the bus, leading to insertion of
        # zero's in the telemetry data" -- the sensors themselves were fine. A
        # simulation that models this by breaking three sensors has built three
        # sensor faults, not a bus fault, and proves nothing about isolation.
        self.bus_healthy: Dict[int, bool] = {b: True for b in Bus}
        self.mag_corrupt = False
        self.link_healthy = True
        self.last_ground_contact_t = 0.0

        self.obc_boot_count = 0

    # ---- fault injection (ground truth) -------------------------------------

    def inject(self, fault_name: str) -> None:
        if fault_name not in FAULT_TYPES:
            raise ValueError(f"unknown fault type {fault_name!r}, expected one of {FAULT_TYPES}")
        if fault_name in self._fault_start:
            return
        self._fault_start[fault_name] = self.t

        if fault_name == "sensor_lockup":
            self._frozen_imu_values = self._raw_imu_sample()
        elif fault_name == "radio_latchup":
            # One state change; every observable consequence is derived.
            self.rail_latched[Rail.RADIO] = True
            self._rail_draw_multiplier[Rail.RADIO] = LATCHUP_DRAW_MULTIPLIER
            self.device_responsive["radio"] = False
            self.link_healthy = False
        elif fault_name == "rail_overcurrent":
            self.rail_latched[Rail.PAYLOAD] = True
            self._rail_draw_multiplier[Rail.PAYLOAD] = 30.0
        elif fault_name == "communication_loss":
            self.link_healthy = False
        elif fault_name == "data_bus_failure":
            self.bus_healthy[Bus.I2C_A] = False
        elif fault_name == "radio_unresponsive":
            # Deliberately NOT latched and NOT drawing extra current: the whole
            # point is that this is indistinguishable from radio_latchup on the
            # link alone, and separable only by per-rail current.
            self.device_responsive["radio"] = False
            self.link_healthy = False
        elif fault_name == "sensor_corruption":
            self.mag_corrupt = True

    def clear(self, fault_name: str) -> None:
        """
        Remove an injected condition. NOTE: this is the test harness reaching in
        and un-breaking the hardware -- it is not a recovery action, and nothing
        the flight software does can call it. Latches raised by that condition
        are cleared here too, because the physical cause is being removed.
        """
        self._fault_start.pop(fault_name, None)
        if fault_name == "sensor_lockup":
            self._frozen_imu_values = None
        elif fault_name == "radio_latchup":
            self.rail_latched[Rail.RADIO] = False
            self._rail_draw_multiplier[Rail.RADIO] = 1.0
            self.device_responsive["radio"] = True
            self.link_healthy = True
        elif fault_name == "rail_overcurrent":
            self.rail_latched[Rail.PAYLOAD] = False
            self._rail_draw_multiplier[Rail.PAYLOAD] = 1.0
        elif fault_name == "communication_loss":
            self.link_healthy = True
        elif fault_name == "data_bus_failure":
            self.bus_healthy[Bus.I2C_A] = True
        elif fault_name == "radio_unresponsive":
            self.device_responsive["radio"] = True
            self.link_healthy = True
        elif fault_name == "sensor_corruption":
            self.mag_corrupt = False
        elif fault_name == "gradual_drift":
            self.battery_r_internal_ohm = BATTERY_R_NOMINAL_OHM
        elif fault_name == "undervoltage":
            self._v_oc_sag = 0.0

    def clear_all(self) -> None:
        for name in list(self._fault_start):
            self.clear(name)
        self._fault_start.clear()
        self._frozen_imu_values = None

    def active_faults(self) -> List[str]:
        return sorted(self._fault_start)

    # ---- actuators (what flight software is allowed to command) -------------
    # These consume no RNG draws, deliberately: issuing a command must not shift
    # the noise stream, or a replay with recovery actions would diverge from one
    # without them and determinism testing would be meaningless.

    def set_rail_power(self, rail: int, on: bool) -> bool:
        """
        Switch a rail. Returns True if the command was accepted.

        Turning a rail back on clears its latch only if power was removed for at
        least LATCH_CLEAR_OFF_TIME_S -- a too-brief cycle is a real failure mode
        and the scenarios assert on it.
        """
        rail = Rail(rail)
        if not on:
            if self.rail_powered[rail]:
                self._rail_off_since[rail] = self.t
            self.rail_powered[rail] = False
            return True

        off_since = self._rail_off_since[rail]
        off_duration = None if off_since is None else self.t - off_since
        if (self.latch_clears_on_power_cycle
                and off_duration is not None
                and off_duration >= LATCH_CLEAR_OFF_TIME_S):
            if self.rail_latched[rail]:
                self.rail_latched[rail] = False
                self._rail_draw_multiplier[rail] = 1.0
                if rail == Rail.RADIO:
                    self.device_responsive["radio"] = True
                    self.link_healthy = True
                    self._fault_start.pop("radio_latchup", None)
                elif rail == Rail.PAYLOAD:
                    self._fault_start.pop("rail_overcurrent", None)
        self.rail_powered[rail] = True
        self._rail_off_since[rail] = None
        return True

    def obc_reset(self) -> None:
        """
        Reboot the flight computer. Physics is untouched on purpose: a latched
        rail stays latched, currents stay elevated, thermal state keeps
        integrating, SOC keeps falling. This is the KySat-2 asymmetry -- the
        software forgets, the hardware does not.
        """
        self.obc_boot_count += 1

    def note_ground_contact(self) -> None:
        self.last_ground_contact_t = self.t

    # ---- physics -------------------------------------------------

    def _raw_imu_sample(self) -> Dict[str, float]:
        return {
            "accel_x": self.rng.gauss(0, 0.01),
            "accel_y": self.rng.gauss(0, 0.01),
            "accel_z": 1.0 + self.rng.gauss(0, 0.01),
            "gyro_x": self.rng.gauss(0, 0.5),
            "gyro_y": self.rng.gauss(0, 0.5),
            "gyro_z": self.rng.gauss(0, 0.5),
        }

    def _rail_currents(self) -> Dict[int, float]:
        return {
            r: (RAIL_NOMINAL_DRAW_A[r] * self._rail_draw_multiplier[r]
                if self.rail_powered[r] else 0.0)
            for r in Rail
        }

    def _update_thermal(self, dt: float, rail_currents: Dict[int, float], bus_v: float) -> None:
        node_of_rail = {
            Rail.RADIO: ThermalNode.RADIO,
            Rail.OBC: ThermalNode.OBC,
            Rail.SENSORS: ThermalNode.STRUCTURE,
            Rail.ADCS: ThermalNode.STRUCTURE,
            Rail.PAYLOAD: ThermalNode.STRUCTURE,
        }
        power_at_node: Dict[int, float] = {n: 0.0 for n in ThermalNode}
        for r, i in rail_currents.items():
            power_at_node[node_of_rail[r]] += i * bus_v
        # The battery dissipates I^2 * R internally -- which is why a rising
        # internal resistance is self-heating, the mechanism behind QuakeSat.
        total_i = sum(rail_currents.values())
        power_at_node[ThermalNode.BATTERY] += total_i * total_i * self.battery_r_internal_ohm

        forced = "thermal" in self._fault_start
        for n in ThermalNode:
            if forced:
                target = THERMAL_INJECTED_C
            else:
                # Nominal steady state must be AMBIENT: subtract the baseline
                # dissipation so a healthy spacecraft sits at 25 C, not above it.
                excess_w = power_at_node[n] - self._baseline_power_at_node(n)
                target = AMBIENT_C + excess_w * THERMAL_R_TH_C_PER_W
            self.node_temp_c[n] += (target - self.node_temp_c[n]) * min(1.0, dt / THERMAL_TAU_S)

    def _baseline_power_at_node(self, node: int) -> float:
        baseline = {
            ThermalNode.RADIO: RAIL_NOMINAL_DRAW_A[Rail.RADIO] * NOMINAL_VOLTAGE_V,
            ThermalNode.OBC: RAIL_NOMINAL_DRAW_A[Rail.OBC] * NOMINAL_VOLTAGE_V,
            ThermalNode.STRUCTURE: (RAIL_NOMINAL_DRAW_A[Rail.SENSORS]
                                    + RAIL_NOMINAL_DRAW_A[Rail.ADCS]
                                    + RAIL_NOMINAL_DRAW_A[Rail.PAYLOAD]) * NOMINAL_VOLTAGE_V,
            ThermalNode.BATTERY: NOMINAL_CURRENT_A ** 2 * BATTERY_R_NOMINAL_OHM,
        }
        return baseline[node]

    def step(self, dt: float) -> "tuple[RawSample, GroundTruth]":
        self.t += dt

        # A healthy link means the ground station's periodic heartbeat is
        # arriving, so contact keeps being re-established (K1). Modelling this
        # explicitly -- rather than reporting "seconds since contact" as None
        # whenever the link is up -- is what makes the scenario harness feed
        # the engine the same KIND of evidence run_simulator.py feeds it, and
        # therefore what lets a scenario exercise "link open but silent" at all.
        if self.link_healthy:
            self.last_ground_contact_t = self.t

        # --- time-varying fault effects on state -----------------------------
        if "gradual_drift" in self._fault_start:
            elapsed = self.t - self._fault_start["gradual_drift"]
            progress = min(1.0, elapsed / DRIFT_RAMP_DURATION_S)
            self.battery_r_internal_ohm = (
                BATTERY_R_NOMINAL_OHM
                + progress * (DRIFT_R_INTERNAL_FINAL_OHM - BATTERY_R_NOMINAL_OHM)
            )
        if "undervoltage" in self._fault_start:
            self._v_oc_sag = UNDERVOLTAGE_V_OC_SAG

        self.device_responsive["imu"] = "sensor_timeout" not in self._fault_start

        # --- power: derive current, then voltage from it ---------------------
        rail_currents = self._rail_currents()
        bus_current = sum(rail_currents.values())
        v_oc = BATTERY_V_OC - self._v_oc_sag
        bus_voltage = v_oc - bus_current * self.battery_r_internal_ohm

        self.battery_soc = max(
            0.0, self.battery_soc - bus_current * dt / (3600.0 * BATTERY_CAPACITY_AH)
        )

        self._update_thermal(dt, rail_currents, max(0.0, bus_voltage))

        # --- sensors ---------------------------------------------------------
        # Draw unconditionally so the RNG stream advances identically regardless
        # of which faults are active -- replay stays deterministic across
        # differing fault schedules.
        fresh_imu = self._raw_imu_sample()
        if "sensor_lockup" in self._fault_start and self._frozen_imu_values is not None:
            imu = dict(self._frozen_imu_values)
        else:
            imu = fresh_imu

        temp_noise = self.rng.gauss(0, 0.3)
        mag_noise = (self.rng.gauss(0, 1.0), self.rng.gauss(0, 1.0), self.rng.gauss(0, 1.0))
        v_noise = self.rng.gauss(0, 0.02)
        i_noise = self.rng.gauss(0, 0.02)

        reported_temp = self.node_temp_c[ThermalNode.STRUCTURE] + temp_noise
        if "thermal" in self._fault_start:
            reported_temp = THERMAL_INJECTED_C + temp_noise

        # Bus corruption: every device on the failed path returns exact zeros
        # while still ACKing. The devices are healthy; only their shared path
        # is not. Note what is NOT done here -- device_responsive is untouched,
        # because a bus that inserts zeros is not the same failure as a device
        # that stopped answering, and collapsing the two would erase the very
        # distinction this fault exists to test.
        mag_reported = (25.0 + mag_noise[0], -8.0 + mag_noise[1], 40.0 + mag_noise[2])
        if self.mag_corrupt:
            # ONE device corrupt, its bus fine. Pairs with data_bus_failure: the
            # correct diagnosis here is the device, not the path.
            mag_reported = (0.0, 0.0, 0.0)
        if not self.bus_healthy[Bus.I2C_A]:
            members = {Device.IMU, Device.MAG, Device.TEMP}
            if Device.IMU in members:
                imu = {k: 0.0 for k in imu}
            if Device.MAG in members:
                mag_reported = (0.0, 0.0, 0.0)
            if Device.TEMP in members:
                reported_temp = 0.0

        sample = RawSample(
            temp_c=reported_temp,
            accel_x=imu["accel_x"], accel_y=imu["accel_y"], accel_z=imu["accel_z"],
            gyro_x=imu["gyro_x"], gyro_y=imu["gyro_y"], gyro_z=imu["gyro_z"],
            mag_x=mag_reported[0],
            mag_y=mag_reported[1],
            mag_z=mag_reported[2],
            bus_voltage_v=bus_voltage + v_noise,
            bus_current_a=bus_current + i_noise,
            imu_responded=self.device_responsive["imu"],
            temp_responded=self.device_responsive["temp"],
            rail_current_a={int(r): rail_currents[r] for r in Rail},
            node_temp_c={int(n): self.node_temp_c[n] for n in ThermalNode},
            radio_responded=self.device_responsive["radio"],
            mag_responded=self.device_responsive["mag"],
            # K1: report elapsed-since-contact ALWAYS, not None-when-healthy.
            #
            # This used to collapse to None while the link was up, which meant
            # the environment was handing the engine a pre-decided verdict
            # ("healthy") rather than the evidence a real transport produces
            # (a heartbeat timestamp). The scenario suite therefore exercised a
            # decision path the deployed transport could not reproduce, and
            # that is precisely why J1 -- a link that is open but silent --
            # survived the whole Phase 6 campaign.
            #
            # last_ground_contact_t advances while the link is healthy, exactly
            # as run_simulator.py's last_client_seen advances on each received
            # ground packet. Same evidence, same units, same meaning.
            seconds_since_ground_contact=self.t - self.last_ground_contact_t,
        )
        truth = GroundTruth(
            t=self.t,
            active_faults=self.active_faults(),
            rail_latched={int(r): self.rail_latched[r] for r in Rail},
            battery_soc=self.battery_soc,
        )
        return sample, truth
