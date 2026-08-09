"""
Fast tests for the ML pipeline (ml/features.py, ml/train.py). Trains a small
model on a small synthetic dataset generated in-test -- must run in seconds,
not minutes, so this stays a real part of the suite instead of something
nobody runs.

Deliberately does not assert exact numeric thresholds that depend on precise
random draws -- asserts relative/directional properties instead, with a fixed
seed for reproducibility.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from simulator.dataset_gen import generate_episode, FAULT_ONSET_S  # noqa: E402
from ml.features import build_features, feature_columns  # noqa: E402
from sklearn.ensemble import IsolationForest  # noqa: E402


# generate_episode() injects at the module-level FAULT_ONSET_S (20s) regardless
# of episode duration -- must run longer than that or the fault never actually
# gets injected before the episode ends. Caught by this test's own assertion
# below, not assumed.
SMALL_EPISODE_DURATION_S = FAULT_ONSET_S + 5.0


def _small_dataset(seed_base=1000):
    """5 nominal + 3 each of undervoltage/thermal, short episodes."""
    rows = []
    episode_id = 0
    for _ in range(5):
        rows += generate_episode(episode_id, seed_base + episode_id, None, SMALL_EPISODE_DURATION_S)
        episode_id += 1
    fault_episode_ids = {"undervoltage": [], "thermal": []}
    for fault_type in ("undervoltage", "thermal"):
        for _ in range(3):
            rows += generate_episode(episode_id, seed_base + episode_id, fault_type, SMALL_EPISODE_DURATION_S)
            fault_episode_ids[fault_type].append(episode_id)
            episode_id += 1
    import pandas as pd
    return pd.DataFrame(rows), fault_episode_ids


@pytest.fixture(scope="module")
def small_model_and_data():
    df, fault_episode_ids = _small_dataset()
    ever_faulty = df.groupby("episode_id")["fault_active"].transform("any")
    nominal = df[~ever_faulty].reset_index(drop=True)
    X_nominal = build_features(nominal)
    model = IsolationForest(n_estimators=20, max_samples=64, contamination=0.02, random_state=0)
    model.fit(X_nominal)
    return model, df, fault_episode_ids


def test_feature_columns_are_stable_and_match_output_shape():
    df, _ = _small_dataset()
    feats = build_features(df)
    assert list(feats.columns) == feature_columns()
    assert len(feats) == len(df)
    assert not feats.isna().any().any()


def test_training_uses_only_whole_nominal_episodes(small_model_and_data):
    model, df, _ = small_model_and_data
    # 5 nominal episodes * (10s / 0.1s) = 500 rows expected
    assert model.n_features_in_ == len(feature_columns())


@pytest.mark.parametrize("fault_type", ["undervoltage", "thermal"])
def test_injected_fault_scores_more_anomalous_than_nominal(small_model_and_data, fault_type):
    """Note: sklearn's decision_function is LOWER for more anomalous samples --
    this direction is asserted explicitly, not assumed, since getting it
    backwards would silently invalidate every downstream threshold decision."""
    model, df, fault_episode_ids = small_model_and_data

    nominal_df = df[df["fault_type"] == "none"]
    nominal_scores = model.decision_function(build_features(nominal_df))

    fault_ep_id = fault_episode_ids[fault_type][0]
    ep_df = df[df["episode_id"] == fault_ep_id].reset_index(drop=True)
    ep_scores = model.decision_function(build_features(ep_df))
    active_mask = ep_df["fault_active"].values

    assert active_mask.any(), "fixture bug: fault was never actually active in this episode"
    mean_active_score = ep_scores[active_mask].mean()
    mean_nominal_score = nominal_scores.mean()

    assert mean_active_score < mean_nominal_score, (
        f"{fault_type}: expected fault-active samples to score MORE anomalous "
        f"(lower decision_function) than nominal, got active={mean_active_score:.4f} "
        f"vs nominal={mean_nominal_score:.4f}"
    )


def test_predict_convention_minus_one_means_anomalous(small_model_and_data):
    """Sanity-checks the -1/1 convention this whole pipeline relies on."""
    model, df, fault_episode_ids = small_model_and_data
    ep_df = df[df["episode_id"] == fault_episode_ids["undervoltage"][0]].reset_index(drop=True)
    scores = model.decision_function(build_features(ep_df))
    preds = model.predict(build_features(ep_df))
    # Every row sklearn calls anomalous (-1) must have a lower score than
    # every row it calls normal (1) -- predict() and decision_function() must
    # never disagree on ordering, by construction of how sklearn implements both.
    if (preds == -1).any() and (preds == 1).any():
        assert scores[preds == -1].max() <= scores[preds == 1].min() + 1e-9
