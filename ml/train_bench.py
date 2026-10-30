"""
Fits ML #1's Isolation Forest to real nominal telemetry from the bench board.

isolation_forest_v1 learned a simulated 5 V spacecraft, so on this 3.3 V board
with a dead magnetometer it flags every sample. That is a mismatch in what
"normal" means, not a detection. This fits the same model to a stretch of real
nominal data and writes a SEPARATE file; v1 and its synthetic evaluation stay
untouched.

Evidence limit: one board, one bench, one session, at rest. A model fitted to
this learns "this board sitting still today", so any motion or warm-up may score
as anomalous. It is advisory either way.

Run: python ml/train_bench.py <capture.csv> --from-ms 50000 --to-ms 140000
"""

import argparse
import json
import sys
import time
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import IsolationForest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.features import build_features, feature_columns  # noqa: E402
from ml.train import CONTAMINATION, MAX_SAMPLES, N_ESTIMATORS  # noqa: E402

HEALTH_TEMP_OK = 1
HEALTH_IMU_OK = 2


def main():
    parser = argparse.ArgumentParser(description="Fit ML #1 to real bench nominal data")
    parser.add_argument("csv")
    parser.add_argument("--from-ms", type=int, required=True)
    parser.add_argument("--to-ms", type=int, required=True)
    parser.add_argument("--out", default="ml/models/isolation_forest_bench.joblib")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    df = df[df.timestamp_ms.between(args.from_ms, args.to_ms)].reset_index(drop=True)
    df["imu_responded"] = (df.health_flags & HEALTH_IMU_OK) != 0
    df["temp_responded"] = (df.health_flags & HEALTH_TEMP_OK) != 0
    if not (df.imu_responded.all() and df.temp_responded.all()):
        raise SystemExit("training window contains a sensor dropout; choose a clean one")
    df["episode_id"] = 0

    X = build_features(df)
    model = IsolationForest(n_estimators=N_ESTIMATORS, max_samples=MAX_SAMPLES,
                            contamination=CONTAMINATION, random_state=args.seed, n_jobs=-1)
    model.fit(X)

    out = Path(args.out)
    joblib.dump(model, out)
    out.with_name(out.stem + "_metadata.json").write_text(json.dumps({
        "feature_columns": feature_columns(),
        "training_capture": str(args.csv),
        "training_window_ms": [args.from_ms, args.to_ms],
        "training_row_count": len(X),
        "seed": args.seed,
        "trained_at_unix_s": time.time(),
        "status": "Fitted to one board at rest in one session. Advisory only.",
    }, indent=2))
    print(f"fitted on {len(X)} real nominal samples: {out}")


if __name__ == "__main__":
    main()
