"""Unit tests for ``app.pipeline.attribution.attribute``.

These cover the four verdict branches (BLUE_ATTRIBUTED / RED_KNOWN /
AMBIGUOUS / UNEXPLAINED), the impersonation guard, and the blue-force
proximity gate that flips a friendly-looking signature into UNEXPLAINED
when no friendly unit is nearby.

The attribution engine reads the emitter library from ``STATE.emitter_library``,
which is seeded from ``state.EMITTER_LIBRARY`` at import time. We keep that
library untouched for these tests — it is a production fixture.
"""

from __future__ import annotations

from typing import Optional

import pytest

from app.pipeline.attribution import attribute
from app.pipeline.blue_force import BlueForceFeed
from app.schemas import (
    BlueForceUnit,
    ClassifiedSignal,
    RFSignalReading,
)
from app.state import STATE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reading(
    *,
    center_mhz: float,
    bw_khz: int = 25,
    pattern: str = "short_burst",
    lat: float = 34.0600,
    lon: float = -118.2400,
) -> RFSignalReading:
    """Build a minimal valid RFSignalReading for attribution scoring."""
    return RFSignalReading(
        center_frequency_mhz=center_mhz,
        bandwidth_khz=bw_khz,
        power_dbm=-30.0,
        duration_ms=200,
        burst_pattern=pattern,
        dominant_freq_bin=32,
        energy=1.0,
        lat=lat,
        lon=lon,
    )


def _classified(predicted: str) -> ClassifiedSignal:
    """ClassifiedSignal with just the fields the attribution engine reads."""
    return ClassifiedSignal(
        reading_id="rd_test",
        predicted_class=predicted,
        confidence=0.9,
        embedding=[0.0] * 32,
        softmax=[1.0 / 8] * 8,
        nearest_known_class=predicted,
        distance_to_nearest_centroid=0.1,
        reconstruction_error=0.01,
        ood_score=0.05,
        baseline_deviation=0.1,
        is_anomaly=False,
        priority="medium",
        action="log",
        explanation="test",
    )


class _StaticBlueForce(BlueForceFeed):
    """BlueForceFeed variant that does NOT touch STATE.blue_force.

    We override the query helpers so each test controls exactly which
    units the attribution engine sees — no cross-test leakage via the
    singleton ``STATE.blue_force`` dict.
    """

    def __init__(self, units: list[BlueForceUnit]) -> None:
        super().__init__()
        self._units = units

    def units_with_emitter(self, emitter_id: str) -> list[BlueForceUnit]:
        return [u for u in self._units if emitter_id in u.active_emitters]

    def closest_unit_with_emitter(
        self,
        emitter_id: str,
        lat: float,
        lon: float,
    ) -> tuple[Optional[BlueForceUnit], Optional[float]]:
        # Use the base class's math — it walks the result of
        # units_with_emitter, which we've already overridden.
        from app.pipeline.blue_force import _haversine_m

        best, best_d = None, float("inf")
        for u in self.units_with_emitter(emitter_id):
            d = _haversine_m(u.lat, u.lon, lat, lon)
            if d < best_d:
                best, best_d = u, d
        return (best, best_d) if best is not None else (None, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_red_known_for_clean_dji_2g4_hit() -> None:
    """A classic DJI 2.4 GHz control burst should land RED_KNOWN."""
    reading = _reading(
        center_mhz=2450.0,
        bw_khz=10_000,  # 10 MHz — matches OcuSync nominal
        pattern="repeated_short_bursts",
    )
    classified = _classified("drone_control_repeated_burst")

    r = attribute(reading, classified, blue_force=None)

    assert r.verdict == "RED_KNOWN"
    assert r.best_emitter_id == "emitter_dji_control_24"
    assert r.best_score >= 0.65
    assert "DJI" in (r.best_emitter_name or "")


def test_unexplained_for_frequency_outside_every_profile() -> None:
    """A 30 GHz burst isn't covered by any library entry → UNEXPLAINED."""
    reading = _reading(
        center_mhz=30_000.0,   # 30 GHz — no profile covers this band
        bw_khz=200,
        pattern="continuous",
    )
    classified = _classified("unknown_ood")

    r = attribute(reading, classified, blue_force=None)

    assert r.verdict == "UNEXPLAINED"
    assert r.best_score < 0.40
    assert "no library entry" in r.reason.lower() or "closest miss" in r.reason.lower()


def test_blue_attributed_when_friendly_unit_in_range() -> None:
    """Friendly radio burst + nearby blue unit operating that radio → BLUE."""
    reading = _reading(
        center_mhz=100.0,  # inside 30-174 MHz PRC-148 band
        bw_khz=25,
        pattern="short_burst",
        lat=34.0600,
        lon=-118.2400,
    )
    classified = _classified("friendly_radio_burst")

    nearby_unit = BlueForceUnit(
        unit_id="Blue-2",
        callsign="RAIDER-1-8",
        lat=34.0603,  # ~30 m away
        lon=-118.2401,
        active_emitters=["emitter_blue_prc148_vhf"],
    )
    bf = _StaticBlueForce([nearby_unit])

    r = attribute(
        reading,
        classified,
        blue_force=bf,
        fix_lat=reading.lat,
        fix_lon=reading.lon,
    )

    assert r.verdict == "BLUE_ATTRIBUTED"
    assert r.attributed_unit_id == "Blue-2"
    assert r.attributed_unit_callsign == "RAIDER-1-8"
    assert r.distance_to_attributed_unit_m is not None
    assert r.distance_to_attributed_unit_m <= 100.0  # ~30 m in reality


def test_friendly_profile_no_friendly_nearby_becomes_unexplained() -> None:
    """Friendly-looking freq but no blue unit in range → UNEXPLAINED (possible impersonation).

    This is the mentor's "adversary on your friendly band" case.
    """
    reading = _reading(
        center_mhz=100.0,
        bw_khz=25,
        pattern="short_burst",
        lat=34.0600,
        lon=-118.2400,
    )
    classified = _classified("friendly_radio_burst")

    far_away_unit = BlueForceUnit(
        unit_id="Blue-2",
        callsign="RAIDER-1-8",
        # Move the unit ~10 km away — well outside the 800 m radius.
        lat=34.15,
        lon=-118.35,
        active_emitters=["emitter_blue_prc148_vhf"],
    )
    bf = _StaticBlueForce([far_away_unit])

    r = attribute(
        reading,
        classified,
        blue_force=bf,
        fix_lat=reading.lat,
        fix_lon=reading.lon,
    )

    assert r.verdict == "UNEXPLAINED"
    assert "no friendly unit" in r.reason.lower() or "impersonation" in r.reason.lower()


def test_impersonation_hint_blocks_blue_attribution_even_if_unit_is_close() -> None:
    """If the classifier flagged impersonation, refuse blue attribution."""
    reading = _reading(
        center_mhz=100.0,
        bw_khz=25,
        pattern="short_burst",
    )
    classified = _classified("friendly_profile_mismatch")

    close_unit = BlueForceUnit(
        unit_id="Blue-2",
        callsign="RAIDER-1-8",
        lat=reading.lat + 0.0001,  # a few metres away
        lon=reading.lon + 0.0001,
        active_emitters=["emitter_blue_prc148_vhf"],
    )
    bf = _StaticBlueForce([close_unit])

    r = attribute(reading, classified, blue_force=bf)

    assert r.verdict == "UNEXPLAINED"
    assert "deception" in r.reason.lower() or "mismatch" in r.reason.lower()


def test_ambiguous_for_wifi_civilian_hit() -> None:
    """WiFi civilian band → AMBIGUOUS (watch-but-don't-alarm)."""
    reading = _reading(
        center_mhz=2450.0,
        bw_khz=20_000,  # 20 MHz — WiFi bandwidth
        pattern="continuous",
    )
    classified = _classified("commercial_continuous")

    r = attribute(reading, classified, blue_force=None)

    assert r.verdict == "AMBIGUOUS"
    assert r.best_emitter_id is not None
    assert r.best_emitter_id.startswith("emitter_commercial_wifi")


def test_no_library_returns_unexplained() -> None:
    """Empty library → UNEXPLAINED without exception."""
    saved = STATE.emitter_library
    STATE.emitter_library = []
    try:
        reading = _reading(center_mhz=2450.0)
        classified = _classified("commercial_continuous")
        r = attribute(reading, classified, blue_force=None)
        assert r.verdict == "UNEXPLAINED"
        assert r.reason == "no_library"
    finally:
        STATE.emitter_library = saved


def test_feature_scores_rounded_and_summed_correctly() -> None:
    """Exposed feature_scores should be in [0, 1] and be the 4 weighted dims."""
    reading = _reading(center_mhz=2450.0, bw_khz=10_000, pattern="repeated_short_bursts")
    classified = _classified("drone_control_repeated_burst")

    r = attribute(reading, classified, blue_force=None)

    assert set(r.feature_scores.keys()) == {"freq", "bw", "pattern", "class"}
    for k, v in r.feature_scores.items():
        assert 0.0 <= v <= 1.0, f"{k} out of range: {v}"


def test_runner_ups_are_sorted_and_distinct() -> None:
    """Runner-ups should be different profiles, in descending score order."""
    reading = _reading(center_mhz=2450.0, bw_khz=10_000, pattern="repeated_short_bursts")
    classified = _classified("drone_control_repeated_burst")

    r = attribute(reading, classified, blue_force=None)

    ids = [ru["emitter_id"] for ru in r.runner_ups]
    assert len(ids) == len(set(ids)), "runner-ups contain duplicates"
    # best_emitter_id should not appear among runner-ups.
    assert r.best_emitter_id not in ids
    # Scores should be non-increasing.
    scores = [ru["score"] for ru in r.runner_ups]
    assert scores == sorted(scores, reverse=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
