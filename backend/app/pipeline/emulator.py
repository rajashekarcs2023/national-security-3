"""RF sensor emulator.

Streams synthetic RF spectrograms + the corresponding RFSignalReading events.
Acts exactly like a downstream consumer of an SDR/CASK pipeline would expect:
the rest of the system never knows whether the input is emulated or live.

Two modes:
  - free_run: random class draws weighted to mostly-normal background.
  - scripted: follows a queue of (class, dwell_ticks) instructions from
    pipeline/scenario.py, useful for the demo.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import numpy as np

from app.ml.synth import (
    CLASS_META,
    CLASSES,
    GENERATORS,
    NUM_CLASSES,
    bin_to_mhz,
    estimate_features,
)
from app.ml.radioml import RadioMLPool, RealSample
from app.schemas import RFSignalReading, new_id, utc_now


# Default class weights for free-run mode (background-dominant, occasional anomaly).
DEFAULT_CLASS_WEIGHTS: dict[str, float] = {
    "background_noise": 0.55,
    "friendly_radio_burst": 0.18,
    "commercial_continuous": 0.12,
    "drone_control_repeated_burst": 0.04,
    "frequency_hopping": 0.04,
    "chirp": 0.02,
    "friendly_profile_mismatch": 0.02,
    "unknown_ood": 0.03,
}


@dataclass
class ScriptedStep:
    """One step in a scripted scenario."""

    class_name: str
    dwell_ticks: int = 1
    note: Optional[str] = None  # optional human-readable note for the demo


def quantize_uint8(spec: np.ndarray) -> list[list[int]]:
    arr = np.clip(spec * 255.0, 0, 255).astype(np.uint8)
    return arr.tolist()


class RFEmulator:
    """Async RF feature event generator."""

    def __init__(
        self,
        # Defaults mirror the deployed Foundry edge device record
        # (datfromfoundry/edge_devices.csv :: EDGE-ALPHA-01, Alpha Site - Forward OP, LA).
        site_id: str = "Alpha Site - Forward OP",
        sensor_id: str = "EDGE-ALPHA-01",
        site_lat: float = 34.0522,
        site_lon: float = -118.2437,
        seed: Optional[int] = None,
        real_pool: Optional[RadioMLPool] = None,
        real_data_mix: float = 0.0,
    ):
        self.site_id = site_id
        self.sensor_id = sensor_id
        self.site_lat = site_lat
        self.site_lon = site_lon
        self._rng = np.random.default_rng(seed)
        self._py_rng = random.Random(seed)
        # Scripted mode state
        self._script: deque[ScriptedStep] = deque()
        self._scripted_mode: bool = False
        # Tick counter
        self._ticks: int = 0
        # Real-data injection. When real_pool is provided and the dataset is
        # actually on disk, free-run ticks pull from RadioML with probability
        # real_data_mix ∈ [0, 1]. Scripted scenarios always stay synthetic so
        # demo runs remain deterministic.
        self.real_pool = real_pool
        self.real_data_mix = max(0.0, min(1.0, float(real_data_mix)))

    # ------------------------------------------------------------------
    # Scripted scenario controls
    # ------------------------------------------------------------------
    def load_script(self, steps: list[ScriptedStep]) -> None:
        self._script.clear()
        for s in steps:
            self._script.append(s)
        self._scripted_mode = True

    def clear_script(self) -> None:
        self._script.clear()
        self._scripted_mode = False

    @property
    def has_script(self) -> bool:
        return len(self._script) > 0

    # ------------------------------------------------------------------
    # Single-tick generation
    # ------------------------------------------------------------------
    def _next_class(self) -> tuple[str, Optional[str]]:
        """Pick the next class to emit. Returns (class_name, optional note)."""
        if self._scripted_mode and self._script:
            step = self._script[0]
            note = step.note if step.dwell_ticks == step.dwell_ticks else None  # captured once
            note = step.note
            step.dwell_ticks -= 1
            if step.dwell_ticks <= 0:
                self._script.popleft()
                if not self._script:
                    self._scripted_mode = False
            return step.class_name, note
        # free-run: weighted random
        names = list(DEFAULT_CLASS_WEIGHTS.keys())
        weights = list(DEFAULT_CLASS_WEIGHTS.values())
        choice = self._py_rng.choices(names, weights=weights, k=1)[0]
        return choice, None

    def _maybe_draw_real(self, target_class: Optional[str]) -> Optional[RealSample]:
        """Pull a real RadioML sample if eligible. Scripted ticks stay synthetic."""
        if self._scripted_mode:
            return None
        if self.real_pool is None or not self.real_pool.available:
            return None
        if self.real_data_mix <= 0.0:
            return None
        if self._py_rng.random() >= self.real_data_mix:
            return None
        # When the free-run roll picked a class that has no real-data
        # mapping (e.g. background_noise, frequency_hopping, friendly_profile_mismatch)
        # we fall through to an unconstrained draw rather than skipping the tick —
        # the dashboard will just show the modulation's natural mapping.
        return self.real_pool.sample(target_class) or self.real_pool.sample(None)

    def tick(self) -> tuple[np.ndarray, RFSignalReading, str, Optional[str], dict]:
        """Generate one tick.

        Returns:
            (spectrogram, reading, true_class, scenario_note, source_info).
            source_info["source"] is "synth" or "real". For real samples it
            also carries modulation, snr_db, and threat_label so the
            dashboard can show provenance honestly.
        """
        self._ticks += 1
        class_name, note = self._next_class()

        real = self._maybe_draw_real(class_name)
        if real is not None:
            # Use the dataset's modulation→threat mapping for tactical
            # framing, but feed the real spectrogram into the pipeline.
            class_name = real.threat_class
            spec = real.spec
            source_info: dict = {
                "source": "real",
                "modulation": real.modulation,
                "snr_db": real.snr_db,
                "threat_label": real.threat_label,
            }
        else:
            class_idx = CLASSES.index(class_name)
            spec = GENERATORS[class_idx](self._rng)
            source_info = {"source": "synth"}

        # Pass class_name so the emitted metadata uses realistic tactical bands
        # (2412 MHz DJI, 5745 MHz swarm, 144-148 MHz VHF, ...). The spectrogram
        # itself is untouched — the CNN sees the same bin structure it was trained on.
        feats = estimate_features(spec, class_name=class_name)
        reading = RFSignalReading(
            id=new_id("rd_"),
            timestamp=utc_now(),
            sensor_id=self.sensor_id,
            site_id=self.site_id,
            lat=self.site_lat + float(self._rng.normal(0, 0.0005)),
            lon=self.site_lon + float(self._rng.normal(0, 0.0005)),
            center_frequency_mhz=feats["center_frequency_mhz"],
            bandwidth_khz=feats["bandwidth_khz"],
            power_dbm=feats["power_dbm"],
            duration_ms=feats["duration_ms"],
            burst_pattern=feats["burst_pattern"],
            dominant_freq_bin=feats["dominant_freq_bin"],
            energy=feats["energy"],
            raw_source="radioml" if source_info["source"] == "real" else "emulator",
            spectrogram_u8=quantize_uint8(spec),
        )
        return spec, reading, class_name, note, source_info

    # ------------------------------------------------------------------
    # Async stream
    # ------------------------------------------------------------------
    async def stream(
        self,
        period_seconds: float = 0.9,
        stop_event: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[tuple[np.ndarray, RFSignalReading, str, Optional[str], dict]]:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            yield self.tick()
            await asyncio.sleep(period_seconds)
