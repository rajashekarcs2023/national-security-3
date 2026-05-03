"""Unit tests for ``app.pipeline.persistence.PersistenceTracker``.

Covers:
  * non-UNEXPLAINED verdicts are ignored
  * MIN_CLUSTER_SIZE-th point promotes the cluster and returns a PersistentEmitter
  * subsequent additions keep returning updated events (not None)
  * points separated beyond EPS_M form separate clusters
  * predicted-class keying keeps cluster-of-unknowns together even when the
    closest-miss emitter_id flips
  * pruning drops stale points
  * MEDIUM→HIGH priority flip at n=5
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.pipeline.persistence import (
    EPS_M,
    MIN_CLUSTER_SIZE,
    PersistenceTracker,
    TIME_WINDOW_S,
)
from app.schemas import AttributionResult, ClassifiedSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classified(predicted: str = "unknown_ood") -> ClassifiedSignal:
    return ClassifiedSignal(
        reading_id="rd_test",
        predicted_class=predicted,
        confidence=0.9,
        embedding=[0.0] * 32,
        softmax=[1.0 / 8] * 8,
        nearest_known_class=predicted,
        distance_to_nearest_centroid=0.1,
        reconstruction_error=0.01,
        ood_score=0.2,
        baseline_deviation=0.1,
        is_anomaly=True,
        priority="medium",
        action="log",
        explanation="test",
    )


def _attr(
    verdict: str = "UNEXPLAINED",
    best_emitter_id: str = "emitter_unknown_ku_15ghz",
) -> AttributionResult:
    return AttributionResult(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=0.5,
        best_emitter_id=best_emitter_id,
        best_emitter_name=best_emitter_id,
        best_score=0.35,
        reason="test",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_non_unexplained_is_ignored() -> None:
    tracker = PersistenceTracker()
    for verdict in ("BLUE_ATTRIBUTED", "RED_KNOWN", "AMBIGUOUS"):
        ev = tracker.add(
            _attr(verdict=verdict),
            _classified(),
            lat=34.05,
            lon=-118.22,
            event_id=f"evt_{verdict}",
        )
        assert ev is None, f"verdict {verdict} should not have promoted a cluster"


def test_promotes_on_min_cluster_size() -> None:
    """The MIN_CLUSTER_SIZE-th add is the first to return a PersistentEmitter."""
    tracker = PersistenceTracker()
    t0 = datetime.now(timezone.utc)
    # Cluster three detections at ~same spot.
    events = []
    for i in range(MIN_CLUSTER_SIZE + 1):
        ev = tracker.add(
            _attr(),
            _classified(),
            lat=34.0500 + i * 1e-5,  # tiny spatial jitter, well within EPS_M
            lon=-118.2200 + i * 1e-5,
            timestamp=t0 + timedelta(seconds=10 * i),
            event_id=f"evt_{i}",
        )
        events.append(ev)

    # First (MIN_CLUSTER_SIZE - 1) adds return None (singleton / below threshold).
    for i in range(MIN_CLUSTER_SIZE - 1):
        assert events[i] is None, f"add #{i} should not have promoted"
    # The threshold-crossing add and subsequent add should return an event.
    for i in range(MIN_CLUSTER_SIZE - 1, len(events)):
        assert events[i] is not None, f"add #{i} should have returned an event"

    final = events[-1]
    assert final is not None
    assert final.n_detections == MIN_CLUSTER_SIZE + 1
    # ``signal_class`` is the stripped value (no "class:" prefix) since that's
    # what ends up in the Foundry schema / dashboard.
    assert final.signal_class == "unknown_ood"
    # Event-id trail is preserved.
    assert set(final.detection_event_ids) == {f"evt_{i}" for i in range(MIN_CLUSTER_SIZE + 1)}


def test_cluster_id_is_stable_across_updates() -> None:
    """Subsequent promotions of the same cluster reuse the same id."""
    tracker = PersistenceTracker()
    t0 = datetime.now(timezone.utc)
    ids: list[str] = []
    for i in range(MIN_CLUSTER_SIZE + 2):
        ev = tracker.add(
            _attr(),
            _classified(),
            lat=34.0500,
            lon=-118.2200,
            timestamp=t0 + timedelta(seconds=i),
            event_id=f"evt_{i}",
        )
        if ev is not None:
            ids.append(ev.id)

    assert len(ids) >= 2
    assert len(set(ids)) == 1, f"cluster id changed between updates: {ids}"


def test_far_apart_points_form_separate_clusters() -> None:
    """Two detections > EPS_M apart must NOT join the same cluster."""
    tracker = PersistenceTracker()
    t0 = datetime.now(timezone.utc)
    # Two widely separated spots, each with MIN_CLUSTER_SIZE detections.
    spot_a = (34.0500, -118.2200)
    spot_b = (34.0800, -118.1800)  # >> EPS_M away

    for i in range(MIN_CLUSTER_SIZE):
        tracker.add(
            _attr(), _classified(),
            lat=spot_a[0], lon=spot_a[1],
            timestamp=t0 + timedelta(seconds=i),
            event_id=f"a_{i}",
        )
    for i in range(MIN_CLUSTER_SIZE):
        tracker.add(
            _attr(), _classified(),
            lat=spot_b[0], lon=spot_b[1],
            timestamp=t0 + timedelta(seconds=100 + i),
            event_id=f"b_{i}",
        )

    active = tracker.snapshot()
    # There should be exactly two promoted clusters.
    assert len(active) == 2
    signal_classes = {c.signal_class for c in active}
    # Both keyed on the same class (unknown_ood) but they are separate clusters.
    assert signal_classes == {"unknown_ood"}
    centroids = sorted((round(c.lat, 2), round(c.lon, 2)) for c in active)
    assert centroids == [(round(spot_a[0], 2), round(spot_a[1], 2)),
                         (round(spot_b[0], 2), round(spot_b[1], 2))]


def test_unexplained_clusters_on_predicted_class_not_closest_miss() -> None:
    """When closest-miss emitter flips, unexplained points at one spot still
    all land in ONE cluster because we key on predicted_class."""
    tracker = PersistenceTracker()
    t0 = datetime.now(timezone.utc)
    # Alternate the closest-miss emitter id across adds — but the predicted
    # class is the same, so the cluster key ("class:unknown_ood") is stable.
    best_ids = [
        "emitter_dji_video_58",
        "emitter_unknown_ku_15ghz",
        "emitter_dji_video_58",
        "emitter_unknown_ku_15ghz",
    ]
    ev = None
    for i, bid in enumerate(best_ids):
        ev = tracker.add(
            _attr(best_emitter_id=bid),
            _classified(),
            lat=34.0500,
            lon=-118.2200,
            timestamp=t0 + timedelta(seconds=i),
            event_id=f"evt_{i}",
        )
    assert ev is not None
    assert ev.n_detections == len(best_ids)
    assert ev.signal_class == "unknown_ood"


def test_priority_flips_to_high_at_n5() -> None:
    """MEDIUM below 5 detections, HIGH at 5+; action flips to HAND_OFF_INTERCEPTOR."""
    tracker = PersistenceTracker()
    t0 = datetime.now(timezone.utc)
    latest = None
    for i in range(6):
        latest = tracker.add(
            _attr(),
            _classified(),
            lat=34.0500,
            lon=-118.2200,
            timestamp=t0 + timedelta(seconds=i),
            event_id=f"evt_{i}",
        )
        if latest is not None:
            if latest.n_detections < 5:
                assert latest.priority == "medium"
                assert latest.recommended_action == "INVESTIGATE_AND_GEOLOCATE"
            else:
                assert latest.priority == "high"
                assert latest.recommended_action == "HAND_OFF_INTERCEPTOR"
    assert latest is not None and latest.n_detections == 6


def test_pruning_drops_stale_points() -> None:
    """prune() removes points older than TIME_WINDOW_S."""
    tracker = PersistenceTracker()
    t_stale = datetime.now(timezone.utc) - timedelta(seconds=TIME_WINDOW_S + 60)
    # Add MIN_CLUSTER_SIZE stale points; they form a cluster...
    for i in range(MIN_CLUSTER_SIZE):
        tracker.add(
            _attr(), _classified(),
            lat=34.05, lon=-118.22,
            timestamp=t_stale + timedelta(seconds=i),
            event_id=f"stale_{i}",
        )
    # ... then prune at "now".
    dropped = tracker.prune()
    assert dropped == MIN_CLUSTER_SIZE
    # No active clusters left.
    assert tracker.snapshot() == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
