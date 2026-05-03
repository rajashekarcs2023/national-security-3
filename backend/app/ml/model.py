"""Tiny edge spectrum model.

Combined CNN classifier + autoencoder. Encoder is shared so a single forward
pass yields:
  - logits over signal classes
  - embedding vector (used for OOD / nearest-known-profile distance)
  - reconstruction (used for OOD reconstruction error)

Designed to be small enough to run on edge hardware (~150K params, <1MB FP32,
<300KB quantised). Trains on synthetic spectrograms in seconds on CPU.
"""

from __future__ import annotations

import torch
import torch.nn as nn


EMBED_DIM_DEFAULT = 32
INPUT_SIZE_DEFAULT = 64


class SpectrumModel(nn.Module):
    """Shared-encoder classifier + autoencoder for 64x64 RF spectrograms."""

    def __init__(self, num_classes: int = 8, embed_dim: int = EMBED_DIM_DEFAULT):
        super().__init__()
        self.num_classes = num_classes
        self.embed_dim = embed_dim

        # ---- Encoder: 1x64x64 -> embed_dim ----
        self.encoder_conv = nn.Sequential(
            nn.Conv2d(1, 8, kernel_size=3, stride=2, padding=1),    # 8x32x32
            nn.ReLU(inplace=True),
            nn.Conv2d(8, 16, kernel_size=3, stride=2, padding=1),   # 16x16x16
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),  # 32x8x8
            nn.ReLU(inplace=True),
        )
        self.encoder_fc = nn.Linear(32 * 8 * 8, embed_dim)

        # ---- Classifier head: embed_dim -> num_classes ----
        self.classifier = nn.Linear(embed_dim, num_classes)

        # ---- Decoder: embed_dim -> 1x64x64 ----
        self.decoder_fc = nn.Linear(embed_dim, 32 * 8 * 8)
        self.decoder_deconv = nn.Sequential(
            nn.ConvTranspose2d(32, 16, kernel_size=4, stride=2, padding=1),  # 16x16x16
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(16, 8, kernel_size=4, stride=2, padding=1),   # 8x32x32
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(8, 1, kernel_size=4, stride=2, padding=1),    # 1x64x64
            nn.Sigmoid(),
        )

    # ---------------------------------------------------------------------
    # Building-block forward methods
    # ---------------------------------------------------------------------

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        h = self.encoder_conv(x)
        h = h.flatten(1)
        return self.encoder_fc(h)

    def decode(self, emb: torch.Tensor) -> torch.Tensor:
        h = self.decoder_fc(emb)
        h = h.view(-1, 32, 8, 8)
        return self.decoder_deconv(h)

    def classify(self, emb: torch.Tensor) -> torch.Tensor:
        return self.classifier(emb)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Returns (logits, embedding, reconstruction)."""
        emb = self.encode(x)
        logits = self.classify(emb)
        recon = self.decode(emb)
        return logits, emb, recon

    # ---------------------------------------------------------------------
    # Inspection helpers
    # ---------------------------------------------------------------------

    def num_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters())

    def parameter_summary(self) -> dict:
        total = self.num_parameters()
        return {
            "total_params": total,
            "size_fp32_bytes": total * 4,
            "size_int8_bytes": total,
            "embed_dim": self.embed_dim,
            "num_classes": self.num_classes,
        }
