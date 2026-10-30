"""
Real telemetry from the NUCLEO over USB serial, through the existing FDIR engine
and ML #1 on the PC.

This is V1 hardware feeding V0 software, and the boundary matters: the board
measures and transmits, the PC decides. Nothing here runs on the STM32, so a
latency it reports is firmware-clock time for the PC's engine to latch a flag,
not a claim about onboard FDIR, which is V2.

The engine is driven by the firmware's timestamp_ms rather than the PC clock, so
USB and Windows scheduling delay cannot leak into a measured latency.

Bench differences from fdir/config.py are applied in bench_profile() and listed
there. They are stated rather than edited into config.py because they describe
this breadboard, not the design.

    python ground-station/serial_bridge.py capture --port COM5 --seconds 120
    python ground-station/serial_bridge.py replay data/bench/<file>.csv
"""

import argparse
import csv
import sys
import time
from dataclasses import fields
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "simulator"))
import protocol as proto  # noqa: E402
import fdir.config as cfg  # noqa: E402
from fdir.engine import FDIREngine, MLAdvisory  # noqa: E402
from icd import Device, FaultFlag, HealthFlag, RawSample  # noqa: E402

DEFAULT_MODEL = ROOT / "ml" / "models" / "isolation_forest_v1.joblib"
CSV_DIR = ROOT / "data" / "bench"
TM_FIELDS = [f.name for f in fields(proto.TelemetryPacket)]


def bench_profile():
    """
    The bus here is the Nucleo's 3.3 V rail, and fdir/config.py describes a 5 V
    bus: unchanged, the first sample latches UNDERVOLTAGE_CRITICAL and SAFEs a
    healthy board. Same ratios as the 5 V design (90% warn, 80% critical).
    """
    cfg.NOMINAL_VOLTAGE_V = 3.3
    cfg.UNDERVOLTAGE_WARNING_V = 2.97
    cfg.UNDERVOLTAGE_CRITICAL_V = 2.64


class BenchEngine(FDIREngine):
    """
    The flight engine with the magnetometer declared not fitted.

    The AK09916 does not answer on this board, so its channels are exact zeros.
    Left in, every IMU fault becomes "two devices on one bus went bad together"
    and latches a false DATA_PATH_SUSPECT. The zeros still reach the engine and
    the model unaltered; only this one conclusion is withheld.
    """

    def __init__(self, mag_fitted=False):
        super().__init__()
        self.mag_fitted = mag_fitted

    def _suspect_devices(self, sample):
        suspect = super()._suspect_devices(sample)
        return suspect if self.mag_fitted else suspect - {Device.MAG}


def to_raw_sample(p):
    """The one place a wire packet becomes what the engine is allowed to see."""
    return RawSample(
        temp_c=p.temp_c, accel_x=p.accel_x, accel_y=p.accel_y, accel_z=p.accel_z,
        gyro_x=p.gyro_x, gyro_y=p.gyro_y, gyro_z=p.gyro_z,
        mag_x=p.mag_x, mag_y=p.mag_y, mag_z=p.mag_z,
        bus_voltage_v=p.bus_voltage_v, bus_current_a=p.bus_current_a,
        imu_responded=bool(p.health_flags & HealthFlag.IMU_OK),
        temp_responded=bool(p.health_flags & HealthFlag.TEMP_OK),
        mag_responded=bool(p.health_flags & HealthFlag.MAG_OK),
    )


class SerialSocket:
    """Gives pyserial the recv() that protocol.read_packet expects, so the framer is reused."""

    def __init__(self, ser):
        self.ser = ser

    def recv(self, n):
        while True:
            data = self.ser.read(n)
            if data:
                return data


class Ml:
    """ML #1 through the same streaming path the simulator uses. Advisory only."""

    def __init__(self, model_path):
        import warnings
        import joblib
        from ml.features import feature_columns
        from ml.streaming import StreamingFeatureExtractor
        self.model = joblib.load(model_path)
        expected = list(getattr(self.model, "feature_names_in_", []))
        if expected and expected != list(feature_columns()):
            raise ValueError("feature order does not match the trained model")
        warnings.filterwarnings("ignore", message="X does not have valid feature names",
                                category=UserWarning)
        self.extractor = StreamingFeatureExtractor()

    def advise(self, sample):
        vector = self.extractor.push(sample)
        if vector is None:
            return None
        x = np.asarray([vector])
        return MLAdvisory(score=float(self.model.decision_function(x)[0]),
                          is_anomalous=bool(self.model.predict(x)[0] == -1))


class Monitor:
    """Runs packets through FDIR + ML and measures each IMU dropout."""

    def __init__(self, ml=None, mag_fitted=False, verbose=True):
        self.engine = BenchEngine(mag_fitted)
        self.ml = ml
        self.verbose = verbose
        self.n = 0
        self.lost = 0
        self.last_seq = None
        self.last_ts = None
        self.periods = []
        self.ml_scored = 0
        self.ml_flagged = 0
        self.ml_flagged_healthy = 0
        self.ml_scored_healthy = 0
        self.log_seen = 0
        self.imu_ok_prev = None
        self.last_good_ms = None
        self.first_bad_ms = None
        self.ml_seen_this_trial = False
        self.good_run = 0
        self.trials = []

    def feed(self, p):
        self.n += 1
        if self.last_seq is not None:
            gap = (p.seq_num - self.last_seq - 1) % 65536
            if 0 < gap < 1000:
                self.lost += gap
            self.periods.append(p.timestamp_ms - self.last_ts)
        self.last_seq, self.last_ts = p.seq_num, p.timestamp_ms

        now = p.timestamp_ms / 1000.0
        sample = to_raw_sample(p)
        advisory = self.ml.advise(sample) if self.ml else None
        if advisory is not None:
            self.ml_scored += 1
            self.ml_flagged += advisory.is_anomalous
            if sample.imu_responded and sample.temp_responded:
                self.ml_scored_healthy += 1
                self.ml_flagged_healthy += advisory.is_anomalous

        before = self.engine.fault_flags
        self.engine.tick(sample, now, ml_advisory=advisory)
        # No executor on the bench: board recovery is V2. Proposals are drained
        # and shown, never acted on.
        for intent in self.engine.take_pending_intents():
            self._say(now, f"recovery proposed, NOT executed: {intent}")
        self._track_trial(p, before)

        for t, message in self.engine.log[self.log_seen:]:
            self._say(t, message)
        self.log_seen = len(self.engine.log)

    def _track_trial(self, p, flags_before):
        imu_ok = bool(p.health_flags & HealthFlag.IMU_OK)
        if imu_ok:
            if self.first_bad_ms is None:
                self.last_good_ms = p.timestamp_ms
            self.good_run += 1
        else:
            self.good_run = 0
            if self.first_bad_ms is None:
                self.first_bad_ms = p.timestamp_ms
                self.ml_seen_this_trial = False

        latched_now = (self.engine.fault_flags & FaultFlag.SENSOR_TIMEOUT
                       and not flags_before & FaultFlag.SENSOR_TIMEOUT)
        if latched_now and self.first_bad_ms is not None and self.last_good_ms is not None:
            trial = {"first_bad_ms": self.first_bad_ms, "flag_ms": p.timestamp_ms,
                     "min_ms": p.timestamp_ms - self.first_bad_ms,
                     "max_ms": p.timestamp_ms - self.last_good_ms,
                     "mode": self.engine.mode.name,
                     "flags": repr(FaultFlag(self.engine.fault_flags))}
            self.trials.append(trial)
            print(f"  TRIAL {len(self.trials)}: IMU loss to SENSOR_TIMEOUT between "
                  f"{trial['min_ms']} and {trial['max_ms']} ms, mode {trial['mode']}",
                  flush=True)
        if self.first_bad_ms is not None and self.good_run >= cfg.RESET_EVIDENCE_SAMPLES:
            cleared, still = self.engine.reset_faults(p.timestamp_ms / 1000.0)
            if not still & FaultFlag.SENSOR_TIMEOUT:
                self.first_bad_ms = None
                self.last_good_ms = p.timestamp_ms

    def _say(self, t, message):
        if self.verbose:
            print(f"  [{t:8.2f}s] {message}", flush=True)

    def report(self):
        print(f"\npackets {self.n}, lost by seq gap {self.lost}")
        if self.periods:
            a = np.asarray(self.periods, dtype=float)
            print(f"period ms: mean {a.mean():.2f}, std {a.std():.2f}, "
                  f"min {a.min():.0f}, max {a.max():.0f}")
        print(f"final mode {self.engine.mode.name}, flags {FaultFlag(self.engine.fault_flags)!r}")
        print(f"diagnosis {self.engine.diagnosis.cause.name}")
        if self.ml_scored:
            print(f"ML flagged {self.ml_flagged}/{self.ml_scored} scored samples; "
                  f"on healthy-sensor samples {self.ml_flagged_healthy}/{self.ml_scored_healthy}")
        for i, t in enumerate(self.trials, 1):
            print(f"trial {i}: {t['min_ms']}..{t['max_ms']} ms, mode {t['mode']}, {t['flags']}")


def capture(args):
    import serial
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    path = CSV_DIR / f"{args.label}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    ser = serial.Serial(args.port, 115200, timeout=0.2)
    sock = SerialSocket(ser)
    ml = Ml(args.model)
    monitor = Monitor(ml, args.mag_fitted)
    resyncs = 0
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["host_time_s"] + TM_FIELDS)
        print(f"listening on {args.port}, writing {path}", flush=True)
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            packet, corrupted = proto.read_packet(sock)
            # Everything before the first packet is the boot text report, so a
            # resync there is expected and is not link corruption.
            if corrupted and monitor.n > 0:
                resyncs += 1
            if not isinstance(packet, proto.TelemetryPacket):
                continue
            writer.writerow([f"{time.monotonic():.4f}"] + [getattr(packet, n) for n in TM_FIELDS])
            f.flush()
            monitor.feed(packet)
    ser.close()
    monitor.report()
    print(f"CRC/framing resyncs after first packet: {resyncs}")
    print(f"log: {path}")


def replay(args):
    ml = Ml(args.model) if not args.no_ml else None
    monitor = Monitor(ml, args.mag_fitted, verbose=not args.quiet)
    with open(args.csv, newline="") as f:
        for row in csv.DictReader(f):
            values = {}
            for name in TM_FIELDS:
                kind = float if name in ("temp_c", "bus_voltage_v", "bus_current_a") \
                    or name[:5] in ("accel", "gyro_") or name[:4] == "mag_" else int
                values[name] = kind(row[name])
            monitor.feed(proto.TelemetryPacket(**values))
    monitor.report()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--model", default=str(DEFAULT_MODEL))
        p.add_argument("--mag-fitted", action="store_true")
        if name == "capture":
            p.add_argument("--port", default="COM5")
            p.add_argument("--seconds", type=float, default=120.0)
            p.add_argument("--label", default="bench")
            p.set_defaults(func=capture)
        else:
            p.add_argument("csv")
            p.add_argument("--no-ml", action="store_true")
            p.add_argument("--quiet", action="store_true")
            p.set_defaults(func=replay)
    args = parser.parse_args()
    bench_profile()
    args.func(args)


if __name__ == "__main__":
    main()
