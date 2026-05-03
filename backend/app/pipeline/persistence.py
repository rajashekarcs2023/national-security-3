"""Phase E — Persistence detector.

Clusters ``UNEXPLAINED`` detections in (space × time × signal-class) so the
operator gets a single high-priority event when the same unknown emitter
keeps showing up — instead of a wall of one-off RF anomalies.

Algorithm
---------
A streaming DBSCAN-like clusterer:

  * Each detection is a point ``(lat, lon, t, signal_class)``.
  * Two points are *connected* iff they share the same ``signal_class``
    AND are within ``EPS_M`` metres AND within ``TIME_WINDOW_S`` seconds
    of each other.
  * A cluster of size ``>= MIN_CLUSTER_SIZE`` is a *persistent unknown
    emitter*. The first time a cluster crosses that threshold we emit a
    ``PersistentEmitter`` event; subsequent additions update the centroid
    + last-seen.

This isn't full Euclidean DBSCAN because (a) the time axis is open-ended,
not an additive feature, and (b) we want stream-friendly amortised O(N).
The implementation maintains, per ``signal_class``:

  * a rolling deque of the last ``MAX_HISTORY`` detection points
  * a list of active clusters

Each new point checks the *current* clusters for connectivity and joins
one if any. Otherwise it starts a new singleton cluster. Stale points
(outside the time window) are pruned at the head of the deque.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.schemas import (
    AttributionResult,
    ClassifiedSignal,
    PersistentEmitter,
    new_id,
    utc_now,
)

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------

EPS_M: float = 200.0
"""Spatial neighbourhood — points within this distance are *connected*."""

TIME_WINDOW_S: float = 3600.0
"""Temporal neighbourhood — points within this many seconds are *connected*."""

MIN_CLUSTER_SIZE: int = 3
"""Cluster size at which we promote it to a PersistentEmitter event."""

MAX_HISTORY: int = 500
"""Per-class hard cap on the rolling detection buffer (defensive)."""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ----------------------------------------------------------------------------
# Cluster bookkeeping
# ----------------------------------------------------------------------------

class _Cluster:
    """One in-memory cluster of UNEXPLAINED detections."""

    __slots__ = (
        "id",
        "signal_class",
        "points",            # list[(lat, lon, t, event_id)]
        "first_seen",
        "last_seen",
        "promoted",
    )

    def __init__(self, signal_class: str, lat: float, lon: float, t: datetime, event_id: Optional[str]) -> None:
        self.id = new_id("pe_")
        self.signal_class = signal_class
        self.points: list[tuple[float, float, datetime, Optional[str]]] = [
            (lat, lon, t, event_id)
        ]
        self.first_seen = t
        self.last_seen = t
        self.promoted = False

    @property
    def n(self) -> int:
        return len(self.points)

    def centroid(self) -> tuple[float, float]:
        n = float(self.n)
        lat = sum(p[0] for p in self.points) / n
        lon = sum(p[1] for p in self.points) / n
        return lat, lon

    def radius_p95_m(self) -> float:
        """95th-percentile distance from centroid — used as the 'cluster size'."""
        if self.n <= 1:
            return 0.0
        lat0, lon0 = self.centroid()
        dists = sorted(_haversine_m(lat0, lon0, p[0], p[1]) for p in self.points)
        idx = max(0, int(0.95 * (len(dists) - 1)))
        return dists[idx]

    def event_ids(self) -> list[str]:
        return [p[3] for p in self.points if p[3] is not None]

    def add(self, lat: float, lon: float, t: datetime, event_id: Optional[str]) -> None:
        self.points.append((lat, lon, t, event_id))
        if t > self.last_seen:
            self.last_seen = t
        if t < self.first_seen:
            self.first_seen = t

    def is_connected_to(self, lat: float, lon: float, t: datetime) -> bool:
        for plat, plon, pt, _ in self.points:
            if _haversine_m(plat, plon, lat, lon) > EPS_M:
                continue
            if abs((pt - t).total_seconds()) > TIME_WINDOW_S:
                continue
            return True
        return False

    def prune_before(self, cutoff_t: datetime) -> None:
        self.points = [p for p in self.points if p[2] >= cutoff_t]
        if self.points:
            self.first_seen = min(p[2] for p in self.points)
            self.last_seen = max(p[2] for p in self.points)


# ----------------------------------------------------------------------------
# PersistenceTracker — public API
# ----------------------------------------------------------------------------

class PersistenceTracker:
    """Keeps cluster state, emits ``PersistentEmitter`` events.

    Usage
    -----
    >>> tracker = PersistenceTracker()
    >>> evt = tracker.add(...)  # returns a PersistentEmitter the moment a
    ...                          # cluster crosses MIN_CLUSTER_SIZE, and on
    ...                          # every subsequent update for that cluster.
    """

    def __init__(self) -> None:
        # Active clusters keyed by signal_class then list — keeps the
        # search local when a new point arrives.
        self._clusters_by_class: dict[str, list[_Cluster]] = {}

    # ------------------------------------------------------------------
    def _cluster_key(self, attribution: AttributionResult, classified: ClassifiedSignal) -> str:
        """Pick the cluster discriminator.

        For UNEXPLAINED verdicts we *intentionally* key on the classifier's
        predicted class (e.g. ``class:unknown_ood``). The attribution's
        ``best_emitter_id`` is the closest *miss* — it can flip between
        adjacent civilian/red emitters tick-to-tick (DJI 5.8 vs Unknown
        Ku-band ~15 GHz, etc.) and would split the cluster across keys.
        Keying on predicted_class keeps every recurrence of the same
        unknown signal at the same spot inside one cluster.

        For non-UNEXPLAINED verdicts (this code path is only reachable
        from ``ingest`` after the UNEXPLAINED gate, but the helper is
        still kept robust) we fall back to the emitter id when present.
        """
        if attribution.verdict == "UNEXPLAINED":
            return f"class:{classified.predicted_class}"
        if attribution.best_emitter_id:
            return f"emitter:{attribution.best_emitter_id}"
        return f"class:{classified.predicted_class}"

    # ------------------------------------------------------------------
    def add(
        self,
        attribution: AttributionResult,
        classified: ClassifiedSignal,
        lat: float,
        lon: float,
        timestamp: Optional[datetime] = None,
        event_id: Optional[str] = None,
    ) -> Optional[PersistentEmitter]:
        """Ingest one detection. Returns a ``PersistentEmitter`` iff the
        cluster the point joined is at or above ``MIN_CLUSTER_SIZE``.

        Only ``UNEXPLAINED`` verdicts are processed; everything else is a
        no-op. We *do* let AMBIGUOUS through because the operator may
        want to track ambiguity that recurs at the same place — but
        keeping it strictly UNEXPLAINED gives the clearest demo story.
        """
        if attribution.verdict != "UNEXPLAINED":
            return None

        timestamp = timestamp or utc_now()
        key = self._cluster_key(attribution, classified)
        clusters = self._clusters_by_class.setdefault(key, [])

        # Try to join an existing cluster.
        joined: Optional[_Cluster] = None
        for c in clusters:
            if c.is_connected_to(lat, lon, timestamp):
                c.add(lat, lon, timestamp, event_id)
                joined = c
                break

        if joined is None:
            # Start a fresh singleton.
            joined = _Cluster(key, lat, lon, timestamp, event_id)
            clusters.append(joined)

        # If two clusters became connected through the new point, merge.
        if len(clusters) > 1:
            self._maybe_merge(clusters, joined)

        # Defensive cap.
        if joined.n > MAX_HISTORY:
            joined.points = joined.points[-MAX_HISTORY:]

        if joined.n >= MIN_CLUSTER_SIZE:
            joined.promoted = True
            return self._to_event(joined, key)
        return None

    # ------------------------------------------------------------------
    def _maybe_merge(self, clusters: list[_Cluster], anchor: _Cluster) -> None:
        merged = True
        while merged:
            merged = False
            for c in list(clusters):
                if c is anchor:
                    continue
                # Connected if any of c's points connects to anchor.
                bridges = any(
                    anchor.is_connected_to(p[0], p[1], p[2]) for p in c.points
                )
                if bridges:
                    for p in c.points:
                        anchor.add(*p)
                    clusters.remove(c)
                    merged = True

    # ------------------------------------------------------------------
    def _to_event(self, c: _Cluster, key: str) -> PersistentEmitter:
        lat, lon = c.centroid()
        # Heuristic: 5+ recurrences = high priority, else medium.
        priority = "high" if c.n >= 5 else "medium"
        signal_class = key.split(":", 1)[1] if ":" in key else key
        # Recommended action: if we know the band but no profile owns it,
        # tip the COA layer to investigate.
        recommended_action = (
            "INVESTIGATE_AND_GEOLOCATE"
            if c.n < 5
            else "HAND_OFF_INTERCEPTOR"
        )
        return PersistentEmitter(
            id=c.id,
            first_seen=c.first_seen,
            last_seen=c.last_seen,
            n_detections=c.n,
            lat=lat,
            lon=lon,
            radius_m=c.radius_p95_m(),
            signal_class=signal_class,
            detection_event_ids=c.event_ids(),
            recommended_action=recommended_action,
            priority=priority,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    def prune(self, now: Optional[datetime] = None) -> int:
        """Drop points older than ``TIME_WINDOW_S``. Returns # points dropped."""
        now = now or utc_now()
        cutoff = now - timedelta(seconds=TIME_WINDOW_S)
        dropped = 0
        for key in list(self._clusters_by_class):
            new_clusters: list[_Cluster] = []
            for c in self._clusters_by_class[key]:
                before = c.n
                c.prune_before(cutoff)
                dropped += before - c.n
                if c.n > 0:
                    new_clusters.append(c)
            if new_clusters:
                self._clusters_by_class[key] = new_clusters
            else:
                self._clusters_by_class.pop(key, None)
        return dropped

    # ------------------------------------------------------------------
    def snapshot(self) -> list[PersistentEmitter]:
        """List all *promoted* clusters as ``PersistentEmitter`` records."""
        out: list[PersistentEmitter] = []
        for key, clusters in self._clusters_by_class.items():
            for c in clusters:
                if c.promoted and c.n >= MIN_CLUSTER_SIZE:
                    out.append(self._to_event(c, key))
        return out
