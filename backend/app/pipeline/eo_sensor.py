"""Tipping EO/IR camera sensor — simulated.

Phase A of the kill-chain plan (`PLAN_KILL_CHAIN.md`).

This module is a *behavioural* simulator of a real tipping electro-optical /
infra-red gimbal camera. It is NOT a real CV pipeline and does not load any
visual model. We model the things that actually matter for the demo:

  - Gimbal slew latency (real cameras take ~400-1200 ms to slew 90°).
  - Imperfect agreement with RF (real multi-INT fusion disagrees ~10-20% of
    the time at low SNR / range).
  - Occasional "no_visual" outcomes (occlusion, fog, dust, range, tree-line).
  - Occasional "contradiction" outcomes (RF says drone, EO sees a bird —
    possible deception, or genuine sensor mismatch — operator must adjudicate).

The output is `EOObservation` (defined in `app.schemas`). The frontend
draws a *synthetic* camera frame from the sector + bbox + frame_kind so a
judge can see something visually compelling without us shipping a fake CV
model. Both the README and the dashboard label this clearly as a simulator.

A real production deployment would replace `EOSensor.observe()` with calls
into a YOLOv8n-int8 (~6 MB) running on the same edge device — the rest of
the pipeline would not change.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from typing import Optional

from app.schemas import EOFrameKind, EOObservation, UASTrack, utc_now


# Map sector tag -> bearing centre in degrees (clockwise from true north).
# Quartered compass: NE=045, SE=135, SW=225, NW=315.
SECTOR_BEARING_DEG: dict[str, float] = {
    "NE": 45.0,
    "SE": 135.0,
    "SW": 225.0,
    "NW": 315.0,
}


# Probability table for the *frame_kind* outcome of an EO observation,
# conditioned on the RF-derived `track.classification`. Numbers are
# illustrative but reflect three real fusion truths:
#
#   1. Even when RF is highly confident, EO sometimes can't see (no_visual)
#      due to occlusion, fog, dust, tree-line, or range.
#   2. Avian false-alarms are real — small birds at distance look a lot
#      like a Group-1 quadcopter on a low-resolution gimbal feed.
#   3. RF spoofing / deception happens; EO contradiction is the cheapest
#      way to catch it.
#
# Order of keys in each tuple:
#   (quadcopter, fixed_wing, bird, person, no_visual, contradiction)
_FRAME_PROBS_BY_TRACK_CLASS: dict[str, tuple[float, ...]] = {
    "CONFIRMED_UAS": (0.78, 0.04, 0.06, 0.00, 0.08, 0.04),
    "POSSIBLE_UAS":  (0.55, 0.10, 0.10, 0.05, 0.15, 0.05),
    "UNKNOWN":       (0.20, 0.15, 0.10, 0.15, 0.30, 0.10),
    "FALSE_ALARM":   (0.00, 0.00, 0.20, 0.10, 0.65, 0.05),
}

_FRAME_KINDS: tuple[EOFrameKind, ...] = (
    "quadcopter",
    "fixed_wing",
    "bird",
    "person",
    "no_visual",
    "contradiction",
)


# Confidence ranges per frame kind. These shape the dashboard reading and
# also feed the `confirms_rf` decision (>=0.6 + recognised class confirms).
_CONFIDENCE_RANGES: dict[EOFrameKind, tuple[float, float]] = {
    "quadcopter":    (0.78, 0.94),
    "fixed_wing":    (0.74, 0.91),
    "bird":          (0.62, 0.84),
    "person":        (0.66, 0.88),
    "no_visual":     (0.00, 0.05),
    "contradiction": (0.55, 0.78),
}


# Human-readable label for the dashboard's classification field.
_FRAME_LABEL: dict[EOFrameKind, str] = {
    "quadcopter":    "Group-1 quadrotor UAS",
    "fixed_wing":    "Fixed-wing aircraft / Group-3 UAS",
    "bird":          "Avian (likely false alarm)",
    "person":        "Personnel / dismount",
    "no_visual":     "No visual contact (occluded / fog / range)",
    "contradiction": "Visual disagrees with RF — possible deception",
}


# Range estimate ranges (metres) per frame kind. None for no_visual.
_RANGE_M_RANGES: dict[EOFrameKind, Optional[tuple[float, float]]] = {
    "quadcopter":    (25.0, 250.0),
    "fixed_wing":    (300.0, 2000.0),
    "bird":          (15.0, 90.0),
    "person":        (40.0, 300.0),
    "no_visual":     None,
    "contradiction": (30.0, 200.0),
}


def _seeded_rng(track_id: str, salt: str = "") -> random.Random:
    """Deterministic per-track RNG for reproducible demo runs."""
    h = hashlib.sha1(f"{track_id}|{salt}".encode("utf-8")).hexdigest()
    seed = int(h[:8], 16)
    return random.Random(seed)


class EOSensor:
    """Simulated tipping EO/IR camera.

    Public API is `observe(track) -> EOObservation`. The call awaits a
    short sleep that models gimbal slew latency, then returns an
    observation whose frame_kind is sampled deterministically from the
    track's RF classification.

    The simulator can be temporarily *disabled* via `disable_for(seconds)`
    — used by the `cross_cue_demo` scenario to demonstrate the
    `VISUAL_LOST_RF_PRESENT → REACQUIRED` custody cycle.
    """

    def __init__(self, sensor_id: str = "EO-GIMBAL-01") -> None:
        self.sensor_id = sensor_id
        # Until this monotonic timestamp, every observation returns
        # `no_visual`. Set by `disable_for()` from scenario steps.
        self._disabled_until: float = 0.0

    # ------------------------------------------------------------------
    def disable_for(self, seconds: float) -> None:
        """Force the sensor into 'no_visual' for ``seconds``."""
        self._disabled_until = time.monotonic() + max(0.0, float(seconds))

    @property
    def is_disabled(self) -> bool:
        return time.monotonic() < self._disabled_until

    # ------------------------------------------------------------------
    async def observe(
        self,
        track: UASTrack,
        triggering_event_id: Optional[str] = None,
        slew_min_ms: int = 400,
        slew_max_ms: int = 1200,
    ) -> EOObservation:
        """Slew to the track's sector and return one EO observation.

        The call is intentionally async — we sleep for a sampled slew time
        so the live dashboard reflects the realistic latency between
        "RF detected at T" and "EO confirmed at T+Δ".
        """
        rng = _seeded_rng(track.track_id, salt=f"obs|{track.n_detections}")

        slew_ms = rng.randint(int(slew_min_ms), int(slew_max_ms))
        # Sleep async to model gimbal latency without blocking the loop.
        await asyncio.sleep(slew_ms / 1000.0)

        # Sector → bearing with ±20° jitter.
        bearing_centre = SECTOR_BEARING_DEG.get(track.sector, 45.0)
        bearing = (bearing_centre + rng.uniform(-20.0, 20.0)) % 360.0

        # If the operator (or scenario) has masked the EO subsystem, force
        # the observation to no_visual regardless of probabilities.
        if self.is_disabled:
            return _make_observation(
                rng=rng,
                track=track,
                triggering_event_id=triggering_event_id,
                sensor_id=self.sensor_id,
                slew_time_ms=slew_ms,
                bearing_deg=bearing,
                forced_kind="no_visual",
                notes="EO subsystem masked (scenario step / hardware fault sim)",
            )

        probs = _FRAME_PROBS_BY_TRACK_CLASS.get(
            track.classification,
            _FRAME_PROBS_BY_TRACK_CLASS["UNKNOWN"],
        )
        kind: EOFrameKind = rng.choices(_FRAME_KINDS, weights=probs, k=1)[0]
        return _make_observation(
            rng=rng,
            track=track,
            triggering_event_id=triggering_event_id,
            sensor_id=self.sensor_id,
            slew_time_ms=slew_ms,
            bearing_deg=bearing,
            forced_kind=kind,
        )


# ----------------------------------------------------------------------
def _make_observation(
    *,
    rng: random.Random,
    track: UASTrack,
    triggering_event_id: Optional[str],
    sensor_id: str,
    slew_time_ms: int,
    bearing_deg: float,
    forced_kind: EOFrameKind,
    notes: str = "",
) -> EOObservation:
    """Helper: build an EOObservation given a frame kind."""
    conf_lo, conf_hi = _CONFIDENCE_RANGES[forced_kind]
    confidence = round(rng.uniform(conf_lo, conf_hi), 2)
    label = _FRAME_LABEL[forced_kind]

    # Bbox (normalised x, y, w, h). Empty for no_visual; small + central
    # for distant drones; off-centre for birds; medium for fixed-wing.
    if forced_kind == "no_visual":
        bbox = (0.0, 0.0, 0.0, 0.0)
    elif forced_kind == "quadcopter":
        x = round(rng.uniform(0.35, 0.55), 3)
        y = round(rng.uniform(0.30, 0.50), 3)
        w = round(rng.uniform(0.06, 0.14), 3)
        h = round(rng.uniform(0.05, 0.11), 3)
        bbox = (x, y, w, h)
    elif forced_kind == "fixed_wing":
        x = round(rng.uniform(0.20, 0.55), 3)
        y = round(rng.uniform(0.20, 0.45), 3)
        w = round(rng.uniform(0.18, 0.30), 3)
        h = round(rng.uniform(0.07, 0.12), 3)
        bbox = (x, y, w, h)
    elif forced_kind == "bird":
        x = round(rng.uniform(0.10, 0.80), 3)
        y = round(rng.uniform(0.10, 0.55), 3)
        w = round(rng.uniform(0.04, 0.09), 3)
        h = round(rng.uniform(0.04, 0.08), 3)
        bbox = (x, y, w, h)
    elif forced_kind == "person":
        x = round(rng.uniform(0.30, 0.65), 3)
        y = round(rng.uniform(0.55, 0.75), 3)
        w = round(rng.uniform(0.05, 0.10), 3)
        h = round(rng.uniform(0.18, 0.28), 3)
        bbox = (x, y, w, h)
    else:  # contradiction
        x = round(rng.uniform(0.30, 0.60), 3)
        y = round(rng.uniform(0.30, 0.60), 3)
        w = round(rng.uniform(0.06, 0.12), 3)
        h = round(rng.uniform(0.06, 0.10), 3)
        bbox = (x, y, w, h)

    range_range = _RANGE_M_RANGES.get(forced_kind)
    range_m: Optional[float]
    if range_range is None:
        range_m = None
    else:
        range_m = round(rng.uniform(*range_range), 1)

    confirms_rf = (
        forced_kind in {"quadcopter", "fixed_wing", "person"}
        and confidence >= 0.6
    )

    if not notes:
        if forced_kind == "no_visual":
            notes = "Camera holding on cued bearing; no target acquired."
        elif forced_kind == "contradiction":
            notes = "EO classifier confidence is low and class disagrees with RF — escalate to S2 for verification."
        elif confirms_rf:
            notes = "Multi-modal confirmed: RF custody backed by EO target acquisition."
        else:
            notes = "Visual contact made but does not confirm RF threat class."

    return EOObservation(
        timestamp=utc_now(),
        sensor_id=sensor_id,
        site_id=track.site_id,
        track_id=track.track_id,
        triggering_event_id=triggering_event_id,
        sector=track.sector,
        bearing_deg=round(bearing_deg, 1),
        slew_time_ms=int(slew_time_ms),
        frame_kind=forced_kind,
        classification=label,
        confidence=confidence,
        bbox=bbox,
        range_m_estimate=range_m,
        notes=notes,
        confirms_rf=confirms_rf,
    )


# ----------------------------------------------------------------------
# Self-test (run as `python -m app.pipeline.eo_sensor`)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    from datetime import datetime, timezone
    from collections import Counter

    async def _main() -> None:
        sensor = EOSensor()
        # Build N synthetic tracks across the four track classes and observe each.
        kinds_seen: Counter = Counter()
        confirms = 0
        for i in range(60):
            track = UASTrack(
                track_id=f"TRK-TEST-{i:03d}",
                custody_state="DETECTED",
                threat_level="HIGH",
                classification="POSSIBLE_UAS",
                confidence=0.9,
                sector=("NE", "NW", "SE", "SW")[i % 4],
                last_known_lat=34.0,
                last_known_lon=-118.0,
                first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
                n_detections=1,
            )
            obs = await sensor.observe(track, slew_min_ms=1, slew_max_ms=2)
            kinds_seen[obs.frame_kind] += 1
            if obs.confirms_rf:
                confirms += 1
        print(f"60 POSSIBLE_UAS observations: {dict(kinds_seen)}")
        print(f"confirms_rf={confirms}/60  (expected ~33 ≈ 55% confirm rate)")

    asyncio.run(_main())
