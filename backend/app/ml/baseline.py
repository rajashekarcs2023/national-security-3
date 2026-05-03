"""Local baseline tracker.

At deployment time, every site has a different ambient electromagnetic
background. The edge node should learn what "normal" looks like locally and
flag deviations on top of the universal classifier. This is what differentiates
SpectrumCustody from a fixed-rule classifier.

We track:
  - rolling mean embedding (centroid of "what we usually see at Site Alpha")
  - rolling distribution of dominant frequencies
  - rolling rate of anomaly classifications
  - rolling power distribution
"""

from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np


class LocalBaselineTracker:
    def __init__(self, window: int = 120, embed_dim: int = 32):
        self.window = window
        self.embed_dim = embed_dim
        # Rolling embedding buffer
        self._emb_buf: deque[np.ndarray] = deque(maxlen=window)
        # Rolling dominant-frequency-bin buffer
        self._freq_buf: deque[int] = deque(maxlen=window)
        # Rolling power-dBm buffer
        self._power_buf: deque[float] = deque(maxlen=window)
        # Rolling anomaly count buffer
        self._anom_buf: deque[int] = deque(maxlen=window)

    @property
    def n_observed(self) -> int:
        return len(self._emb_buf)

    def update(
        self,
        embedding: np.ndarray,
        dominant_freq_bin: int,
        power_dbm: float,
        is_anomaly: bool,
    ) -> None:
        self._emb_buf.append(embedding.astype(np.float32))
        self._freq_buf.append(int(dominant_freq_bin))
        self._power_buf.append(float(power_dbm))
        self._anom_buf.append(1 if is_anomaly else 0)

    def deviation(self, embedding: np.ndarray) -> float:
        """How far this embedding is from the local baseline mean (0 if not enough history)."""
        if len(self._emb_buf) < 8:
            return 0.0
        mean = np.mean(np.stack(self._emb_buf), axis=0)
        std = np.std(np.stack(self._emb_buf), axis=0).mean()
        d = float(np.linalg.norm(embedding - mean))
        # Normalise by typical embedding scale.
        if std < 1e-6:
            return 0.0
        return float(np.tanh(d / max(1e-3, 4 * std)))

    def anomaly_rate(self) -> float:
        if not self._anom_buf:
            return 0.0
        return float(sum(self._anom_buf) / len(self._anom_buf))

    def dominant_band_histogram(self, n_bands: int = 8) -> list[int]:
        if not self._freq_buf:
            return [0] * n_bands
        bins = np.linspace(0, 64, n_bands + 1)
        hist, _ = np.histogram(list(self._freq_buf), bins=bins)
        return [int(x) for x in hist]

    def baseline_summary(self) -> dict:
        if not self._emb_buf:
            return {
                "n_observed": 0,
                "anomaly_rate": 0.0,
                "mean_power_dbm": None,
                "dominant_band_histogram": [0] * 8,
            }
        return {
            "n_observed": len(self._emb_buf),
            "anomaly_rate": self.anomaly_rate(),
            "mean_power_dbm": float(np.mean(list(self._power_buf))),
            "dominant_band_histogram": self.dominant_band_histogram(),
        }
