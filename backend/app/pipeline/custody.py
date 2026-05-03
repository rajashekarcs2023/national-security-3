"""Custody state machine.

Maps a stream of intelligence events to UAS tracks and emits state-transition
logs. This is the core "we don't just detect, we maintain custody" claim of
the project.

Custody states:
  DETECTED              -> first sighting of a previously-unknown signal
  TRACKING              -> multi-modal hold (RF + EO) OR repeated RF-only hits
  VISUAL_LOST_RF_PRESENT -> track was TRACKING, EO gimbal lost visual, RF still holds
  REACQUIRED            -> visual confirmation regained after a VISUAL_LOST window
  TRACK_LOST            -> no RF for too long
  CLEARED               -> operator-confirmed false alarm
  DISMISSED             -> operator dismissed

Multi-modal promotion (Phase A):
  DETECTED + first EO confirmation             -> TRACKING (fast path)
  DETECTED + >= 3 RF detections (no EO help)   -> TRACKING (slow path)
  TRACKING + EO confirmation older than 4.0 s  -> VISUAL_LOST_RF_PRESENT
  VISUAL_LOST_RF_PRESENT + EO re-confirms      -> REACQUIRED
  REACQUIRED + next EO confirm                 -> TRACKING

EO "contradiction" frames (RF says drone, EO sees bird / disagrees) do NOT
promote and DO emit a custody log with action_cue = "possible deception"
so an operator can adjudicate. This is the battlefield-correct conservative
default: we never auto-elevate confidence on disagreement.
"""

from __future__ import annotations

import time
from typing import Optional

from app.schemas import (
    CustodyStateLog,
    EOObservation,
    IntelligenceEvent,
    SensorModality,
    UASTrack,
    new_id,
    utc_now,
)


# How long can we go without a fresh detection in the same class before we say track_lost?
TRACK_TIMEOUT_SECONDS = 25.0
# Same class within this window in the same sector -> same track.
SAME_TRACK_WINDOW_SECONDS = 30.0
# How long a TRACK_LOST/CLEARED/DISMISSED track lingers in memory before being
# purged. The custody log entries persist independently; only the in-memory
# `tracks` dict is trimmed so the live custody timeline stays readable.
TRACK_LOST_PURGE_SECONDS = 90.0
# How long after the last EO-confirmed observation before we degrade a
# TRACKING track to VISUAL_LOST_RF_PRESENT. Real tipping cameras can lose
# lock in ~2-5 s at range; 4 s is a reasonable middle.
VISUAL_LOST_WINDOW_SECONDS = 4.0
# Minimum RF detections required to promote DETECTED -> TRACKING when EO has
# NOT confirmed yet. Stricter than before (was 2) so the visual pathway is
# clearly the preferred promotion route.
RF_ONLY_PROMOTE_THRESHOLD = 3

# Classes that represent friendly, civilian, or background activity. These
# should NEVER open a custody track — they're labelled FALSE_ALARM by the
# threat-level mapping and would otherwise pollute the timeline. (Their
# intelligence events are still emitted to the dashboard — we just don't
# tie them to a tracked target.)
FALSE_ALARM_CLASSES = frozenset({
    "background_noise",
    "friendly_radio_burst",
    "commercial_continuous",
})

# Classes that LEGITIMATELY move across frequency sectors as part of their
# signature (hopping is the entire point). For these, allow merging into the
# existing track even when the dominant frequency bin lands in a different
# sector — otherwise every hop opens a new track.
CROSS_SECTOR_CLASSES = frozenset({
    "frequency_hopping",
    "unknown_ood",
})


def _sector_for_freq_bin(bin_idx: int) -> str:
    """Trivial sector assignment from frequency bin (demo-only)."""
    if bin_idx < 16:
        return "SW"
    if bin_idx < 32:
        return "NE"
    if bin_idx < 48:
        return "NW"
    return "SE"


class CustodyManager:
    """Tracks UAS tracks and emits state-transition logs as events arrive."""

    def __init__(self, tracks: dict[str, UASTrack]):
        self.tracks = tracks
        self._track_seq = 0
        # Map class_name -> last_track_id, last_seen_ts, last_sector
        self._recent_by_class: dict[str, tuple[str, float, str]] = {}

    # ------------------------------------------------------------------
    def _new_track_id(self, classification: str, sector: str) -> str:
        self._track_seq += 1
        short = classification.split("_")[0].upper()[:4]
        return f"TRK-{short}-{sector}-{self._track_seq:03d}"

    def _classification_to_label(self, classification: str) -> tuple[str, str]:
        """Returns (classification_label, threat_level)."""
        mapping = {
            "drone_control_repeated_burst": ("CONFIRMED_UAS", "HIGH"),
            "frequency_hopping": ("POSSIBLE_UAS", "HIGH"),
            "unknown_ood": ("POSSIBLE_UAS", "MEDIUM"),
            "friendly_profile_mismatch": ("UNKNOWN", "MEDIUM"),
            "chirp": ("UNKNOWN", "MEDIUM"),
            "friendly_radio_burst": ("FALSE_ALARM", "LOW"),
            "commercial_continuous": ("FALSE_ALARM", "LOW"),
            "background_noise": ("FALSE_ALARM", "LOW"),
        }
        return mapping.get(classification, ("UNKNOWN", "MEDIUM"))

    # ------------------------------------------------------------------
    def on_event(
        self,
        event: IntelligenceEvent,
        dominant_freq_bin: int,
        lat: float,
        lon: float,
    ) -> Optional[tuple[UASTrack, CustodyStateLog]]:
        """Apply the event to the custody state machine.

        Returns (track, log) for events worth tracking, or None for events
        whose class is FALSE_ALARM (friendly radio, commercial, background
        noise) which should never open a custody track.
        """
        # ----- Fix #1: Friendly / civilian classes never open a track.
        if event.classification in FALSE_ALARM_CLASSES:
            return None

        now = time.time()
        ts = utc_now()
        sector = _sector_for_freq_bin(dominant_freq_bin)
        cross_sector = event.classification in CROSS_SECTOR_CLASSES

        # Find or create track.
        prev = self._recent_by_class.get(event.classification)
        track: Optional[UASTrack] = None
        previous_state: Optional[str] = None
        if prev is not None:
            track_id, last_seen, last_sector = prev
            # ----- Fix #2: Hoppers may move across sectors and still be the
            # same target. Skip the sector-equality check for those.
            sector_ok = cross_sector or last_sector == sector
            if now - last_seen <= SAME_TRACK_WINDOW_SECONDS and sector_ok:
                track = self.tracks.get(track_id)
                if track is not None:
                    previous_state = track.custody_state
                    track.n_detections += 1
                    track.last_seen = ts
                    track.last_known_lat = lat
                    track.last_known_lon = lon
                    # ----- Multi-modal promotion (Phase A).
                    # Visual-confirmed tracks already promoted via
                    # on_eo_observation(). RF-only tracks wait until we
                    # have RF_ONLY_PROMOTE_THRESHOLD detections, which is
                    # the conservative "don't promote on a single echo"
                    # default.
                    if (
                        track.custody_state == "DETECTED"
                        and not track.visual_confirmed
                        and track.n_detections >= RF_ONLY_PROMOTE_THRESHOLD
                    ):
                        track.custody_state = "TRACKING"

        if track is None:
            classification_label, threat_level = self._classification_to_label(event.classification)
            track_id = self._new_track_id(event.classification, sector)
            track = UASTrack(
                track_id=track_id,
                site_id=event.site_id,
                custody_state="DETECTED",
                threat_level=threat_level,  # type: ignore[arg-type]
                classification=classification_label,  # type: ignore[arg-type]
                confidence=event.confidence,
                sector=sector,
                last_known_lat=lat,
                last_known_lon=lon,
                first_seen=ts,
                last_seen=ts,
            )
            self.tracks[track_id] = track

        self._recent_by_class[event.classification] = (track.track_id, now, sector)

        # Emit a custody log
        log = CustodyStateLog(
            id=new_id("log_"),
            track_id=track.track_id,
            timestamp=ts,
            previous_state=previous_state,  # type: ignore[arg-type]
            new_state=track.custody_state,
            action_cue=event.recommended_action,
            evidence_summary=", ".join(event.evidence[:3]),
            triggering_event_id=event.id,
        )
        # Stamp the event with the resolved track id (events are mutable here).
        event.track_id = track.track_id
        return track, log

    # ------------------------------------------------------------------
    def transition(
        self,
        track_id: str,
        new_state: str,
        action_cue: str,
        evidence_summary: str = "",
        evidence_modalities: Optional[list[SensorModality]] = None,
    ) -> Optional[CustodyStateLog]:
        track = self.tracks.get(track_id)
        if not track:
            return None
        prev = track.custody_state
        track.custody_state = new_state  # type: ignore[assignment]
        track.last_seen = utc_now()
        return CustodyStateLog(
            id=new_id("log_"),
            track_id=track_id,
            timestamp=utc_now(),
            previous_state=prev,  # type: ignore[arg-type]
            new_state=new_state,  # type: ignore[arg-type]
            action_cue=action_cue,
            evidence_summary=evidence_summary,
            evidence_modalities=evidence_modalities or ["RF"],
        )

    # ------------------------------------------------------------------
    def on_eo_observation(
        self,
        obs: EOObservation,
    ) -> Optional[CustodyStateLog]:
        """Apply a tipping-camera observation to the custody state machine.

        Promotion / degradation rules (see module docstring):
          * `DETECTED + obs.confirms_rf`            -> `TRACKING`     [RF+EO]
          * `VISUAL_LOST_RF_PRESENT + obs.confirms_rf` -> `REACQUIRED`   [RF+EO]
          * `REACQUIRED + obs.confirms_rf`          -> `TRACKING`     [RF+EO]
          * `* + frame_kind == "contradiction"`       -> no state change,
              but a log with action_cue flagging possible deception so the
              operator (or a future autonomous decision layer) can adjudicate.
          * `* + frame_kind == "no_visual"`            -> no state change; we
              just stamp `last_eo_obs` so the dashboard can display "camera
              holding, no target acquired yet".

        The battlefield-correct default on disagreement is **never** to
        elevate confidence. Contradictions and missing visuals stay where
        they are (the RF evidence is untouched) and leave the human in the
        loop.
        """
        track = self.tracks.get(obs.track_id)
        if track is None:
            # Track may have been purged between tipping and observation
            # returning; drop silently — the EO call itself is still logged
            # via the websocket, but there's no custody transition to emit.
            return None

        # Always stamp the latest observation so the dashboard has something
        # to display, regardless of whether state changes below.
        track.last_eo_obs = obs

        # Handle the three meaningful cases.
        if obs.frame_kind == "no_visual":
            # No change. Don't even emit a log — the UI shows the obs via
            # the `eo_observation` WS message already.
            return None

        if obs.frame_kind == "contradiction":
            # Don't mutate state. Emit a log so the timeline shows the flag.
            return CustodyStateLog(
                id=new_id("log_"),
                track_id=track.track_id,
                timestamp=utc_now(),
                previous_state=track.custody_state,  # type: ignore[arg-type]
                new_state=track.custody_state,       # type: ignore[arg-type]  # unchanged
                action_cue=(
                    "EO disagreement — possible spoofing or RF deception. "
                    "Do NOT auto-elevate. Request S2 verification."
                ),
                evidence_summary=(
                    f"eo_class={obs.classification}, eo_conf={obs.confidence:.2f}, "
                    f"bearing={obs.bearing_deg:.0f}°"
                ),
                triggering_event_id=obs.triggering_event_id,
                evidence_modalities=["RF", "EO"],
            )

        # Real confirming visual frame (quadcopter / fixed_wing / person etc).
        if obs.confirms_rf:
            track.visual_confirmed = True
            track.last_eo_confirm_ts = utc_now()
            prev = track.custody_state
            new_state: Optional[str] = None
            if prev == "DETECTED":
                new_state = "TRACKING"
                action_cue = (
                    f"EO confirmed visual on bearing {obs.bearing_deg:.0f}° "
                    f"({obs.classification}). Multi-modal custody opened."
                )
            elif prev == "VISUAL_LOST_RF_PRESENT":
                new_state = "REACQUIRED"
                action_cue = (
                    f"EO re-acquired target at bearing {obs.bearing_deg:.0f}°. "
                    f"Custody restored to multi-modal hold."
                )
            elif prev == "REACQUIRED":
                new_state = "TRACKING"
                action_cue = (
                    "Visual hold stable after reacquisition. "
                    "Custody promoted back to TRACKING."
                )
            # else: already TRACKING — no state change needed; stamping is enough.

            if new_state is None:
                return None

            track.custody_state = new_state  # type: ignore[assignment]
            return CustodyStateLog(
                id=new_id("log_"),
                track_id=track.track_id,
                timestamp=utc_now(),
                previous_state=prev,  # type: ignore[arg-type]
                new_state=new_state,  # type: ignore[arg-type]
                action_cue=action_cue,
                evidence_summary=(
                    f"eo_class={obs.classification}, conf={obs.confidence:.2f}, "
                    f"range≈{obs.range_m_estimate}m, slew={obs.slew_time_ms}ms"
                ),
                triggering_event_id=obs.triggering_event_id,
                evidence_modalities=["RF", "EO"],
            )

        # Non-confirming but non-contradictory (e.g. `bird`): no state change.
        return None

    # ------------------------------------------------------------------
    def sweep_timeouts(self) -> list[CustodyStateLog]:
        """Mark idle tracks as TRACK_LOST, degrade visual-stale ones, and purge ancient ones.

        Three responsibilities (run every few seconds from the main loop):
          1. Tracks idle longer than TRACK_TIMEOUT_SECONDS → TRACK_LOST.
          2. TRACKING tracks whose last EO confirmation is older than
             VISUAL_LOST_WINDOW_SECONDS → VISUAL_LOST_RF_PRESENT. RF is
             still holding the track; we just flag that EO lost lock.
          3. Tracks already in a terminal state and idle longer than
             TRACK_LOST_PURGE_SECONDS → dropped from the in-memory dict
             so the live custody timeline stays human-readable. Their
             custody logs persist (we only trim `self.tracks`).
        """
        out: list[CustodyStateLog] = []
        purge_ids: list[str] = []
        now_dt = utc_now()
        for track in list(self.tracks.values()):
            age = (now_dt - track.last_seen).total_seconds()
            if track.custody_state in ("TRACK_LOST", "CLEARED", "DISMISSED"):
                if age > TRACK_LOST_PURGE_SECONDS:
                    purge_ids.append(track.track_id)
                continue

            # Rule 2: visual-stale on a TRACKING or REACQUIRED track.
            if (
                track.custody_state in ("TRACKING", "REACQUIRED")
                and track.visual_confirmed
                and track.last_eo_confirm_ts is not None
            ):
                eo_age = (now_dt - track.last_eo_confirm_ts).total_seconds()
                if eo_age > VISUAL_LOST_WINDOW_SECONDS:
                    log = self.transition(
                        track.track_id,
                        "VISUAL_LOST_RF_PRESENT",
                        action_cue=(
                            f"EO lost visual on track {track.track_id} "
                            f"({int(eo_age)}s since last confirm). RF still holds."
                        ),
                        evidence_summary=f"last_eo_confirm={track.last_eo_confirm_ts.isoformat()}",
                        evidence_modalities=["RF"],
                    )
                    if log:
                        out.append(log)
                    # Don't also TRACK_LOST in the same sweep.
                    continue

            # Rule 1: RF-stale.
            if age > TRACK_TIMEOUT_SECONDS:
                log = self.transition(
                    track.track_id,
                    "TRACK_LOST",
                    action_cue=f"Track {track.track_id} timed out — no fresh detections in {int(age)}s.",
                    evidence_summary=f"last_seen={track.last_seen.isoformat()}",
                )
                if log:
                    out.append(log)

        # Drop dead tracks and any stale recency-cache entries pointing to
        # them, so a fresh detection of the same class opens a clean track.
        if purge_ids:
            dead = set(purge_ids)
            for tid in purge_ids:
                self.tracks.pop(tid, None)
            self._recent_by_class = {
                k: v for k, v in self._recent_by_class.items() if v[0] not in dead
            }
        return out
