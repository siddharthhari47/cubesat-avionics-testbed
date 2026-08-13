"""
Interface Control Document types — the shared vocabulary of the whole system.

WHY THIS PACKAGE EXISTS (D5, and the circular import it was half of):

These types used to live in `simulator/protocol.py`, which meant:

  * `fdir/engine.py` — the module whose entire premise is being hardware-agnostic
    and portable to firmware — had to `sys.path.insert` into `simulator/` to get
    `FaultFlag`. The FDIR package could not be lifted out, vendored, or imported
    without the simulator directory present.
  * `simulator/environment.py` imported `RawSample` from `fdir.engine`, while
    `fdir/engine.py` imported from `simulator/protocol.py` — a genuine import
    cycle that only worked because the two halves resolved through different
    sys.path roots.
  * Worst of all, that trick produced **two distinct FaultFlag classes at
    runtime**. Verified before this refactor:
        protocol.FaultFlag is simulator.protocol.FaultFlag  -> False
        isinstance(engine_flag, simulator.protocol.FaultFlag) -> False
    It worked only because IntFlag compares by value. Any isinstance, match, or
    pickle across that seam would have failed silently.

These are domain/ICD types, not simulation types. They belong below both the
simulator and the FDIR engine, depending on neither. Everything here is pure
data: no I/O, no sockets, no numpy, no simulation. A firmware-side port maps
these directly onto the C definitions in firmware/inc/telemetry_protocol.h.

Wire encoding (struct formats, packing, CRC) deliberately stays in
simulator/protocol.py — that is transport, not vocabulary.
"""

import math
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Dict, Optional, Set

__all__ = ["Mode", "FaultFlag", "HealthFlag", "RawSample", "Rail", "ThermalNode",
           "Device", "Bus", "BUS_MEMBERS"]


class Rail(IntEnum):
    """
    Switchable power rails. IntEnum so it maps to a uint8 device id in C.

    Per-rail current sensing and independently switchable loads is the one
    hardware capability the failure research put a purchase deadline on: the
    case study attributes KySat-2's loss to its absence, and the
    radio_latchup / radio_unresponsive discrimination pair exists specifically
    to measure what it buys before the board is bought.
    """

    OBC = 0
    RADIO = 1
    SENSORS = 2
    ADCS = 3
    PAYLOAD = 4


class ThermalNode(IntEnum):
    """Lumped thermal masses. Separate nodes are what let a local heat source
    (a latched-up radio) be distinguished from a genuine spacecraft-wide
    thermal event, and from a drifting temperature sensor."""

    BATTERY = 0
    RADIO = 1
    OBC = 2
    STRUCTURE = 3


class Device(IntEnum):
    """Addressable sensors/peripherals, distinct from the RAIL that powers them.
    A device can be healthy while its power rail is fine and its DATA BUS is
    not -- keeping the two vocabularies separate is what makes that statement
    expressible at all."""

    IMU = 0
    MAG = 1
    TEMP = 2
    RADIO = 3


class Bus(IntEnum):
    """Shared data paths."""

    I2C_A = 0
    SPI_B = 1


# Which devices sit on which bus. This is spacecraft topology, not simulation
# detail: the diagnosis layer needs it to tell "three sensors failed at once"
# from "the one bus they share failed", which is the Delfi-C3 distinction and
# the only genuine diagnostic ambiguity the failure research turned up.
BUS_MEMBERS = {
    Bus.I2C_A: (Device.IMU, Device.MAG, Device.TEMP),
    Bus.SPI_B: (),
}


def bus_of(device: "Device"):
    for bus, members in BUS_MEMBERS.items():
        if device in members:
            return bus
    return None


class Mode(IntEnum):
    """Flight-computer operating mode. Wire-encoded as uint8."""

    BOOT = 0
    NOMINAL = 1
    SAFE = 2
    TEST = 3


class FaultFlag(IntFlag):
    """
    Latched fault conditions. Wire-encoded as an unsigned bitfield.

    Bits 0-9 are allocated; the remainder are reserved. See
    docs/interfaces/telemetry-dictionary.md for the authoritative mapping and
    fdir/engine.py's SAFE_MODE_TRIGGER_FLAGS for which of these carry
    autonomous mode-change authority (deliberately, only a minority do).
    """

    NONE = 0
    SENSOR_TIMEOUT = 1 << 0
    UNDERVOLTAGE_WARNING = 1 << 1
    UNDERVOLTAGE_CRITICAL = 1 << 2
    COMMS_LOSS = 1 << 3
    CORRUPTED_PACKET = 1 << 4
    ADAPTIVE_ANOMALY = 1 << 5
    ML_ANOMALY = 1 << 6
    WATCHDOG_RESET = 1 << 7
    THERMAL_ANOMALY = 1 << 8
    SENSOR_LOCKUP = 1 << 9
    # Set when an escalating recovery campaign has exhausted every rung
    # without its verification condition being met. Autonomy stands down;
    # the spacecraft holds and waits for the ground. Uses bit 10, which
    # only exists because Phase 1b widened this field past uint16.
    RECOVERY_FAILED = 1 << 10
    # Two or more devices sharing one bus went invalid together. The bus is
    # a better explanation than simultaneous independent device failures --
    # Delfi-C3, where protective responses fired against subsystems that
    # were themselves fine.
    DATA_PATH_SUSPECT = 1 << 11
    # Something is wrong and no enumerated diagnosis matches. The spacecraft
    # is required to be able to say "I do not know" rather than invent a
    # cause: 63% of the failure record has no identifiable cause at all.
    UNKNOWN_ANOMALY = 1 << 12
    # A sensor channel returned a value that is not physically representable
    # (NaN or infinity) -- the reading carries no information at all.
    #
    # This exists because of a defect found in the V0 adversarial safety review
    # (F3): every comparison with NaN is False in both Python and C, so a NaN
    # bus voltage silently passed `v < UNDERVOLTAGE_CRITICAL_V` and was recorded
    # as POSITIVE EVIDENCE that a latched undervoltage had cleared. A vehicle
    # correctly held in SAFE could be returned to service on readings that meant
    # nothing. Making "this channel is lying" a first-class observable is the
    # fix; silently trusting it was the bug.
    #
    # Carries NEITHER SAFE-mode nor recovery authority: an invalid reading says
    # the sensor is untrustworthy, not that the spacecraft is in danger, and
    # inventing a specific fault from a broken channel is the wrong-diagnosis
    # failure this project exists to avoid (R10).
    SENSOR_INVALID = 1 << 13
    # One device is returning finite but physically impossible readings while
    # its bus is fine. Distinct from SENSOR_INVALID (not a number at all) and
    # from DATA_PATH_SUSPECT (the shared path, not the device). This is the
    # single-device partner to the Delfi-C3 case: two devices failing together
    # is better explained by their bus, but ONE device failing alone genuinely
    # is a device fault, and until now nothing latched for it at all.
    SENSOR_IMPLAUSIBLE = 1 << 14
    # A switchable rail is drawing above its ceiling. KySat-2 lost its battery
    # to exactly this while every fixed VOLTAGE threshold stayed happy -- the
    # current is where the fault is visible, and the per-rail sensing that
    # makes it visible is the one hardware capability the failure research put
    # a purchase deadline on.
    RAIL_OVERCURRENT = 1 << 15


class HealthFlag(IntFlag):
    """Per-subsystem health. A SET bit means healthy/responding."""

    NONE = 0
    TEMP_OK = 1 << 0
    IMU_OK = 1 << 1
    MAG_OK = 1 << 2
    POWER_OK = 1 << 3
    ALL_OK = TEMP_OK | IMU_OK | MAG_OK | POWER_OK


@dataclass
class RawSample:
    """
    One acquisition cycle's worth of physical observables — the contract every
    sensor source must satisfy, simulated or real.

    This is deliberately the ONLY thing the FDIR engine is allowed to see. It
    contains no simulation ground truth (no "is fault X injected" flag), because
    a detector that can read the test harness's answer key cannot be evaluated
    against it. `imu_responded` / `temp_responded` model whether the device
    actually produced a fresh reading this cycle — a real, physically observable
    I2C/SPI outcome (an ACK or its absence), not a label.

    It lives here rather than in fdir/engine.py because a future hardware driver
    must be able to construct one without importing the decision engine.
    """

    temp_c: float
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    mag_x: float
    mag_y: float
    mag_z: float
    bus_voltage_v: float
    bus_current_a: float
    imu_responded: bool = True
    temp_responded: bool = True

    # --- extended observables (Phase 2) --------------------------------------
    # Defaulted so every existing construction site keeps working unchanged.
    #
    # These are deliberately NOT on the wire yet. Adding per-rail current to
    # the telemetry packet is an ICD revision with a firmware and ground-station
    # cost, and the decision of whether it earns its bytes should be made from
    # the measurement the discrimination-pair scenarios produce, not before it.
    # Until then they are available to detectors running onboard, which is
    # exactly where a latch-up detector would live anyway.
    rail_current_a: Optional[Dict[int, float]] = None
    node_temp_c: Optional[Dict[int, float]] = None
    radio_responded: bool = True
    mag_responded: bool = True
    seconds_since_ground_contact: Optional[float] = None

    # --- validity (V0 safety review, F3/F4) ----------------------------------
    # Deliberately reported rather than raised. Flight software does not get to
    # crash on a bad sensor read; it has to keep flying and say so. Construction
    # therefore never fails, and every detector asks these questions itself.

    def invalid_devices(self) -> Set["Device"]:
        """
        Devices whose readings this cycle are not physically representable.
        NaN and +/-inf both qualify: neither carries information, and both
        silently satisfy or defeat every threshold comparison written about
        them, depending only on which way the predicate happens to be phrased.
        """
        bad: Set[Device] = set()
        if not _all_finite(self.accel_x, self.accel_y, self.accel_z,
                           self.gyro_x, self.gyro_y, self.gyro_z):
            bad.add(Device.IMU)
        if not _all_finite(self.mag_x, self.mag_y, self.mag_z):
            bad.add(Device.MAG)
        if not _all_finite(self.temp_c):
            bad.add(Device.TEMP)
        return bad

    @property
    def power_valid(self) -> bool:
        """Are the power observables usable? Voltage drives SAFE-mode authority,
        so this is the one whose absence must never read as 'nominal'."""
        return _all_finite(self.bus_voltage_v, self.bus_current_a)


def _all_finite(*values) -> bool:
    """
    True only if every value is a real, finite number.

    Booleans are rejected on purpose: `isinstance(True, int)` is True in Python,
    and a bool arriving where a voltage belongs is a wiring mistake worth
    catching rather than silently coercing to 1.0.
    """
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(v):
            return False
    return True
