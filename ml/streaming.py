"""
Streaming (online) feature extraction for ML #1.

ml/features.py is batch/pandas: it groups by episode_id and rolls over a whole
dataframe. That is right for training and evaluation and wrong for a spacecraft,
which sees one sample at a time and has no episodes. This is the online
equivalent, and the contract it must satisfy is exact numerical parity with the
batch path -- the model's split thresholds were learned against those features,
so a streaming implementation that is merely close produces a detector that has
silently drifted from the one that was evaluated.

FOUR THINGS THE AUDIT FLAGGED, ALL LOAD-BEARING:

1. `episode_id` has no streaming equivalent. The boundary that replaces it is a
   RESET: buffers clear on boot, on any transition through BOOT, and on a
   telemetry gap. Nothing in the codebase defined that policy before.

2. O(n) becomes O(1). Calling build_features() on a growing buffer each tick is
   O(n^2). Twelve ring buffers of five floats is 240 bytes and constant time.
   Recomputing mean/std over five elements is also more numerically stable than
   incremental sum/sum-of-squares, which drifts over a long run.

3. `ddof=1` is not a detail. Pandas' rolling.std() is the SAMPLE standard
   deviation. A C implementation using the population form would shift all
   twelve *_roll_std features by sqrt(5/4) ~ 1.118 -- an 11.8% error against
   thresholds frozen into the trees. Matched exactly here and asserted by test.

4. `min_periods=1` is a cold-start false-positive generator, and this is the
   subtle one. On the first sample after any reset, every *_roll_std is 0.0 --
   which is PRECISELY the sensor_lockup signature the model detects best (91x
   baseline). So a naive streaming port emits a burst of maximally-anomalous
   vectors at every boot. This class therefore suppresses the advisory until the
   window is genuinely full, which is a deliberate divergence from training-time
   semantics rather than an oversight.
"""

import math
import sys
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.features import BINARY_FIELDS, CHANNELS, ROLLING_WINDOW, feature_columns  # noqa: E402


def _sample_std(values) -> float:
    """
    Sample standard deviation, ddof=1 -- matching pandas rolling.std().

    Returns 0.0 for a single observation, matching features.py's fillna(0.0):
    one sample has no spread, and that is a defined value rather than missing.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


class StreamingFeatureExtractor:
    """
    Feeds one RawSample at a time and returns the same 38-element vector
    ml/features.py produces for the corresponding row.

    `ready` is False until the window is full. A caller must not issue an ML
    advisory while it is False -- see point 4 in the module docstring.
    """

    def __init__(self, window: int = ROLLING_WINDOW):
        self.window = window
        self._buffers: Dict[str, Deque[float]] = {ch: deque(maxlen=window) for ch in CHANNELS}
        self._latest_binary: Dict[str, float] = {f: 1.0 for f in BINARY_FIELDS}
        self._count = 0

    def reset(self, reason: str = "") -> None:
        """
        Clear all history. This is the streaming stand-in for an episode
        boundary: call on boot, on any transition through BOOT, and on a
        telemetry gap longer than a few sample periods. Blending across a gap
        would fabricate a rolling statistic from samples that are not adjacent
        in time.
        """
        for buf in self._buffers.values():
            buf.clear()
        self._count = 0
        self.last_reset_reason = reason

    @property
    def ready(self) -> bool:
        return self._count >= self.window

    def push(self, sample) -> Optional[List[float]]:
        """
        Add a sample and return its feature vector, or None if not yet warm.

        Returning None rather than a partial vector is the point: a partial
        window produces all-zero standard deviations, which is the lockup
        signature, so emitting it would manufacture the exact anomaly the model
        is best at detecting.
        """
        for ch in CHANNELS:
            self._buffers[ch].append(float(getattr(sample, ch)))
        for f in BINARY_FIELDS:
            self._latest_binary[f] = float(bool(getattr(sample, f)))
        self._count += 1

        if not self.ready:
            return None
        return self._vector()

    def push_unguarded(self, sample) -> List[float]:
        """
        Same as push() but always returns a vector, using partial-window
        statistics exactly as the batch path's min_periods=1 does.

        This exists ONLY so the parity test can compare against build_features()
        row for row, including its warm-up rows. Flight code must use push().
        """
        for ch in CHANNELS:
            self._buffers[ch].append(float(getattr(sample, ch)))
        for f in BINARY_FIELDS:
            self._latest_binary[f] = float(bool(getattr(sample, f)))
        self._count += 1
        return self._vector()

    def _vector(self) -> List[float]:
        out: List[float] = []
        for ch in CHANNELS:
            buf = self._buffers[ch]
            out.append(buf[-1])
            out.append(sum(buf) / len(buf))
            out.append(_sample_std(buf))
        for f in BINARY_FIELDS:
            out.append(self._latest_binary[f])
        return out

    @staticmethod
    def feature_names() -> List[str]:
        return feature_columns()
