"""One-shot training script for the SpectrumCustody edge model.

Generates synthetic RF spectrograms, trains the combined classifier +
autoencoder, computes per-class embedding centroids and reconstruction-error
statistics for runtime OOD scoring, and saves all artefacts to backend/data/.

Run from the backend/ directory:

    python train.py

Trains in ~30-60s on a laptop CPU. No GPU required. Colab/H100 also works
(useful only if you scale n_per_class >> 500 or epochs >> 10).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

from app.ml.model import SpectrumModel
from app.ml.synth import CLASSES, NUM_CLASSES, generate_dataset


DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

WEIGHTS_PATH = DATA_DIR / "weights.pt"
CENTROIDS_PATH = DATA_DIR / "centroids.npy"
META_PATH = DATA_DIR / "meta.json"


def train(
    n_per_class: int = 600,
    epochs: int = 12,
    batch_size: int = 64,
    lr: float = 1e-3,
    embed_dim: int = 32,
    cls_weight: float = 1.0,
    rec_weight: float = 0.5,
    seed: int = 42,
) -> dict:
    torch.manual_seed(seed)
    np.random.seed(seed)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[train] device={device}")

    print(f"[train] generating {n_per_class * NUM_CLASSES} synthetic spectrograms ...")
    t0 = time.time()
    X, y = generate_dataset(n_per_class=n_per_class, seed=seed)
    print(f"[train] data ready in {time.time() - t0:.1f}s — X={X.shape}, y={y.shape}")

    X_t = torch.from_numpy(X)
    y_t = torch.from_numpy(y)

    # 85/15 split
    g = torch.Generator().manual_seed(seed)
    perm = torch.randperm(len(X_t), generator=g)
    split = int(0.85 * len(X_t))
    X_train, y_train = X_t[perm[:split]], y_t[perm[:split]]
    X_val, y_val = X_t[perm[split:]], y_t[perm[split:]]

    train_loader = DataLoader(
        TensorDataset(X_train, y_train),
        batch_size=batch_size,
        shuffle=True,
    )

    model = SpectrumModel(num_classes=NUM_CLASSES, embed_dim=embed_dim).to(device)
    summary = model.parameter_summary()
    print(
        f"[train] model: {summary['total_params']:,} params  "
        f"(~{summary['size_fp32_bytes'] / 1024:.1f} KB fp32, "
        f"~{summary['size_int8_bytes'] / 1024:.1f} KB int8)"
    )

    opt = optim.Adam(model.parameters(), lr=lr)
    cls_loss_fn = nn.CrossEntropyLoss()
    rec_loss_fn = nn.MSELoss()

    t0 = time.time()
    for epoch in range(1, epochs + 1):
        model.train()
        agg_cls, agg_rec, n_seen = 0.0, 0.0, 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            logits, _emb, recon = model(xb)
            l_cls = cls_loss_fn(logits, yb)
            l_rec = rec_loss_fn(recon, xb)
            loss = cls_weight * l_cls + rec_weight * l_rec
            loss.backward()
            opt.step()
            agg_cls += l_cls.item() * xb.size(0)
            agg_rec += l_rec.item() * xb.size(0)
            n_seen += xb.size(0)

        model.eval()
        with torch.no_grad():
            logits_v, _, recon_v = model(X_val.to(device))
            val_acc = (logits_v.argmax(1) == y_val.to(device)).float().mean().item()
            val_rec = ((recon_v - X_val.to(device)) ** 2).mean().item()
        print(
            f"[train] epoch {epoch:2d}/{epochs}  "
            f"cls={agg_cls / n_seen:.4f}  "
            f"rec={agg_rec / n_seen:.4f}  "
            f"val_acc={val_acc:.3f}  "
            f"val_rec={val_rec:.4f}"
        )
    train_secs = time.time() - t0
    print(f"[train] trained in {train_secs:.1f}s")

    # ------------------------------------------------------------------
    # Compute artefacts for runtime OOD scoring.
    # ------------------------------------------------------------------
    model.eval()
    with torch.no_grad():
        embeddings = model.encode(X_train.to(device)).cpu().numpy()
        _, _, recon_train = model(X_train.to(device))
        per_sample_rec_err = (
            ((recon_train - X_train.to(device)) ** 2).mean(dim=(1, 2, 3)).cpu().numpy()
        )

    centroids = np.zeros((NUM_CLASSES, embed_dim), dtype=np.float32)
    rec_err_per_class = []
    for c in range(NUM_CLASSES):
        mask = y_train.numpy() == c
        centroids[c] = embeddings[mask].mean(axis=0)
        rec_err_per_class.append(
            {
                "class": CLASSES[c],
                "mean": float(per_sample_rec_err[mask].mean()),
                "std": float(per_sample_rec_err[mask].std()),
            }
        )

    rec_err_mean = float(per_sample_rec_err.mean())
    rec_err_std = float(per_sample_rec_err.std())
    rec_err_p95 = float(np.percentile(per_sample_rec_err, 95))

    # Save
    torch.save(model.state_dict(), WEIGHTS_PATH)
    np.save(CENTROIDS_PATH, centroids)
    meta = {
        "classes": CLASSES,
        "num_classes": NUM_CLASSES,
        "embed_dim": embed_dim,
        "input_size": 64,
        "val_acc": val_acc,
        "val_rec_loss": val_rec,
        "rec_err_mean": rec_err_mean,
        "rec_err_std": rec_err_std,
        "rec_err_p95": rec_err_p95,
        "rec_err_per_class": rec_err_per_class,
        "train_seconds": train_secs,
        "param_summary": summary,
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"[train] saved weights   -> {WEIGHTS_PATH}")
    print(f"[train] saved centroids -> {CENTROIDS_PATH}")
    print(f"[train] saved meta      -> {META_PATH}")
    print(f"[train] DONE  val_acc={val_acc:.3f}")

    return meta


if __name__ == "__main__":
    train()
