# ML Anomaly Detection -- Evaluation Report

**Status: Simulated + Trained.** Every number below comes from this synthetic simulator and this specific trained model. Nothing here has been run against real hardware or real sensor data, and none of it should be read as a claim about real-world detection performance.

Model: `ml\models\isolation_forest_v1.joblib` -- Isolation Forest, 50 trees, trained on 24000 nominal samples (seed 0). Evaluated on a held-out dataset generated with seed 1 -- disjoint episodes, never seen during training.

## Per-fault-type detection: FDIR (deterministic) vs. ML (Isolation Forest)

No single blended "accuracy" number is reported here on purpose -- it would hide exactly the differences that matter. FDIR "detection" means the fault's mapped `FaultFlag` bit latched (not necessarily that SAFE mode was entered -- `sensor_timeout` and `gradual_drift` are flag-only by design, see `fdir/engine.py`'s `SAFE_MODE_TRIGGER_FLAGS`). ML "detection" means `IsolationForest.predict() == -1` (sklearn's own contamination-derived threshold, not a hand-picked cutoff).

| Fault type | Episodes | FDIR recall | FDIR mean latency (s) | ML recall | ML mean latency (s) |
|---|---|---|---|---|---|
| undervoltage | 15 | 1.00 | 0.20 | 1.00 | 1.82 |
| thermal | 15 | 1.00 | 0.30 | 1.00 | 2.57 |
| sensor_lockup | 15 | 1.00 | 0.50 | 1.00 | 0.45 |
| sensor_timeout | 15 | 1.00 | 0.20 | 0.80 | 10.41 |
| gradual_drift | 15 | 0.00 | n/a | 1.00 | 5.69 |

## Notable finding: gradual_drift

FDIR's adaptive baseline (`FDIR-006`, an EWMA over `bus_voltage_v`) recalled **0%** of `gradual_drift` episodes; the trained Isolation Forest recalled **100%**. This is not a bug in the EWMA detector -- it is doing exactly what an online-adaptive statistic is supposed to do, continuously updating its notion of "normal" toward the current signal. That is precisely what makes it structurally unable to catch a *slow* drift: each sample-to-sample change is too small to ever exceed the deviation threshold, so the baseline just tracks the drift as the new normal instead of flagging it. The Isolation Forest, trained once on a fixed nominal reference and never updated afterward, has no such blind spot -- it still measures every new sample against the original training distribution. This is the concrete, measured version of the argument for adding a trained ML layer on top of adaptive statistics in the first place (see `docs/requirements/SRS.md`'s `FDIR-007` and `docs/architecture/phase0-1-engineering-decisions.md`, decision 4) -- not a hypothetical benefit, a specific failure mode this evaluation reproduced and measured.

## False positive rate (nominal episodes only)

Measured on 40 held-out nominal episodes (0.67 simulated hours total, 40 episodes x 600 samples/episode).

| Detector | Episodes with >=1 false alarm | False alarms/hour (episode-level) | False-flagged samples (row-level) |
|---|---|---|---|
| FDIR (any fault flag) | 0.00% | 0.000 | 0.000% |
| ML (Isolation Forest) | 100.00% | 60.100 | 0.950% |

**Read the row-level column, not just the episode-level one, for ML:** with `contamination=0.01`, sklearn's `predict()` is constructed to flag roughly that fraction of in-distribution samples by definition. At 600 samples/episode, essentially any nonzero per-row rate makes the *episode*-level "had at least one false alarm" rate approach 100% -- that's an artifact of episode length, not evidence the detector is unusable. The row-level rate is the number that should be compared to the `contamination=0.01` setting.

**Important distinction:** `FDIR-008` (see `docs/requirements/SRS.md`) targets <=1 false *SAFE-mode entry* per 6h. The ML false-alarm rate above is a different, broader measure (any `ML_ANOMALY` flag latch) -- `ML_ANOMALY` can never force SAFE mode by itself (see `fdir/engine.py`'s `SAFE_MODE_TRIGGER_FLAGS`), so this table is not a measurement of FDIR-008 and should not be read as one.

## What the anomaly score is -- and is not

The Isolation Forest score is the model's mean normalized path length across its trees (shorter average path to isolate a point = more anomalous; sklearn's `decision_function` reports this so that lower values mean more anomalous). **This is not a probability.** It is not calibrated against any real frequency of fault occurrence, and nothing in this codebase presents it as one. The binary anomalous/normal calls in the table above come from sklearn's own `predict()`, which thresholds this score using the `contamination=0.01` value chosen at training time -- an explicit hyperparameter, not a discovered probability cutoff.

## Computational / memory requirements

50 trees, average 125 nodes/tree (6228 total nodes). Exported to C (`ml/export_embedded.py` -> `firmware/inc/anomaly_model.h`) as flat arrays of (feature index, threshold, left child, right child) per node -- roughly 97 KB as plain arrays before any packing/compression, well within a typical STM32F4's flash budget. Inference is pure integer/float comparison tree traversal -- at most 50 traversals of depth ~log2(256) each per sample, no floating-point matrix multiplication and no neural-network runtime dependency. This is the reason Isolation Forest was chosen over a neural network for Phase 1 -- see `docs/architecture/phase0-1-engineering-decisions.md`, decision 4.

## Limitations of synthetic data

- Sensor noise is modeled as independent Gaussian per channel; real sensor noise (temperature-dependent, correlated across axes, occasionally non-Gaussian) is not represented.
- No radiation, EMI, or thermal-cycling effects are modeled at all.
- Fault signatures (step changes, linear ramps, frozen values, non-response) are simplified models of real failure modes, not measurements of how this project's actual hardware fails.
- All thresholds and debounce windows (`fdir/config.py`) are unvalidated design targets, not characterized from real hardware timing.
- Every number above comes from one simulated environment with a fixed noise model; it may not generalize to a differently-tuned simulator, let alone to real hardware.
- Training and held-out data were generated by the same environment code with different seeds, not by independently-modeled processes -- this is a weaker form of held-out evaluation than genuinely independent data would provide.

![Score distributions](../../ml/reports/score_distributions.png)
