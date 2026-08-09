"""
Batch/headless labeled dataset generator for the ML pipeline.

Unlike run_simulator.py (real-time, paced, socket-serving), this drives
SpacecraftEnvironment directly with no pacing and no sockets -- just tight
.step() loops -- so a dataset with hours of simulated telemetry generates in
seconds. Ground truth (which fault was active, when) is recorded alongside
every sample; this is the ONLY place in the codebase where that ground truth
is allowed to leave the environment and get written down, since it's the
whole point of a labeled dataset for training/evaluation. fdir/engine.py must
never read the output of this script's label columns as an input signal.

Run: python simulator/dataset_gen.py [--seed 0] [--out data/datasets/phase1_dataset.csv]
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import SpacecraftEnvironment, FAULT_TYPES  # noqa: E402

DT_S = 0.1                  # 10 Hz, a realistic telemetry sample rate
NOMINAL_EPISODES = 40
FAULT_EPISODES_PER_TYPE = 15
FAULT_ONSET_S = 20.0
FAULT_CLEAR_S = 45.0        # not applied to gradual_drift -- see below


def _sample_fields(sample):
    return {
        "temp_c": sample.temp_c,
        "accel_x": sample.accel_x, "accel_y": sample.accel_y, "accel_z": sample.accel_z,
        "gyro_x": sample.gyro_x, "gyro_y": sample.gyro_y, "gyro_z": sample.gyro_z,
        "mag_x": sample.mag_x, "mag_y": sample.mag_y, "mag_z": sample.mag_z,
        "bus_voltage_v": sample.bus_voltage_v, "bus_current_a": sample.bus_current_a,
        "imu_responded": sample.imu_responded, "temp_responded": sample.temp_responded,
    }


def generate_episode(episode_id, seed, fault_type, episode_duration_s):
    """fault_type is None for a purely nominal episode."""
    env = SpacecraftEnvironment(seed=seed)
    rows = []
    t = 0.0
    injected = False
    cleared_at = None if fault_type != "gradual_drift" else float("inf")
    while t < episode_duration_s:
        sample, truth = env.step(DT_S)
        t = truth.t

        if fault_type is not None and not injected and t >= FAULT_ONSET_S:
            env.inject(fault_type)
            injected = True
        if (fault_type is not None and fault_type != "gradual_drift"
                and injected and cleared_at is None and t >= FAULT_CLEAR_S):
            env.clear(fault_type)
            cleared_at = t

        row = _sample_fields(sample)
        row.update({
            "episode_id": episode_id,
            "t": t,
            "fault_type": truth.active_faults[0] if truth.active_faults else "none",
            "fault_active": bool(truth.active_faults),
        })
        rows.append(row)
    return rows


def generate_dataset(master_seed, episode_duration_s):
    episodes = []
    episode_index = 0

    for _ in range(NOMINAL_EPISODES):
        episodes.append(("none", episode_index))
        episode_index += 1
    for fault_type in FAULT_TYPES:
        for _ in range(FAULT_EPISODES_PER_TYPE):
            episodes.append((fault_type, episode_index))
            episode_index += 1

    all_rows = []
    for fault_type, idx in episodes:
        episode_seed = master_seed * 100_000 + idx
        rows = generate_episode(
            episode_id=idx, seed=episode_seed,
            fault_type=None if fault_type == "none" else fault_type,
            episode_duration_s=episode_duration_s,
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    manifest = {
        "master_seed": master_seed,
        "dt_s": DT_S,
        "episode_duration_s": episode_duration_s,
        "fault_onset_s": FAULT_ONSET_S,
        "fault_clear_s": FAULT_CLEAR_S,
        "nominal_episodes": NOMINAL_EPISODES,
        "fault_episodes_per_type": FAULT_EPISODES_PER_TYPE,
        "fault_types": list(FAULT_TYPES),
        "total_episodes": len(episodes),
        "total_samples": len(df),
        "generated_at_unix_s_placeholder": None,  # filled by caller; Workflow-safe scripts avoid time.time() at import time
    }
    return df, manifest


def main():
    parser = argparse.ArgumentParser(description="Generate a labeled synthetic telemetry dataset")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=str, default="data/datasets/phase1_dataset.csv")
    parser.add_argument("--episode-duration-s", type=float, default=60.0)
    args = parser.parse_args()

    df, manifest = generate_dataset(args.seed, args.episode_duration_s)
    manifest["generated_at_unix_s_placeholder"] = time.time()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    manifest_path = out_path.with_name(out_path.stem + "_manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print(f"wrote {len(df)} samples across {manifest['total_episodes']} episodes to {out_path}")
    print(f"manifest: {manifest_path}")
    print("samples per fault_type:")
    print(df["fault_type"].value_counts().to_string())
    print(f"fault_active correlates with fault_type != 'none': "
          f"{(df['fault_active'] == (df['fault_type'] != 'none')).all()}")


if __name__ == "__main__":
    main()
