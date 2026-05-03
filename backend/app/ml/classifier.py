"""Edge classifier wrapper.

Loads the trained SpectrumModel + class centroids + meta, and produces a
ClassifiedSignal for any input spectrogram. Combines:

  - softmax confidence over known classes
  - reconstruction error (autoencoder)
  - distance to nearest class centroid (in embedding space)

into a single 0-1 OOD score.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import torch

from app.ml.model import SpectrumModel
from app.ml.synth import CLASSES
from app.schemas import ClassifiedSignal, RFSignalReading, new_id, utc_now


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
WEIGHTS_PATH = DATA_DIR / "weights.pt"
CENTROIDS_PATH = DATA_DIR / "centroids.npy"
META_PATH = DATA_DIR / "meta.json"


# Map from raw signal class -> the "expected" flag we treat as friendly/background.
EXPECTED_CLASSES = {
    "background_noise",
    "friendly_radio_burst",
    "commercial_continuous",
}


class EdgeClassifier:
    """Live edge classifier. Stateless across calls; cheap to call repeatedly."""

    def __init__(self) -> None:
        self.device: str = "cpu"
        self.model: Optional[SpectrumModel] = None
        self.centroids: Optional[np.ndarray] = None
        self.classes: list[str] = CLASSES
        self.embed_dim: int = 32
        self.rec_err_mean: float = 0.0
        self.rec_err_std: float = 1e-3
        self.rec_err_p95: float = 1e-3
        self.val_acc: float = 0.0
        self.loaded: bool = False
        # Sensitivity mode adjusts thresholds at runtime.
        self.sensitivity: str = "normal"

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self) -> None:
        if not WEIGHTS_PATH.exists():
            raise FileNotFoundError(
                f"weights.pt not found at {WEIGHTS_PATH}. Run `python train.py` first."
            )
        with open(META_PATH) as f:
            meta = json.load(f)
        self.classes = meta["classes"]
        self.embed_dim = int(meta["embed_dim"])
        self.rec_err_mean = float(meta["rec_err_mean"])
        self.rec_err_std = float(meta["rec_err_std"])
        self.rec_err_p95 = float(meta["rec_err_p95"])
        self.val_acc = float(meta.get("val_acc", 0.0))

        self.model = SpectrumModel(num_classes=len(self.classes), embed_dim=self.embed_dim)
        state = torch.load(WEIGHTS_PATH, map_location="cpu")
        self.model.load_state_dict(state)
        self.model.eval()
        self.centroids = np.load(CENTROIDS_PATH)
        self.loaded = True

    def summary(self) -> dict:
        if not self.loaded or self.model is None:
            return {"loaded": False}
        s = self.model.parameter_summary()
        return {
            "loaded": True,
            "classes": self.classes,
            "val_acc": self.val_acc,
            **s,
        }

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def _ood_score(
        self,
        softmax: np.ndarray,
        rec_err: float,
        nearest_dist: float,
    ) -> float:
        """Combine three OOD signals into a single [0, 1] score."""
        # 1) Classifier uncertainty: 1 - max(softmax)
        sm_uncertainty = 1.0 - float(softmax.max())

        # 2) Reconstruction error normalised against training distribution.
        rec_z = max(0.0, (rec_err - self.rec_err_mean) / max(1e-6, self.rec_err_std))
        rec_norm = float(np.tanh(rec_z / 4.0))  # squash to [0, 1)

        # 3) Embedding distance: scale relative to "typical" inter-centroid scale.
        dists = np.linalg.norm(self.centroids[:, None] - self.centroids[None, :], axis=-1)
        typical = float(dists[dists > 0].mean()) if (dists > 0).any() else 1.0
        dist_norm = float(np.tanh(nearest_dist / max(1e-3, typical)))

        # Weighted blend. Reconstruction is the most reliable signal in our
        # synthetic setup; uncertainty supports it.
        ood = 0.45 * rec_norm + 0.35 * sm_uncertainty + 0.20 * dist_norm
        return float(np.clip(ood, 0.0, 1.0))

    def _priority(self, predicted_class: str, ood_score: float, baseline_dev: float) -> str:
        if predicted_class in EXPECTED_CLASSES:
            # Possibly elevate if local baseline says it's anomalous for this site.
            if baseline_dev > 0.7 and predicted_class == "commercial_continuous":
                return "low"
            return "low"
        # Unexpected classes
        if predicted_class == "drone_control_repeated_burst":
            return "critical" if ood_score > 0.4 else "high"
        if predicted_class in {"frequency_hopping", "unknown_ood"}:
            return "high" if ood_score > 0.5 else "medium"
        if predicted_class == "friendly_profile_mismatch":
            return "high"
        if predicted_class == "chirp":
            return "medium"
        return "medium"

    def _action_label(self, priority: str, predicted_class: str) -> str:
        if predicted_class in EXPECTED_CLASSES and priority == "low":
            return "ignore" if predicted_class == "background_noise" else "log"
        if priority in {"high", "critical"}:
            return "sync"
        return "queue"

    def _explanation(
        self,
        predicted_class: str,
        confidence: float,
        ood_score: float,
        nearest_class: str,
        nearest_dist: float,
        rec_err: float,
        baseline_dev: float,
    ) -> str:
        parts = []
        parts.append(f"Predicted class: {predicted_class} (confidence {confidence:.2f}).")
        parts.append(f"Nearest known profile: {nearest_class} (embedding distance {nearest_dist:.2f}).")
        parts.append(
            f"Reconstruction error {rec_err:.4f} vs training mean {self.rec_err_mean:.4f}."
        )
        parts.append(f"OOD score: {ood_score:.2f}.")
        if baseline_dev > 0.0:
            parts.append(f"Local baseline deviation: {baseline_dev:.2f}.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    def classify(
        self,
        spec: np.ndarray,
        reading: RFSignalReading,
        baseline_deviation: float = 0.0,
    ) -> ClassifiedSignal:
        if not self.loaded or self.model is None or self.centroids is None:
            raise RuntimeError("Classifier not loaded — call load() first.")

        x = torch.from_numpy(spec).float().unsqueeze(0).unsqueeze(0)  # (1,1,64,64)
        with torch.no_grad():
            logits, emb, recon = self.model(x)
        logits_np = logits.squeeze(0).numpy()
        emb_np = emb.squeeze(0).numpy()
        recon_np = recon.squeeze(0).squeeze(0).numpy()

        softmax = np.exp(logits_np - logits_np.max())
        softmax = softmax / softmax.sum()
        pred_idx = int(softmax.argmax())
        predicted_class = self.classes[pred_idx]
        confidence = float(softmax[pred_idx])

        rec_err = float(((recon_np - spec) ** 2).mean())

        # Distance to nearest centroid in embedding space.
        dists = np.linalg.norm(self.centroids - emb_np, axis=1)
        nearest_idx = int(dists.argmin())
        nearest_class = self.classes[nearest_idx]
        nearest_dist = float(dists[nearest_idx])

        ood = self._ood_score(softmax, rec_err, nearest_dist)

        # Sensitivity adjustment: in "high" mode, lower the bar for declaring anomaly.
        anomaly_threshold = {"high": 0.35, "normal": 0.50, "low": 0.65}.get(
            self.sensitivity, 0.50
        )
        is_expected = predicted_class in EXPECTED_CLASSES
        is_anomaly = (not is_expected) or (ood > anomaly_threshold)

        priority = self._priority(predicted_class, ood, baseline_deviation)
        # In high-sensitivity mode, bump priority by one level for non-expected classes
        if self.sensitivity == "high" and not is_expected:
            priority = {"low": "medium", "medium": "high", "high": "critical"}.get(
                priority, priority
            )

        action = self._action_label(priority, predicted_class)
        explanation = self._explanation(
            predicted_class,
            confidence,
            ood,
            nearest_class,
            nearest_dist,
            rec_err,
            baseline_deviation,
        )

        return ClassifiedSignal(
            id=new_id("cls_"),
            reading_id=reading.id,
            timestamp=utc_now(),
            predicted_class=predicted_class,
            confidence=confidence,
            embedding=emb_np.tolist(),
            softmax=softmax.tolist(),
            nearest_known_class=nearest_class,
            distance_to_nearest_centroid=nearest_dist,
            reconstruction_error=rec_err,
            ood_score=ood,
            baseline_deviation=baseline_deviation,
            is_anomaly=is_anomaly,
            priority=priority,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            explanation=explanation,
        )
