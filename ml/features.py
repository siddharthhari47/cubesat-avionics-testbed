"""
Feature engineering for the anomaly-detection pipeline.

Deliberately simple: raw channel value + a short rolling mean/std per channel,
computed WITHIN each episode only (never across episode boundaries -- that
would leak information between unrelated simulated runs, a real bug to avoid,
not a style preference).

Why this feature set and not something fancier: raw values alone miss
temporal anomalies like gradual_drift, where no single sample looks wrong in
isolation -- only the trend does. A short rolling window gives Isolation
Forest just enough temporal context to catch that, without going as far as
full sequence modeling (an LSTM/transformer over windows), which would need
far more data than this synthetic set has to offer and far more compute than
an STM32 has to spend. This is a middle ground chosen for this problem's
actual shape, not a default.

Streaming note: this implementation is batch/dataframe-oriented (pandas
groupby+rolling) because that's what's needed for training and evaluation
here. Adapting it to true online/streaming inference on an MCU means
replacing the rolling window with a fixed-size ring buffer per channel,
updated one sample at a time -- the feature *definitions* below stay the
same, only how the rolling stats get computed changes. That adaptation is a
V1 firmware task, not done here.
"""

from typing import List

import numpy as np
import pandas as pd

CHANNELS = [
    "temp_c",
    "accel_x", "accel_y", "accel_z",
    "gyro_x", "gyro_y", "gyro_z",
    "mag_x", "mag_y", "mag_z",
    "bus_voltage_v", "bus_current_a",
]
BINARY_FIELDS = ["imu_responded", "temp_responded"]
ROLLING_WINDOW = 5


def feature_columns() -> List[str]:
    """
    The exact, ordered list of feature column names this module produces.
    ml/train.py, ml/evaluate.py, and ml/export_embedded.py all import this --
    a mismatch here would silently break inference, so there is exactly one
    place this order is defined.
    """
    cols = []
    for ch in CHANNELS:
        cols += [ch, f"{ch}_roll_mean", f"{ch}_roll_std"]
    cols += BINARY_FIELDS
    return cols


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    df must have an 'episode_id' column plus all CHANNELS/BINARY_FIELDS
    columns (as produced by simulator/dataset_gen.py or an equivalent
    real-time buffer). Returns a new dataframe with exactly feature_columns(),
    same row count and order as the input, index preserved so callers can
    join back to ground truth / raw samples by position.
    """
    out = pd.DataFrame(index=df.index)
    grouped = df.groupby("episode_id", sort=False)

    for ch in CHANNELS:
        out[ch] = df[ch].astype(float)
        roll = grouped[ch].rolling(window=ROLLING_WINDOW, min_periods=1)
        out[f"{ch}_roll_mean"] = roll.mean().reset_index(level=0, drop=True)
        # std of a single sample is NaN (0/0 in pandas' ddof=1 default); a
        # single observation has no useful "spread" yet, so 0.0 is the
        # correct value, not a missing one.
        out[f"{ch}_roll_std"] = roll.std().reset_index(level=0, drop=True).fillna(0.0)

    for field in BINARY_FIELDS:
        out[field] = df[field].astype(float)

    return out[feature_columns()]
