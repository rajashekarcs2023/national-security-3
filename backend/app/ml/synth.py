"""Synthetic RF spectrogram generator.

Generates 64x64 spectrogram-like matrices for 8 signal families. Each sample
includes Gaussian noise floor + class-specific structure (bursts, hopping,
chirps, etc.) with random offsets, jitter, power variation, and bandwidth.

Used for both:
  - training (offline, in train.py)
  - the live RF emulator at runtime (pipeline/emulator.py)
"""

from __future__ import annotations

import numpy as np

# Spectrogram dimensions: frequency bins x time steps.
# The spectrograms the CNN sees are *structure-preserving synthetic patterns*
# (bursts, hops, chirps, noise floor) laid out across 64 frequency bins and
# 64 time steps. The bins are abstract — the per-class REAL center frequency
# (2412 MHz DJI, 5745 MHz swarm, 144-148 MHz VHF tactical, etc.) is attached
# to the emitted RFSignalReading via CLASS_TO_REAL_FREQ_RANGE_MHZ below. See
# `estimate_features(spec, class_name=...)` for how the mapping is applied.
N_FREQ = 64
N_TIME = 64

CLASSES = [
    "background_noise",
    "friendly_radio_burst",
    "commercial_continuous",
    "drone_control_repeated_burst",
    "frequency_hopping",
    "chirp",
    "friendly_profile_mismatch",
    "unknown_ood",
]

NUM_CLASSES = len(CLASSES)

# Friendly emitter library (mirrors what the action engine references).
FRIENDLY_FREQ_BIN_RANGE = (24, 32)  # Blue-2 lives at bins 24-32

# Per-class metadata used to populate RF feature events alongside the spectrogram.
CLASS_META = {
    "background_noise":               {"label": "Normal background", "expected": True,  "default_band": (0, 64)},
    "friendly_radio_burst":           {"label": "Friendly radio burst (Blue-2)", "expected": True, "default_band": (24, 32)},
    "commercial_continuous":          {"label": "Commercial-like continuous", "expected": True, "default_band": (8, 16)},
    "drone_control_repeated_burst":   {"label": "Drone-control-like repeated burst", "expected": False, "default_band": (38, 48)},
    "frequency_hopping":              {"label": "Frequency-hopping unknown", "expected": False, "default_band": (5, 60)},
    "chirp":                          {"label": "Chirp / sweep", "expected": False, "default_band": (10, 55)},
    "friendly_profile_mismatch":      {"label": "Friendly profile mismatch", "expected": False, "default_band": (50, 60)},
    "unknown_ood":                    {"label": "Unknown out-of-distribution pattern", "expected": False, "default_band": (0, 64)},
}


def _noise_floor(rng: np.random.Generator, mean: float = 0.10, std: float = 0.04) -> np.ndarray:
    return rng.normal(mean, std, (N_FREQ, N_TIME))


def _add_band(spec: np.ndarray, f_lo: int, f_hi: int, t_lo: int, t_hi: int, power: float) -> None:
    f_lo = max(0, f_lo)
    f_hi = min(N_FREQ, f_hi)
    t_lo = max(0, t_lo)
    t_hi = min(N_TIME, t_hi)
    if f_hi > f_lo and t_hi > t_lo:
        spec[f_lo:f_hi, t_lo:t_hi] += power


def _normalize(spec: np.ndarray) -> np.ndarray:
    spec = np.clip(spec, 0.0, None)
    m = spec.max()
    if m > 1e-8:
        spec = spec / m
    return spec.astype(np.float32)


# ---------------------------------------------------------------------------
# Per-class generators
# ---------------------------------------------------------------------------

def gen_background_noise(rng: np.random.Generator) -> np.ndarray:
    spec = _noise_floor(rng)
    # Optional very faint broadband activity
    if rng.random() < 0.3:
        spec += rng.normal(0.02, 0.01, spec.shape)
    return _normalize(spec)


def gen_friendly_radio_burst(rng: np.random.Generator) -> np.ndarray:
    """Short narrowband burst inside the friendly Blue-2 band (bins 24-32)."""
    spec = _noise_floor(rng)
    f_center = int(rng.integers(FRIENDLY_FREQ_BIN_RANGE[0] + 1, FRIENDLY_FREQ_BIN_RANGE[1] - 1))
    f_w = int(rng.integers(1, 3))
    t_start = int(rng.integers(15, 45))
    t_dur = int(rng.integers(3, 9))
    power = float(rng.uniform(0.55, 0.9))
    _add_band(spec, f_center - f_w, f_center + f_w, t_start, t_start + t_dur, power)
    return _normalize(spec)


def gen_commercial_continuous(rng: np.random.Generator) -> np.ndarray:
    """Continuous narrow signal at low band (commercial-like)."""
    spec = _noise_floor(rng)
    f_center = int(rng.integers(8, 16))
    f_w = int(rng.integers(1, 3))
    power = float(rng.uniform(0.30, 0.55))
    _add_band(spec, f_center - f_w, f_center + f_w, 0, N_TIME, power)
    # Slight power jitter over time
    jitter = rng.normal(0.0, 0.04, (1, N_TIME))
    spec[f_center - f_w:f_center + f_w, :] += jitter
    return _normalize(spec)


def gen_drone_control_repeated_burst(rng: np.random.Generator) -> np.ndarray:
    """Periodic short bursts in drone-control band (bins 38-48)."""
    spec = _noise_floor(rng)
    f_center = int(rng.integers(40, 47))
    f_w = int(rng.integers(2, 4))
    n_bursts = int(rng.integers(4, 8))
    period = N_TIME // (n_bursts + 1)
    burst_dur = max(2, period // 4)
    base_power = float(rng.uniform(0.55, 0.85))
    for i in range(n_bursts):
        t = (i + 1) * period + int(rng.integers(-2, 3))
        _add_band(spec, f_center - f_w, f_center + f_w,
                  t, t + burst_dur,
                  base_power + float(rng.normal(0, 0.05)))
    return _normalize(spec)


def gen_frequency_hopping(rng: np.random.Generator) -> np.ndarray:
    """Frequency hopping: random band each time slice."""
    spec = _noise_floor(rng)
    n_hops = int(rng.integers(6, 13))
    hop_dur = max(2, N_TIME // n_hops)
    for i in range(n_hops):
        t_start = i * hop_dur
        t_end = min(N_TIME, t_start + hop_dur)
        f_center = int(rng.integers(5, 60))
        f_w = int(rng.integers(1, 3))
        _add_band(spec, f_center - f_w, f_center + f_w,
                  t_start, t_end,
                  float(rng.uniform(0.5, 0.85)))
    return _normalize(spec)


def gen_chirp(rng: np.random.Generator) -> np.ndarray:
    """Linear frequency sweep over time."""
    spec = _noise_floor(rng)
    direction = int(rng.choice([-1, 1]))
    if direction > 0:
        f_start, f_end = int(rng.integers(8, 25)), int(rng.integers(40, 58))
    else:
        f_start, f_end = int(rng.integers(40, 58)), int(rng.integers(8, 25))
    f_w = int(rng.integers(1, 3))
    power = float(rng.uniform(0.55, 0.85))
    for t in range(N_TIME):
        f = int(f_start + (f_end - f_start) * t / N_TIME)
        _add_band(spec, f - f_w, f + f_w, t, t + 1, power)
    return _normalize(spec)


def gen_friendly_profile_mismatch(rng: np.random.Generator) -> np.ndarray:
    """Burst that *looks* like a friendly emission but at the wrong band.

    This is the "possible spoof / mis-tuned blue force" class.
    """
    spec = _noise_floor(rng)
    # Same burst shape as friendly, but bands chosen OUTSIDE the friendly window
    if rng.random() < 0.5:
        f_center = int(rng.integers(2, FRIENDLY_FREQ_BIN_RANGE[0] - 4))
    else:
        f_center = int(rng.integers(FRIENDLY_FREQ_BIN_RANGE[1] + 4, 60))
    f_w = int(rng.integers(1, 3))
    t_start = int(rng.integers(15, 45))
    t_dur = int(rng.integers(3, 9))
    _add_band(spec, f_center - f_w, f_center + f_w,
              t_start, t_start + t_dur,
              float(rng.uniform(0.55, 0.9)))
    return _normalize(spec)


def gen_unknown_ood(rng: np.random.Generator) -> np.ndarray:
    """Genuinely unusual / OOD pattern. Mix of structures unseen in known classes."""
    spec = _noise_floor(rng)
    pattern = int(rng.integers(0, 4))
    if pattern == 0:
        # Multi-tone harmonic stack
        for _ in range(int(rng.integers(3, 6))):
            f = int(rng.integers(0, 60))
            f_w = int(rng.integers(1, 3))
            _add_band(spec, f - f_w, f + f_w, 0, N_TIME,
                      float(rng.uniform(0.30, 0.55)))
    elif pattern == 1:
        # Wideband impulsive pulse
        t_start = int(rng.integers(15, 45))
        t_end = t_start + int(rng.integers(2, 6))
        _add_band(spec, 0, N_FREQ, t_start, t_end,
                  float(rng.uniform(0.35, 0.6)))
    elif pattern == 2:
        # Diagonal grid (sweep + repeat)
        n_diag = int(rng.integers(3, 6))
        for i in range(n_diag):
            offset = i * (N_FREQ // n_diag)
            for t in range(N_TIME):
                f = (t + offset) % N_FREQ
                _add_band(spec, f, f + 1, t, t + 1,
                          float(rng.uniform(0.4, 0.65)))
    else:
        # Multiple overlapping chirps with crossings
        for _ in range(int(rng.integers(2, 4))):
            f_start = int(rng.integers(0, 60))
            f_end = int(rng.integers(0, 60))
            f_w = int(rng.integers(1, 2))
            for t in range(N_TIME):
                f = int(f_start + (f_end - f_start) * t / N_TIME) % N_FREQ
                _add_band(spec, f - f_w, f + f_w, t, t + 1,
                          float(rng.uniform(0.30, 0.55)))
    return _normalize(spec)


GENERATORS = [
    gen_background_noise,
    gen_friendly_radio_burst,
    gen_commercial_continuous,
    gen_drone_control_repeated_burst,
    gen_frequency_hopping,
    gen_chirp,
    gen_friendly_profile_mismatch,
    gen_unknown_ood,
]


def generate_one(class_idx: int, rng: np.random.Generator | None = None) -> np.ndarray:
    """Generate a single 64x64 spectrogram for a given class index."""
    if rng is None:
        rng = np.random.default_rng()
    return GENERATORS[class_idx](rng)


def generate_dataset(n_per_class: int = 500, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """Generate a labelled training set.

    Returns:
        X: (N, 1, 64, 64) float32 — channel-first spectrograms in [0, 1]
        y: (N,) int64 — class indices
    """
    rng = np.random.default_rng(seed)
    Xs: list[np.ndarray] = []
    ys: list[int] = []
    for label in range(NUM_CLASSES):
        for _ in range(n_per_class):
            Xs.append(GENERATORS[label](rng))
            ys.append(label)
    X = np.stack(Xs)[:, None, :, :]  # add channel dim
    y = np.asarray(ys, dtype=np.int64)
    return X, y


# ---------------------------------------------------------------------------
# RF feature event helpers (used by the live emulator).
# A spectrogram alone isn't enough — we also produce the ontology-shaped
# feature row that the rest of the pipeline consumes.
# ---------------------------------------------------------------------------

# Realistic tactical frequency allocations per class. These match the bands
# seen in the deployed Palantir Foundry datasets (datfromfoundry/sensor_events.csv):
#   2412 MHz = 2.4 GHz DJI control / WiFi
#   5745 MHz = 5.8 GHz drone swarm / high-speed DJI
#   144-148 MHz = VHF tactical man-pack radio (friendlies)
#   225-400 MHz = UHF military
# The CNN was trained on structural patterns (burst shapes), NOT on absolute
# frequency. So we preserve the bin-based spectrograms for the model but
# override the *metadata* we publish so operators see realistic MHz values.
CLASS_TO_REAL_FREQ_RANGE_MHZ: dict[str, tuple[float, float]] = {
    "background_noise":               (100.0, 6000.0),    # could be anywhere
    "friendly_radio_burst":           (144.0, 148.0),     # VHF tactical (Blue-2 man-pack)
    "commercial_continuous":          (2400.0, 2484.0),   # 2.4 GHz WiFi / ISM
    "drone_control_repeated_burst":   (2400.0, 2484.0),   # 2.4 GHz DJI control (2412 MHz = WiFi ch 1)
    "frequency_hopping":              (5725.0, 5850.0),   # 5.8 GHz swarm / DJI-HD
    "chirp":                          (1030.0, 1090.0),   # L-band radar-like
    "friendly_profile_mismatch":      (330.0, 370.0),     # VHF-shape but wrong band (possible spoof)
    "unknown_ood":                    (100.0, 6000.0),    # anywhere — it's OOD
}

CLASS_TO_TYPICAL_BW_KHZ: dict[str, int] = {
    "background_noise":              0,
    "friendly_radio_burst":          25,       # narrow VHF FM
    "commercial_continuous":         20000,    # WiFi 20 MHz channel
    "drone_control_repeated_burst":  10000,    # DJI OcuSync hopping channel
    "frequency_hopping":             20000,    # wide hopping envelope
    "chirp":                         1000,     # swept narrow
    "friendly_profile_mismatch":     25,       # same shape as friendly
    "unknown_ood":                   5000,     # varies
}


def bin_to_mhz(freq_bin: int) -> float:
    """Generic bin-to-MHz mapping used as a fallback when class is unknown.

    Spans 30 MHz (bin 0) to 6 GHz (bin 63) for realistic coverage of HF
    through C-band. Only used when we don't know the emitting class (e.g.
    early in training or for OOD samples where any band is plausible).
    """
    f = 30.0 + (6000.0 - 30.0) * (freq_bin / max(1, N_FREQ - 1))
    return round(float(f), 2)


def real_frequency_for_class(class_name: str, rng: np.random.Generator | None = None) -> float:
    """Sample a realistic center frequency (MHz) for a given signal class."""
    if rng is None:
        rng = np.random.default_rng()
    band = CLASS_TO_REAL_FREQ_RANGE_MHZ.get(class_name)
    if band is None:
        return bin_to_mhz(32)  # mid-band fallback
    lo, hi = band
    return round(float(rng.uniform(lo, hi)), 2)


def estimate_features(spec: np.ndarray, class_name: str | None = None) -> dict:
    """Estimate compact RF features from a spectrogram window.

    When ``class_name`` is provided, publish a *realistic* center frequency
    and bandwidth for that class (matching the bands seen in real tactical
    systems + the Palantir Foundry datasets). When not provided, fall back
    to the generic bin-to-MHz mapping.

    The spectrogram itself is not modified — this only affects the structured
    metadata row attached to the reading.
    """
    # Time-collapsed power per frequency bin
    f_power = spec.mean(axis=1)
    # Frequency-collapsed power per time step
    t_power = spec.mean(axis=0)

    # Dominant frequency bin (most energetic)
    dominant_bin = int(f_power.argmax())
    # Bandwidth: number of bins above 50% of peak
    peak = float(f_power.max())
    bw_bins = int((f_power >= 0.5 * peak).sum()) if peak > 0 else 0
    # Burst pattern guess from time-power autocorrelation/energy variation
    energy = float(spec.sum())
    t_var = float(t_power.var())
    if t_var < 1e-3 and energy > 5.0:
        burst_pattern = "continuous"
    elif t_var > 0.02:
        # Multiple energetic time chunks
        peaks = (t_power > 0.5 * t_power.max()).astype(int)
        # Count rising edges
        edges = int(((peaks[1:] - peaks[:-1]) > 0).sum())
        burst_pattern = "repeated_short_bursts" if edges >= 3 else "single_burst"
    else:
        burst_pattern = "single_burst" if peak > 0.4 else "none"

    # Estimated power in dBm from normalized peak (synthetic mapping)
    # 1.0 -> -30 dBm, 0.0 -> -90 dBm.
    power_dbm = round(-90.0 + 60.0 * peak, 1)

    # Pick the frequency + bandwidth to publish:
    if class_name and class_name in CLASS_TO_REAL_FREQ_RANGE_MHZ:
        # Realistic frequency band for this class. Jitter within the band
        # using the dominant bin as a seed for reproducibility.
        lo, hi = CLASS_TO_REAL_FREQ_RANGE_MHZ[class_name]
        center_frequency_mhz = round(lo + (hi - lo) * (dominant_bin / max(1, N_FREQ - 1)), 2)
        # Realistic bandwidth for this class, modulated by the actual peak width.
        typical_bw = CLASS_TO_TYPICAL_BW_KHZ.get(class_name, bw_bins * 250)
        bandwidth_khz = int(max(5, typical_bw * max(0.5, bw_bins / 8.0)))
    else:
        center_frequency_mhz = bin_to_mhz(dominant_bin)
        bandwidth_khz = int(round(bw_bins * 250))

    return {
        "center_frequency_mhz": center_frequency_mhz,
        "bandwidth_khz": bandwidth_khz,
        "power_dbm": power_dbm,
        "duration_ms": int(round(900 * (1.0 - t_var))) if burst_pattern != "continuous" else 1500,
        "burst_pattern": burst_pattern,
        "dominant_freq_bin": dominant_bin,
        "energy": round(energy, 3),
    }
