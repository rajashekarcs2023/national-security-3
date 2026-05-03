"""RadioML 2016.10A loader.

Pulls REAL I/Q recordings from the DeepSig open dataset, converts them to
64x64 magnitude spectrograms (the same shape our CNN expects), and exposes
a sample pool that the emulator can mix into the live stream.

------------------------------------------------------------
WHY THIS MATTERS — read this before changing anything below
------------------------------------------------------------
Our CNN was trained on *structural* synthetic patterns (rectangles, hops,
chirps overlaid on Gaussian noise). RadioML samples are real I/Q
recordings whose magnitude spectrograms have a very different texture —
they are genuine out-of-distribution (OOD) input from the model's
perspective.

We use this dataset for two things:

  1. **Credibility.** Judges can see we run real-world recordings through
     the same edge pipeline rather than only test-on-train.
  2. **Live OOD demo.** Real samples should trigger the open-set detector
     (autoencoder reconstruction-error spike). This is the system working
     as designed, not a failure — the operator brief will say "I don't
     recognise this signal, escalate" instead of confidently
     mis-labelling it.

The mapping table below is for *ground-truth display only* ("the dataset
calls this GFSK, which we treat as drone-like for tactical context").
The live classifier prediction is computed independently from the
spectrogram and may disagree — and when it disagrees by escalating to
unknown_ood, that is the OOD layer earning its keep.
"""

from __future__ import annotations

import os
import pickle
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


# Default location for the extracted RadioML 2016.10A pickle. Override via
# the RADIOML_PATH env var if you keep the dataset elsewhere.
_DEFAULT = (
    Path(__file__).resolve().parents[3]
    / "datasets"
    / "radioml2016"
    / "RML2016.10a_dict_optimized.pkl"
)
DEFAULT_RADIOML_PATH = Path(os.environ.get("RADIOML_PATH", _DEFAULT))


# RadioML modulation -> (our threat class, operator-language justification).
# Used ONLY for the ground-truth display label shown next to a real sample
# in the dashboard. The classifier's prediction is independent of this map.
MODULATION_TO_THREAT: dict[str, tuple[str, str]] = {
    "AM-DSB": ("friendly_radio_burst", "Analog AM voice — military VHF tactical"),
    "AM-SSB": ("friendly_radio_burst", "Analog AM voice — narrowband"),
    "WBFM":   ("friendly_radio_burst", "Wideband FM — voice / public safety"),
    "QAM16":  ("commercial_continuous", "16-QAM — LTE / WiFi-class digital"),
    "QAM64":  ("commercial_continuous", "64-QAM — LTE / WiFi-class digital"),
    "QPSK":   ("commercial_continuous", "QPSK — generic digital comms"),
    "8PSK":   ("commercial_continuous", "8-PSK — generic digital comms"),
    "BPSK":   ("unknown_ood",           "BPSK — uncommon in tactical RF"),
    "PAM4":   ("unknown_ood",           "PAM-4 — uncommon in tactical RF"),
    "CPFSK":  ("chirp",                 "CPFSK — continuous-phase, sweep-like"),
    "GFSK":   ("drone_control_repeated_burst", "GFSK — DJI / Bluetooth-class FHSS-like"),
}


# We sample only mid-to-high SNR pairs. -20..-4 dB is pure noise even in
# academic benchmarks; using it would just clog the demo with garbage.
DEMO_SNR_RANGE: tuple[int, int] = (0, 18)


@dataclass
class RealSample:
    """One real RadioML sample, post-STFT, ready to drop into the pipeline."""

    spec: np.ndarray              # (n_freq, n_time) float32 in [0, 1]
    modulation: str               # RadioML label (e.g. "GFSK")
    snr_db: int                   # RadioML SNR (e.g. 8)
    threat_class: str             # mapped tactical class for ground-truth display
    threat_label: str             # human-readable mapping rationale


class RadioMLPool:
    """In-memory pool of real-RF spectrograms keyed by (modulation, SNR).

    The pickle is loaded lazily on the first sample request so the backend
    boots fast even when the dataset isn't installed.
    """

    def __init__(
        self,
        path: Path = DEFAULT_RADIOML_PATH,
        n_freq: int = 64,
        n_time: int = 64,
        seed: Optional[int] = None,
    ) -> None:
        self.path = Path(path)
        self.n_freq = n_freq
        self.n_time = n_time
        self._data: Optional[dict] = None
        self._lock = threading.Lock()
        self._rng = np.random.default_rng(seed)

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """True iff the dataset file exists on disk."""
        return self.path.exists()

    @property
    def loaded(self) -> bool:
        return self._data is not None

    def stats(self) -> dict:
        """Lightweight metadata for the dashboard / health endpoint."""
        info: dict = {
            "available": self.available,
            "loaded": self.loaded,
            "path": str(self.path),
        }
        if self._data is not None:
            info["n_keys"] = len(self._data)
            info["modulations"] = sorted({k[0] for k in self._data.keys()})
            info["snrs"] = sorted({k[1] for k in self._data.keys()})
        return info

    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._data is not None:
            return
        with self._lock:
            if self._data is not None:
                return
            with open(self.path, "rb") as f:
                # RadioML pickles are protocol-0/1 from Python 2; latin1
                # decoding is the canonical workaround for Python 3.
                self._data = pickle.load(f, encoding="latin1")

    # ------------------------------------------------------------------
    def _iq_to_spectrogram(self, iq_examples: np.ndarray) -> np.ndarray:
        """Concatenate consecutive 128-sample I/Q examples and STFT them.

        Args:
            iq_examples: shape (k, 2, 128) — RadioML's native layout where
                axis-1 is [I, Q] and axis-2 is time samples.

        Returns:
            Magnitude spectrogram of shape (n_freq, n_time), float32 in [0, 1].
            Range is normalised per-sample to roughly match the synthetic
            generator's distribution so the same uint8 quantiser works.
        """
        n_per = iq_examples.shape[2]                       # 128
        needed = self.n_time * self.n_freq                  # e.g. 4096
        n_ex = (needed + n_per - 1) // n_per                # ~32 examples
        if iq_examples.shape[0] < n_ex:
            reps = n_ex // iq_examples.shape[0] + 1
            iq_examples = np.tile(iq_examples, (reps, 1, 1))

        # Pick a random consecutive run for variety across calls.
        max_start = iq_examples.shape[0] - n_ex
        start = int(self._rng.integers(0, max_start + 1)) if max_start > 0 else 0
        chunk = iq_examples[start : start + n_ex]

        # I + jQ -> complex baseband, flatten to one long sample stream.
        iq_complex = chunk[:, 0, :] + 1j * chunk[:, 1, :]
        iq_flat = iq_complex.reshape(-1)[:needed]

        # No-overlap STFT: n_time hops of n_freq samples each. fftshift puts
        # DC in the middle so positive/negative frequencies are intuitive.
        windows = iq_flat.reshape(self.n_time, self.n_freq)
        hann = np.hanning(self.n_freq).astype(np.complex64)
        windows = windows * hann[None, :]
        spectrum = np.fft.fftshift(np.fft.fft(windows, axis=1), axes=1)
        magnitude = np.abs(spectrum).T                     # -> (n_freq, n_time)

        # Compress dynamic range, then normalise per-sample. Cap at 0.85 so
        # the brightness profile lands in the same ballpark as our synth
        # spectrograms (which peak ~0.6-0.8 above a ~0.1 noise floor).
        log_mag = np.log1p(magnitude)
        lo, hi = float(log_mag.min()), float(log_mag.max())
        if hi - lo > 1e-9:
            log_mag = (log_mag - lo) / (hi - lo)
        return np.clip(log_mag * 0.85, 0.0, 1.0).astype(np.float32)

    # ------------------------------------------------------------------
    def _candidate_keys(self, threat_class: Optional[str] = None) -> list[tuple[str, int]]:
        """Pickle keys filtered to the demo SNR range and (optionally) class."""
        self._ensure_loaded()
        assert self._data is not None
        snr_lo, snr_hi = DEMO_SNR_RANGE
        keys = [k for k in self._data.keys() if snr_lo <= k[1] <= snr_hi]
        if threat_class is not None:
            allowed = {m for m, (c, _) in MODULATION_TO_THREAT.items() if c == threat_class}
            keys = [k for k in keys if k[0] in allowed]
        return keys

    # ------------------------------------------------------------------
    def sample(self, threat_class: Optional[str] = None) -> Optional[RealSample]:
        """Draw one real spectrogram.

        Args:
            threat_class: if given, restrict to modulations whose mapping
                produces this tactical class. Used by scripted scenarios
                that want a real signal for a specific demo beat.

        Returns:
            A RealSample, or None if the dataset is unavailable, the
            pickle is corrupt, or no candidate keys match the filter.
        """
        if not self.available:
            return None
        try:
            keys = self._candidate_keys(threat_class)
            if not keys:
                return None
            mod, snr = keys[int(self._rng.integers(0, len(keys)))]
            iq_examples = self._data[(mod, snr)]  # type: ignore[index]
            spec = self._iq_to_spectrogram(iq_examples)
            cls, label = MODULATION_TO_THREAT.get(
                mod, ("unknown_ood", f"{mod} (no tactical mapping)")
            )
            return RealSample(
                spec=spec,
                modulation=mod,
                snr_db=int(snr),
                threat_class=cls,
                threat_label=label,
            )
        except Exception:
            # Never crash the live pipeline because of a bad RadioML key.
            return None
