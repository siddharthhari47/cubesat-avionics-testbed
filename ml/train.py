"""
Trains an Isolation Forest anomaly detector on nominal-only telemetry.

Unsupervised by design: reliable fault-labeled data isn't realistically
available operationally (that's the whole reason FDIR and ground testing
exist), so the model learns "what normal looks like" and flags deviations,
rather than learning to classify labeled fault examples. The generated
dataset's fault-labeled rows exist for EVALUATION (ml/evaluate.py), not
training.

Why Isolation Forest and not a neural network: see
docs/architecture/phase0-1-engineering-decisions.md, decision 4, for the full
reasoning. In short -- inference is pure comparison-based tree traversal, no
floating-point matrix multiplication, no NN runtime dependency, trivially
portable to hand-written C (ml/export_embedded.py). n_estimators and
max_samples below are chosen with the eventual STM32 flash/RAM budget in
mind, not left at sklearn's defaults without thought.

Run: python ml/train.py [--dataset data/datasets/phase1_dataset.csv]
                         [--out ml/models/isolation_forest_v1.joblib]
"""

import argparse
import json
import platform
import time
from pathlib import Path

import joblib
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.features import build_features, feature_columns  # noqa: E402

N_ESTIMATORS = 50    # tree count -- directly bounds exported model size/flash use
MAX_SAMPLES = 256     # samples per tree -- bounds tree depth, keeps traversal shallow
CONTAMINATION = 0.01  # small: this is nominal-only training data, few true outliers expected


def train(dataset_path: Path, seed: int = 0):
    df = pd.read_csv(dataset_path)
    # Train on whole nominal EPISODES, not just nominal rows: filtering rows
    # would leave the pre-fault and post-clear chunks of a fault episode under
    # the same episode_id, and build_features' rolling window would then blend
    # across the removed fault period at that boundary -- a real (if narrow)
    # leakage bug caught by sanity-checking scores before writing this.
    ever_faulty = df.groupby("episode_id")["fault_active"].transform("any")
    nominal = df[~ever_faulty].reset_index(drop=True)

    X = build_features(nominal)
    model = IsolationForest(
        n_estimators=N_ESTIMATORS,
        max_samples=MAX_SAMPLES,
        contamination=CONTAMINATION,
        random_state=seed,
        n_jobs=-1,
    )
    model.fit(X)
    return model, X


def main():
    parser = argparse.ArgumentParser(description="Train the Isolation Forest anomaly detector")
    parser.add_argument("--dataset", type=str, default="data/datasets/phase1_dataset.csv")
    parser.add_argument("--out", type=str, default="ml/models/isolation_forest_v1.joblib")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    model, X = train(dataset_path, seed=args.seed)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_path)

    avg_nodes = sum(est.tree_.node_count for est in model.estimators_) / len(model.estimators_)
    metadata = {
        "feature_columns": feature_columns(),
        "n_estimators": N_ESTIMATORS,
        "max_samples": MAX_SAMPLES,
        "contamination": CONTAMINATION,
        "training_dataset": str(dataset_path),
        "training_row_count": len(X),
        "seed": args.seed,
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
        "trained_at_unix_s": time.time(),
        "avg_tree_node_count": avg_nodes,
        "status": "Trained -- evaluated on synthetic simulator data only, not hardware-tested",
    }
    metadata_path = out_path.with_name(out_path.stem + "_metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2))

    print(f"trained on {len(X)} nominal samples, {len(feature_columns())} features")
    print(f"model: {out_path} ({N_ESTIMATORS} trees, avg {avg_nodes:.0f} nodes/tree)")
    print(f"metadata: {metadata_path}")


if __name__ == "__main__":
    main()
