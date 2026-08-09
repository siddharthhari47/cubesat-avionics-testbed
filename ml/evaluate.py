"""
Evaluates the trained Isolation Forest against held-out labeled episodes, and
compares it directly against the already-built deterministic FDIR engine
(fdir/engine.py) on the SAME data. That comparison is the actual point: it
answers "what does the ML layer catch that the deterministic thresholds
don't, and vice versa" -- see docs/requirements/SRS.md's FDIR-007, which
frames the ML layer as adding value on top of deterministic detection, not
replacing it.

Both detectors see exactly the same RawSample stream. Neither sees ground
truth while "deciding" -- ground truth is used only afterward, to score them.

Status: Simulated + Trained. Not evaluated on real hardware or real sensor
data. Every number in the generated report is a property of this synthetic
environment and this specific trained model, not a measured physical fact.

Run: python ml/evaluate.py
"""

import json
import sys
import time
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.features import build_features  # noqa: E402
from fdir.engine import FDIREngine, RawSample  # noqa: E402
from simulator.protocol import FaultFlag  # noqa: E402
from simulator.dataset_gen import generate_dataset, FAULT_ONSET_S, FAULT_CLEAR_S  # noqa: E402

HELD_OUT_SEED = 1  # different from training seed (0) -- genuinely unseen episodes
MODEL_PATH = Path("ml/models/isolation_forest_v1.joblib")
REPORT_PATH = Path("docs/architecture/ml-evaluation-report.md")
PLOT_PATH = Path("ml/reports/score_distributions.png")

FAULT_TO_EXPECTED_FLAG = {
    "undervoltage": FaultFlag.UNDERVOLTAGE_CRITICAL,
    "thermal": FaultFlag.THERMAL_ANOMALY,
    "sensor_lockup": FaultFlag.SENSOR_LOCKUP,
    "sensor_timeout": FaultFlag.SENSOR_TIMEOUT,
    "gradual_drift": FaultFlag.ADAPTIVE_ANOMALY,
}


def row_to_raw_sample(row) -> RawSample:
    return RawSample(
        temp_c=row.temp_c, accel_x=row.accel_x, accel_y=row.accel_y, accel_z=row.accel_z,
        gyro_x=row.gyro_x, gyro_y=row.gyro_y, gyro_z=row.gyro_z,
        mag_x=row.mag_x, mag_y=row.mag_y, mag_z=row.mag_z,
        bus_voltage_v=row.bus_voltage_v, bus_current_a=row.bus_current_a,
        imu_responded=bool(row.imu_responded), temp_responded=bool(row.temp_responded),
    )


def run_fdir_over_episode(episode_df: pd.DataFrame, expected_flag: FaultFlag):
    """Returns a list of (t, fault_flags_int) for every row, fresh engine per episode."""
    engine = FDIREngine()
    out = []
    for row in episode_df.itertuples():
        sample = row_to_raw_sample(row)
        engine.tick(sample, row.t)
        out.append((row.t, int(engine.fault_flags)))
    return out


def first_detection_time(rows, onset_s, window_end_s, is_hit_fn):
    for t, val in rows:
        if t < onset_s or t > window_end_s:
            continue
        if is_hit_fn(val):
            return t
    return None


def evaluate_fault_type(fault_type, episodes, model):
    expected_flag = FAULT_TO_EXPECTED_FLAG[fault_type]
    window_end = 60.0 if fault_type == "gradual_drift" else FAULT_CLEAR_S

    fdir_latencies, ml_latencies = [], []
    fdir_detected, ml_detected = 0, 0

    for ep_df in episodes:
        fdir_rows = run_fdir_over_episode(ep_df, expected_flag)
        t_fdir = first_detection_time(fdir_rows, FAULT_ONSET_S, window_end,
                                       lambda flags: flags & int(expected_flag))
        if t_fdir is not None:
            fdir_detected += 1
            fdir_latencies.append(t_fdir - FAULT_ONSET_S)

        feats = build_features(ep_df)
        preds = model.predict(feats)  # -1 = anomalous, 1 = normal
        ml_rows = list(zip(ep_df["t"].tolist(), preds.tolist()))
        t_ml = first_detection_time(ml_rows, FAULT_ONSET_S, window_end, lambda p: p == -1)
        if t_ml is not None:
            ml_detected += 1
            ml_latencies.append(t_ml - FAULT_ONSET_S)

    n = len(episodes)
    return {
        "fault_type": fault_type,
        "episodes": n,
        "fdir_recall": fdir_detected / n if n else None,
        "fdir_mean_latency_s": sum(fdir_latencies) / len(fdir_latencies) if fdir_latencies else None,
        "ml_recall": ml_detected / n if n else None,
        "ml_mean_latency_s": sum(ml_latencies) / len(ml_latencies) if ml_latencies else None,
    }


def evaluate_false_positives(nominal_episodes, model):
    # Episode-level ("did this episode have >=1 false alarm") and row-level
    # ("what fraction of individual samples were flagged") are both reported
    # because they answer different questions and can look very different:
    # with contamination=0.01 and ~600 rows/episode, ANY nonzero per-row rate
    # will make the episode-level rate approach 100% just from episode length
    # -- that's an artifact of episode length, not evidence the detector is
    # bad, and reporting only the episode-level number would be misleading.
    fdir_false_episodes = 0
    ml_false_episodes = 0
    total_hours = 0.0
    total_rows = 0
    ml_false_rows = 0
    fdir_false_rows = 0
    for ep_df in nominal_episodes:
        engine = FDIREngine()
        any_fdir_flag = False
        for row in ep_df.itertuples():
            sample = row_to_raw_sample(row)
            engine.tick(sample, row.t)
            if engine.fault_flags:
                any_fdir_flag = True
                fdir_false_rows += 1
        if any_fdir_flag:
            fdir_false_episodes += 1

        feats = build_features(ep_df)
        preds = model.predict(feats)
        n_flagged = int((preds == -1).sum())
        ml_false_rows += n_flagged
        if n_flagged > 0:
            ml_false_episodes += 1

        total_rows += len(ep_df)
        total_hours += (ep_df["t"].max() - ep_df["t"].min()) / 3600.0

    n = len(nominal_episodes)
    return {
        "nominal_episodes": n,
        "nominal_hours": total_hours,
        "fdir_false_episode_rate": fdir_false_episodes / n if n else None,
        "fdir_false_per_hour": fdir_false_episodes / total_hours if total_hours else None,
        "fdir_false_row_rate": fdir_false_rows / total_rows if total_rows else None,
        "ml_false_episode_rate": ml_false_episodes / n if n else None,
        "ml_false_per_hour": ml_false_episodes / total_hours if total_hours else None,
        "ml_false_row_rate": ml_false_rows / total_rows if total_rows else None,
    }


def main():
    model = joblib.load(MODEL_PATH)
    metadata = json.loads(MODEL_PATH.with_name(MODEL_PATH.stem + "_metadata.json").read_text())

    print("generating held-out dataset (seed=%d, disjoint from training seed 0)..." % HELD_OUT_SEED)
    df, manifest = generate_dataset(HELD_OUT_SEED, episode_duration_s=60.0)

    episodes_by_type = {}
    for ftype in list(FAULT_TO_EXPECTED_FLAG) + ["none"]:
        ids = df[df["episode_id"].isin(
            df.groupby("episode_id")["fault_type"].apply(lambda s: (s == ftype).any() if ftype != "none" else (s == "none").all())
            .pipe(lambda s: s[s].index)
        )]["episode_id"].unique()
        episodes_by_type[ftype] = [df[df["episode_id"] == i].reset_index(drop=True) for i in ids]

    fault_results = []
    for ftype in FAULT_TO_EXPECTED_FLAG:
        r = evaluate_fault_type(ftype, episodes_by_type[ftype], model)
        fault_results.append(r)
        print(f"{ftype}: FDIR recall={r['fdir_recall']:.2f} lat={r['fdir_mean_latency_s']}  "
              f"ML recall={r['ml_recall']:.2f} lat={r['ml_mean_latency_s']}")

    fp = evaluate_false_positives(episodes_by_type["none"], model)
    print("false positives (nominal):", fp)

    # nominal vs faulty score distributions, for the report's plot
    nominal_scores = model.decision_function(build_features(episodes_by_type["none"][0]))
    fig, ax = plt.subplots(figsize=(7, 4))
    for ftype in FAULT_TO_EXPECTED_FLAG:
        ep = episodes_by_type[ftype][0]
        scores = model.decision_function(build_features(ep))
        active = ep["fault_active"].values
        if active.any():
            ax.hist(scores[active], bins=20, alpha=0.5, label=f"{ftype} (active)", density=True)
    ax.hist(nominal_scores, bins=20, alpha=0.6, label="nominal", density=True, color="black")
    ax.set_xlabel("Isolation Forest decision_function score (lower = more anomalous)")
    ax.set_ylabel("density")
    ax.set_title("Score distributions: nominal vs. one example episode per fault type")
    ax.legend(fontsize=8)
    PLOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(PLOT_PATH, dpi=120)

    write_report(fault_results, fp, metadata)
    print(f"\nreport written to {REPORT_PATH}")
    print(f"plot written to {PLOT_PATH}")


def write_report(fault_results, fp, metadata):
    avg_nodes = metadata["avg_tree_node_count"]
    n_trees = metadata["n_estimators"]
    approx_bytes = n_trees * avg_nodes * 16  # ~16 bytes/node: feature idx + threshold + 2 child indices

    lines = []
    lines.append("# ML Anomaly Detection -- Evaluation Report")
    lines.append("")
    lines.append("**Status: Simulated + Trained.** Every number below comes from this synthetic "
                  "simulator and this specific trained model. Nothing here has been run against "
                  "real hardware or real sensor data, and none of it should be read as a claim "
                  "about real-world detection performance.")
    lines.append("")
    lines.append(f"Model: `{MODEL_PATH}` -- Isolation Forest, {n_trees} trees, "
                  f"trained on {metadata['training_row_count']} nominal samples "
                  f"(seed {metadata['seed']}). Evaluated on a held-out dataset generated with "
                  f"seed {HELD_OUT_SEED} -- disjoint episodes, never seen during training.")
    lines.append("")
    lines.append("## Per-fault-type detection: FDIR (deterministic) vs. ML (Isolation Forest)")
    lines.append("")
    lines.append("No single blended \"accuracy\" number is reported here on purpose -- it would "
                  "hide exactly the differences that matter. FDIR \"detection\" means the fault's "
                  "mapped `FaultFlag` bit latched (not necessarily that SAFE mode was entered -- "
                  "`sensor_timeout` and `gradual_drift` are flag-only by design, see "
                  "`fdir/engine.py`'s `SAFE_MODE_TRIGGER_FLAGS`). ML \"detection\" means "
                  "`IsolationForest.predict() == -1` (sklearn's own contamination-derived "
                  "threshold, not a hand-picked cutoff).")
    lines.append("")
    lines.append("| Fault type | Episodes | FDIR recall | FDIR mean latency (s) | ML recall | ML mean latency (s) |")
    lines.append("|---|---|---|---|---|---|")
    for r in fault_results:
        def fmt(x):
            return f"{x:.2f}" if isinstance(x, float) else "n/a"
        lines.append(f"| {r['fault_type']} | {r['episodes']} | {fmt(r['fdir_recall'])} | "
                      f"{fmt(r['fdir_mean_latency_s'])} | {fmt(r['ml_recall'])} | {fmt(r['ml_mean_latency_s'])} |")
    lines.append("")
    drift_row = next((r for r in fault_results if r["fault_type"] == "gradual_drift"), None)
    if drift_row and drift_row["fdir_recall"] is not None and drift_row["fdir_recall"] < 0.5 and drift_row["ml_recall"] and drift_row["ml_recall"] > 0.5:
        lines.append("## Notable finding: gradual_drift")
        lines.append("")
        lines.append(f"FDIR's adaptive baseline (`FDIR-006`, an EWMA over `bus_voltage_v`) recalled "
                      f"**{drift_row['fdir_recall']:.0%}** of `gradual_drift` episodes; the trained "
                      f"Isolation Forest recalled **{drift_row['ml_recall']:.0%}**. This is not a "
                      "bug in the EWMA detector -- it is doing exactly what an online-adaptive "
                      "statistic is supposed to do, continuously updating its notion of \"normal\" "
                      "toward the current signal. That is precisely what makes it structurally "
                      "unable to catch a *slow* drift: each sample-to-sample change is too small "
                      "to ever exceed the deviation threshold, so the baseline just tracks the "
                      "drift as the new normal instead of flagging it. The Isolation Forest, "
                      "trained once on a fixed nominal reference and never updated afterward, has "
                      "no such blind spot -- it still measures every new sample against the "
                      "original training distribution. This is the concrete, measured version of "
                      "the argument for adding a trained ML layer on top of adaptive statistics in "
                      "the first place (see `docs/requirements/SRS.md`'s `FDIR-007` and "
                      "`docs/architecture/phase0-1-engineering-decisions.md`, decision 4) -- not a "
                      "hypothetical benefit, a specific failure mode this evaluation reproduced and "
                      "measured.")
        lines.append("")

    lines.append("## False positive rate (nominal episodes only)")
    lines.append("")
    lines.append(f"Measured on {fp['nominal_episodes']} held-out nominal episodes "
                  f"({fp['nominal_hours']:.2f} simulated hours total, {fp['nominal_episodes']} episodes "
                  "x 600 samples/episode).")
    lines.append("")
    lines.append("| Detector | Episodes with >=1 false alarm | False alarms/hour (episode-level) | False-flagged samples (row-level) |")
    lines.append("|---|---|---|---|")
    lines.append(f"| FDIR (any fault flag) | {fp['fdir_false_episode_rate']:.2%} | {fp['fdir_false_per_hour']:.3f} | {fp['fdir_false_row_rate']:.3%} |")
    lines.append(f"| ML (Isolation Forest) | {fp['ml_false_episode_rate']:.2%} | {fp['ml_false_per_hour']:.3f} | {fp['ml_false_row_rate']:.3%} |")
    lines.append("")
    lines.append("**Read the row-level column, not just the episode-level one, for ML:** "
                  f"with `contamination={metadata['contamination']}`, sklearn's `predict()` is "
                  "constructed to flag roughly that fraction of in-distribution samples by "
                  "definition. At 600 samples/episode, essentially any nonzero per-row rate makes "
                  "the *episode*-level \"had at least one false alarm\" rate approach 100% -- that's "
                  "an artifact of episode length, not evidence the detector is unusable. The "
                  "row-level rate is the number that should be compared to the "
                  f"`contamination={metadata['contamination']}` setting.")
    lines.append("")
    lines.append("**Important distinction:** `FDIR-008` (see `docs/requirements/SRS.md`) targets "
                  "<=1 false *SAFE-mode entry* per 6h. The ML false-alarm rate above is a "
                  "different, broader measure (any `ML_ANOMALY` flag latch) -- `ML_ANOMALY` can "
                  "never force SAFE mode by itself (see `fdir/engine.py`'s "
                  "`SAFE_MODE_TRIGGER_FLAGS`), so this table is not a measurement of FDIR-008 "
                  "and should not be read as one.")
    lines.append("")
    lines.append("## What the anomaly score is -- and is not")
    lines.append("")
    lines.append("The Isolation Forest score is the model's mean normalized path length across "
                  "its trees (shorter average path to isolate a point = more anomalous; sklearn's "
                  "`decision_function` reports this so that lower values mean more anomalous). "
                  "**This is not a probability.** It is not calibrated against any real frequency "
                  "of fault occurrence, and nothing in this codebase presents it as one. The "
                  "binary anomalous/normal calls in the table above come from sklearn's own "
                  "`predict()`, which thresholds this score using the `contamination=" +
                  str(metadata["contamination"]) + "` value chosen at training time -- an explicit "
                  "hyperparameter, not a discovered probability cutoff.")
    lines.append("")
    lines.append("## Computational / memory requirements")
    lines.append("")
    lines.append(f"{n_trees} trees, average {avg_nodes:.0f} nodes/tree ({n_trees * avg_nodes:.0f} "
                  f"total nodes). Exported to C (`ml/export_embedded.py` -> "
                  f"`firmware/inc/anomaly_model.h`) as flat arrays of (feature index, threshold, "
                  f"left child, right child) per node -- roughly {approx_bytes/1024:.0f} KB as "
                  "plain arrays before any packing/compression, well within a typical STM32F4's "
                  "flash budget. Inference is pure integer/float comparison tree traversal -- "
                  f"at most {n_trees} traversals of depth ~log2({metadata['max_samples']}) each per "
                  "sample, no floating-point matrix multiplication and no neural-network runtime "
                  "dependency. This is the reason Isolation Forest was chosen over a neural "
                  "network for Phase 1 -- see `docs/architecture/phase0-1-engineering-decisions.md`, "
                  "decision 4.")
    lines.append("")
    lines.append("## Limitations of synthetic data")
    lines.append("")
    lines.append("- Sensor noise is modeled as independent Gaussian per channel; real sensor noise "
                  "(temperature-dependent, correlated across axes, occasionally non-Gaussian) is "
                  "not represented.")
    lines.append("- No radiation, EMI, or thermal-cycling effects are modeled at all.")
    lines.append("- Fault signatures (step changes, linear ramps, frozen values, non-response) are "
                  "simplified models of real failure modes, not measurements of how this project's "
                  "actual hardware fails.")
    lines.append("- All thresholds and debounce windows (`fdir/config.py`) are unvalidated design "
                  "targets, not characterized from real hardware timing.")
    lines.append("- Every number above comes from one simulated environment with a fixed noise "
                  "model; it may not generalize to a differently-tuned simulator, let alone to "
                  "real hardware.")
    lines.append("- Training and held-out data were generated by the same environment code with "
                  "different seeds, not by independently-modeled processes -- this is a weaker "
                  "form of held-out evaluation than genuinely independent data would provide.")
    lines.append("")
    lines.append("![Score distributions](../../ml/reports/score_distributions.png)")
    lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
