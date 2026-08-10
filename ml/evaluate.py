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
    ml_flagged_samples, fault_active_samples = 0, 0

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

        # Per-SAMPLE rate, not just per-episode. Episode-level recall alone is
        # badly misleading here: with ~250 fault-active samples per episode and
        # a threshold that flags ~1% of in-distribution samples by construction
        # (contamination), "at least one flag somewhere in the episode" is
        # nearly certain by chance even for a fault the model cannot actually
        # discriminate. Comparing this rate against the nominal false-flag rate
        # is what shows whether there is real signal.
        active = ep_df["fault_active"].values
        if active.any():
            ml_flagged_samples += int((preds[active] == -1).sum())
            fault_active_samples += int(active.sum())

    n = len(episodes)
    return {
        "fault_type": fault_type,
        "episodes": n,
        "fdir_recall": fdir_detected / n if n else None,
        "fdir_mean_latency_s": sum(fdir_latencies) / len(fdir_latencies) if fdir_latencies else None,
        "ml_recall": ml_detected / n if n else None,
        "ml_mean_latency_s": sum(ml_latencies) / len(ml_latencies) if ml_latencies else None,
        "ml_per_sample_rate": ml_flagged_samples / fault_active_samples if fault_active_samples else None,
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

    split_stats = compute_split_stats(model, metadata["feature_columns"])
    print(f"splits on imu_responded: {split_stats['splits_on_imu_responded']} of "
          f"{split_stats['total_internal_nodes']} internal nodes")
    write_report(fault_results, fp, metadata, split_stats)
    print(f"\nreport written to {REPORT_PATH}")
    print(f"plot written to {PLOT_PATH}")


def compute_split_stats(model, feature_names):
    """How often does the model actually split on `imu_responded`? A feature
    that's constant in nominal-only training data has zero variance and never
    becomes a usable split point -- this quantifies that rather than asserting it."""
    idx = feature_names.index("imu_responded")
    used = sum(int((est.tree_.feature == idx).sum()) for est in model.estimators_)
    total = sum(int((est.tree_.feature >= 0).sum()) for est in model.estimators_)
    return {
        "splits_on_imu_responded": used,
        "total_internal_nodes": total,
        "n_trees": len(model.estimators_),
    }


def write_report(fault_results, fp, metadata, split_stats):
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
    nominal_rate = fp["ml_false_row_rate"]
    lines.append("| Fault type | Episodes | FDIR recall | FDIR mean latency (s) | ML episode recall | ML mean latency (s) | ML per-sample flag rate | vs. nominal |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in fault_results:
        def fmt(x):
            return f"{x:.2f}" if isinstance(x, float) else "n/a"
        psr = r.get("ml_per_sample_rate")
        psr_s = f"{psr:.1%}" if psr is not None else "n/a"
        ratio_s = f"{psr / nominal_rate:.1f}x" if (psr is not None and nominal_rate) else "n/a"
        lines.append(f"| {r['fault_type']} | {r['episodes']} | {fmt(r['fdir_recall'])} | "
                      f"{fmt(r['fdir_mean_latency_s'])} | {fmt(r['ml_recall'])} | "
                      f"{fmt(r['ml_mean_latency_s'])} | {psr_s} | {ratio_s} |")
    lines.append("")
    lines.append("### Read the last two columns, not the recall column")
    lines.append("")
    lines.append("**ML episode recall is the misleading number here, and it is reported only "
                  "because it would be conspicuous to omit.** \"Episode recall\" asks *did at "
                  "least one sample anywhere in this episode get flagged* -- and with ~250 "
                  f"fault-active samples per episode against a threshold that flags {nominal_rate:.1%} "
                  "of in-distribution samples by construction, that question answers itself "
                  "affirmatively by chance alone, whether or not the model can actually "
                  "discriminate the fault. This is the exact same episode-length artifact "
                  "documented for false positives below; it inflates recall and false-alarm "
                  "rate identically, and an earlier draft of this report applied that reasoning "
                  "to only one of the two.")
    lines.append("")
    lines.append("The **per-sample flag rate against the nominal baseline** (last two columns) "
                  "is the honest measure of discriminative power. On that measure:")
    lines.append("")
    for r in fault_results:
        psr = r.get("ml_per_sample_rate")
        if psr is None or not nominal_rate:
            continue
        ratio = psr / nominal_rate
        if ratio >= 10:
            verdict = "**strongly detected** -- unambiguous, orders of magnitude above baseline"
        elif ratio >= 2:
            verdict = ("weak but real signal -- elevated over baseline, though the score "
                       "distributions overlap nominal substantially")
        else:
            verdict = ("**no discriminative power** -- flagged at or below the nominal false-alarm "
                       "rate. Any episode-level \"recall\" for this fault is chance, not detection")
        lines.append(f"- `{r['fault_type']}` ({ratio:.1f}x baseline): {verdict}.")
    lines.append("")
    drift_row = next((r for r in fault_results if r["fault_type"] == "gradual_drift"), None)
    lockup_row = next((r for r in fault_results if r["fault_type"] == "sensor_lockup"), None)
    if drift_row and lockup_row:
        drift_ratio = (drift_row["ml_per_sample_rate"] / nominal_rate) if nominal_rate else 0
        lockup_ratio = (lockup_row["ml_per_sample_rate"] / nominal_rate) if nominal_rate else 0
        lines.append("## What the ML layer actually adds")
        lines.append("")
        lines.append(f"**The one unambiguous win is `sensor_lockup`** ({lockup_ratio:.0f}x the "
                      f"nominal flag rate, {lockup_row['ml_per_sample_rate']:.0%} of fault samples "
                      "flagged). A frozen IMU drives every rolling-standard-deviation feature to "
                      "exactly zero across six channels simultaneously -- a region of feature space "
                      "with no nominal training data anywhere near it, which is precisely the "
                      "situation an isolation-based method handles well. The score distribution "
                      "for this fault is cleanly separated from nominal (see the plot below); it "
                      "is the only fault type for which that is true.")
        lines.append("")
        lines.append(f"**`gradual_drift` is a weaker, more qualified result than an earlier draft "
                      f"of this report claimed.** FDIR's adaptive baseline (`FDIR-006`, an EWMA "
                      f"over `bus_voltage_v`) recalled **{drift_row['fdir_recall']:.0%}** of drift "
                      "episodes -- a genuine, structural blind spot, and not a bug: an "
                      "online-adaptive statistic continuously updates its notion of \"normal\" "
                      "toward the current signal, so a drift slow enough that no single "
                      "sample-to-sample step exceeds the deviation threshold simply gets absorbed "
                      "as the new normal. A model trained once on a fixed reference and never "
                      "updated does not have that blind spot, and the numbers do show the "
                      f"Isolation Forest flagging drift samples at {drift_ratio:.1f}x the nominal "
                      "rate -- real, consistent signal in the right direction.")
        lines.append("")
        timeout_row = next((r for r in fault_results if r["fault_type"] == "sensor_timeout"), None)
        if timeout_row and nominal_rate and (timeout_row["ml_per_sample_rate"] / nominal_rate) < 2:
            s = split_stats
            lines.append("**And `sensor_timeout` is a structural blind spot for the model, for a "
                          "reason worth understanding rather than patching over.** The only "
                          "signature of this fault is the `imu_responded` flag going false; the "
                          "environment still emits plausible-looking IMU values (that is what "
                          "distinguishes a timeout from a lockup). But `imu_responded` is "
                          "*constant at 1.0 throughout the nominal-only training set* -- zero "
                          f"variance -- so no tree ever splits on it: a direct count over the "
                          f"trained model finds **{s['splits_on_imu_responded']} splits on that "
                          f"feature out of {s['total_internal_nodes']} internal nodes across all "
                          f"{s['n_trees']} trees.** Flipping it to 0.0 at inference therefore "
                          "changes no traversal path whatsoever, and the model is not merely bad "
                          "at this fault but blind to it by construction.")
            lines.append("")
            lines.append("The general lesson, which applies well beyond this one fault: **any "
                          "feature that is constant in nominal-only training data is invisible to "
                          "an isolation-based detector, no matter how diagnostic it would be at "
                          "inference time.** Training on normal data alone means the model can only "
                          "learn to be surprised along axes that actually varied during training. "
                          "This is not a tuning problem and more trees will not fix it.")
            lines.append("")
            lines.append("This is also a concrete argument *for* the hybrid architecture rather "
                          "than against it. The deterministic layer catches `sensor_timeout` at "
                          f"{timeout_row['fdir_recall']:.0%} recall in "
                          f"{timeout_row['fdir_mean_latency_s']:.2f} s, because a response/no-response "
                          "check needs no training distribution at all -- and the ML layer catches "
                          "`sensor_lockup`, where a frozen-but-responding sensor produces perfectly "
                          "in-range values that no fixed threshold would object to. The two layers "
                          "have genuinely complementary blind spots, which is measured here, not "
                          "assumed.")
            lines.append("")
        lines.append("But that is a **weak** separation, not a solved detection problem. At "
                      f"{drift_row['ml_per_sample_rate']:.1%} of drift samples flagged, the score "
                      "distributions overlap nominal heavily, and the 100% *episode* recall figure "
                      "is largely the episode-length artifact described above rather than reliable "
                      "per-sample detection. The correct reading is: **this measurement supports "
                      "the direction of `FDIR-007`'s argument -- a trained model sees something the "
                      "adaptive baseline structurally cannot -- without yet demonstrating a "
                      "detector good enough to depend on for drift.** Whether that gap closes with "
                      "better features (an explicit long-window trend feature would target drift "
                      "directly), a different algorithm, or real rather than synthetic data is an "
                      "open question, and deliberately not answered here.")
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
