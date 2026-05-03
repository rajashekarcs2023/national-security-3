"""Course-of-Action (COA) recommender — Phase C.

Given a custody track and the current rules-of-engagement (ROE) posture,
this module produces a ranked list of COA options the operator can
choose from. Each option carries the rationale, ROE citation, and a
list of prerequisites so the operator can audit *why* the system is
suggesting it.

Design choices:

  * **Deterministic rule-based core.** A military operator needs to
    audit every recommendation against the ROE — opaque ML rankings
    fail that bar. The score is a transparent product of (a) baseline
    action weight, (b) track-appropriateness multiplier, and (c) ROE
    gate (1.0 allow / 0.0 deny). Nothing else.

  * **ROE is a hard filter, not a soft prior.** If the current ROE
    posture forbids an action, that option is filtered out entirely —
    it never appears in the list. This avoids the failure mode where
    a system suggests an unauthorised escalation and an operator
    rubber-stamps it.

  * **Multi-modal evidence drives confidence.** Engagement-class actions
    (jam, hand-off, engage) require visual confirmation OR multi-modal
    custody. RF-only tracks default to OBSERVE_AND_REPORT.

  * **Pure functions, no I/O.** This module never touches the network
    or state — it takes a UASTrack + ROE and returns a recommendation.
    The optional LLM rationale is built externally and attached later.

  * **Reversibility-first ordering.** Among same-score options we
    prefer the more reversible one (OBSERVE before JAM before ENGAGE).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from app.schemas import IntelligenceEvent, UASTrack


# ---------------------------------------------------------------------------
# ROE postures
# ---------------------------------------------------------------------------
#
# Ordered from most to least restrictive. Real ROE matrices are written
# per-mission and per-airspace; this 4-state ladder is the *minimum*
# any C-UAS / counter-RF tactical doctrine recognises and is sufficient
# to demonstrate the recommender concept.
#
#   HOLD_FIRE     - Observe / report only. No emissions, no kinetic.
#   WARNING_ONLY  - Add warnings, ID, low-power interrogation.
#   DEFENSIVE     - Add EW (jam), hand-off to interceptor.
#   WEAPONS_FREE  - Kinetic engagement authorised on positive hostile ID.

RoePosture = Literal["HOLD_FIRE", "WARNING_ONLY", "DEFENSIVE", "WEAPONS_FREE"]

ROE_POSTURES: tuple[RoePosture, ...] = (
    "HOLD_FIRE",
    "WARNING_ONLY",
    "DEFENSIVE",
    "WEAPONS_FREE",
)

ROE_DESCRIPTIONS: dict[RoePosture, str] = {
    "HOLD_FIRE": "Hold fire. Observe, report, and identify only. No emissions.",
    "WARNING_ONLY": "Issue warnings and conduct positive ID. No kinetic.",
    "DEFENSIVE": "Non-kinetic effects authorised: EW jam, sensor cueing, intercept hand-off.",
    "WEAPONS_FREE": "Kinetic engagement authorised against positive hostile ID per ROE matrix.",
}


# ---------------------------------------------------------------------------
# COA action catalogue.
# ---------------------------------------------------------------------------

CoaActionId = Literal[
    "OBSERVE_AND_REPORT",
    "INCREASE_SENSITIVITY",
    "REACQUIRE_VISUAL",
    "INVESTIGATE_AND_GEOLOCATE",  # Phase E — low-risk action for persistent unknowns
    "WARN_AND_QUERY",
    "HAND_OFF_INTERCEPTOR",
    "JAM_RF",
    "MARK_FRIENDLY",
    "ENGAGE_KINETIC",
    "DISMISS",
]


@dataclass(frozen=True)
class _ActionDef:
    """Static catalogue entry for a COA action.

    ``min_roe_index`` is the lowest ROE index (in ROE_POSTURES) at which
    this action is permitted. ``base_weight`` is the prior score before
    track-specific multipliers; higher = more aggressive / impactful.
    ``reversible`` is used as a tiebreaker so equally-scored options
    surface least-destructive first.
    """

    action_id: CoaActionId
    label: str
    description: str
    min_roe_index: int          # 0=HOLD_FIRE, 3=WEAPONS_FREE
    base_weight: float          # 0..1
    reversible: bool
    risk_level: Literal["low", "medium", "high"]
    estimated_time_seconds: int
    requires_visual_confirm: bool
    requires_positive_id: bool
    expected_outcome: str
    roe_citation: str           # which doctrinal section authorises it


_CATALOGUE: dict[CoaActionId, _ActionDef] = {
    "OBSERVE_AND_REPORT": _ActionDef(
        action_id="OBSERVE_AND_REPORT",
        label="Observe and report",
        description="Hold sensor on target, log all activity, escalate if pattern persists.",
        min_roe_index=0,
        base_weight=0.45,
        reversible=True,
        risk_level="low",
        estimated_time_seconds=30,
        requires_visual_confirm=False,
        requires_positive_id=False,
        expected_outcome="Continued passive collection. No emissions.",
        roe_citation="ROE §1 — passive ISR always permitted.",
    ),
    "INCREASE_SENSITIVITY": _ActionDef(
        action_id="INCREASE_SENSITIVITY",
        label="Increase edge sensitivity",
        description="Lower OOD threshold and raise classifier sensitivity in this sector.",
        min_roe_index=0,
        base_weight=0.50,
        reversible=True,
        risk_level="low",
        estimated_time_seconds=5,
        requires_visual_confirm=False,
        requires_positive_id=False,
        expected_outcome="Better detection of low-power emitters; higher false-alarm cost accepted.",
        roe_citation="ROE §1 — sensor parameter changes are non-emissive.",
    ),
    "REACQUIRE_VISUAL": _ActionDef(
        action_id="REACQUIRE_VISUAL",
        label="Re-cue EO / IR camera",
        description="Slew the gimbal to last known bearing and request fresh visual.",
        min_roe_index=0,
        base_weight=0.55,
        reversible=True,
        risk_level="low",
        estimated_time_seconds=10,
        requires_visual_confirm=False,
        requires_positive_id=False,
        expected_outcome="Multi-modal custody restored or visual loss confirmed.",
        roe_citation="ROE §1 — passive sensor cueing always permitted.",
    ),
    "INVESTIGATE_AND_GEOLOCATE": _ActionDef(
        # Phase E — action recommended by the persistence detector when a
        # cluster of unexplained detections crosses the promotion threshold
        # (3+ hits inside the same spatial window) but has not yet reached
        # the HAND_OFF_INTERCEPTOR threshold (5+). Still passive — it tasks
        # ISR assets to improve the fix and gather pattern-of-life without
        # emitting RF or committing an interceptor.
        action_id="INVESTIGATE_AND_GEOLOCATE",
        label="Investigate + refine geolocation",
        description=(
            "Dwell additional sensors on the persistent unknown emitter "
            "to improve CEP, observe pattern-of-life, and confirm the "
            "recurrence before committing an interceptor."
        ),
        min_roe_index=0,
        base_weight=0.58,
        reversible=True,
        risk_level="low",
        estimated_time_seconds=30,
        requires_visual_confirm=False,
        requires_positive_id=False,
        expected_outcome=(
            "Tighter fix, richer cluster, and a clearer pattern. No RF "
            "emissions; no interceptor committed."
        ),
        roe_citation="ROE §1 — passive ISR dwell always permitted.",
    ),
    "WARN_AND_QUERY": _ActionDef(
        action_id="WARN_AND_QUERY",
        label="Issue warning broadcast",
        description="Transmit identification challenge on UHF guard; request positive ID.",
        min_roe_index=1,
        base_weight=0.60,
        reversible=True,
        risk_level="medium",
        estimated_time_seconds=20,
        requires_visual_confirm=False,
        requires_positive_id=False,
        expected_outcome="Friendly mistakes can be cleared; hostile intent confirmed by non-response.",
        roe_citation="ROE §2 — warnings authorised within RF-permissive posture.",
    ),
    "HAND_OFF_INTERCEPTOR": _ActionDef(
        action_id="HAND_OFF_INTERCEPTOR",
        label="Hand off to interceptor",
        description="Cue manned/unmanned interceptor with custody packet and kill-chain reference.",
        min_roe_index=2,
        base_weight=0.75,
        reversible=False,
        risk_level="medium",
        estimated_time_seconds=60,
        requires_visual_confirm=True,
        requires_positive_id=False,
        expected_outcome="Interceptor takes custody; this node returns to area surveillance.",
        roe_citation="ROE §3 — handoff to interceptor authorised in defensive posture.",
    ),
    "JAM_RF": _ActionDef(
        action_id="JAM_RF",
        label="Activate RF countermeasure (jam)",
        description="Engage directional EW emitter on the target's command-link band.",
        min_roe_index=2,
        base_weight=0.70,
        reversible=True,
        risk_level="medium",
        estimated_time_seconds=15,
        requires_visual_confirm=False,
        requires_positive_id=True,
        expected_outcome="Loss of C2 link; UAS forced to RTB or land. No kinetic effect.",
        roe_citation="ROE §3 — non-kinetic EW authorised in defensive posture.",
    ),
    "MARK_FRIENDLY": _ActionDef(
        action_id="MARK_FRIENDLY",
        label="Mark friendly",
        description="Tag track as friendly; suppress further alerts and update emitter library.",
        min_roe_index=0,
        base_weight=0.40,
        reversible=True,
        risk_level="low",
        estimated_time_seconds=5,
        requires_visual_confirm=False,
        requires_positive_id=True,
        expected_outcome="Friendly callsign restored; operator workload reduced.",
        roe_citation="ROE §1 — friendly identification is always permitted.",
    ),
    "ENGAGE_KINETIC": _ActionDef(
        action_id="ENGAGE_KINETIC",
        label="Engage (kinetic)",
        description="Authorise kinetic engagement against positively-identified hostile UAS.",
        min_roe_index=3,
        base_weight=0.95,
        reversible=False,
        risk_level="high",
        estimated_time_seconds=45,
        requires_visual_confirm=True,
        requires_positive_id=True,
        expected_outcome="Target neutralised. Permanent. Operator and 2-up authority required.",
        roe_citation="ROE §4 — kinetic engagement on positive hostile ID, weapons-free posture.",
    ),
    "DISMISS": _ActionDef(
        action_id="DISMISS",
        label="Dismiss as nuisance",
        description="Log the track as a non-threat (commercial / spurious) and clear from the board.",
        min_roe_index=0,
        base_weight=0.30,
        reversible=True,
        risk_level="low",
        estimated_time_seconds=3,
        requires_visual_confirm=False,
        requires_positive_id=False,
        expected_outcome="Track closed. Operator workload reduced. No effect on emitter.",
        roe_citation="ROE §1 — administrative dispositions always permitted.",
    ),
}


# ---------------------------------------------------------------------------
# Recommendation output.
# ---------------------------------------------------------------------------

@dataclass
class CoaOption:
    action_id: CoaActionId
    label: str
    description: str
    score: float
    rationale: str
    roe_citation: str
    prerequisites: list[str]
    prerequisites_met: bool
    expected_outcome: str
    risk_level: str
    reversible: bool
    estimated_time_seconds: int


@dataclass
class CoaRecommendation:
    track_id: Optional[str]
    event_id: Optional[str]
    roe_posture: RoePosture
    roe_description: str
    threat_summary: str
    options: list[CoaOption]                # ranked, top first
    filtered_out: list[dict]                # actions denied by ROE, with reason
    generated_at_ts: float = 0.0            # filled by caller if needed
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _roe_index(posture: RoePosture) -> int:
    return ROE_POSTURES.index(posture)


def _has_recent_visual(track: Optional[UASTrack]) -> bool:
    if track is None:
        return False
    return bool(track.visual_confirmed and track.last_eo_obs and track.last_eo_obs.confirms_rf)


def _is_positive_hostile_id(track: Optional[UASTrack]) -> bool:
    """Best-available proxy for 'positive hostile ID' inside this demo.

    Real ROE matrices require human authority to declare positive ID;
    here we surface the *technical* prerequisites the system can verify
    automatically:
      * visual_confirmed AND last EO frame agreed with the RF call
      * AND track.classification is one of CONFIRMED_UAS / hostile-ish
      * AND custody_state is TRACKING or REACQUIRED (not just DETECTED)
    The operator still has to make the legal call.
    """
    if track is None:
        return False
    if not _has_recent_visual(track):
        return False
    if track.classification != "CONFIRMED_UAS":
        return False
    if track.custody_state not in ("TRACKING", "REACQUIRED"):
        return False
    return True


def _appropriateness(action: _ActionDef, track: Optional[UASTrack]) -> float:
    """Track-specific multiplier for an action's baseline weight.

    Returns a value in roughly [0.1 .. 1.4]. The numbers are tuned so
    that:
      * RF-only DETECTED tracks see OBSERVE / SENSITIVITY at the top.
      * Multi-modal TRACKING tracks see WARN / HAND_OFF / JAM at the top.
      * Visual-confirmed CONFIRMED_UAS in REACQUIRED state see
        HAND_OFF + JAM most strongly; ENGAGE only if WEAPONS_FREE.
      * VISUAL_LOST_RF_PRESENT pushes REACQUIRE_VISUAL up.
    """
    if track is None:
        # No track → only event-only actions matter (OBSERVE, MARK, DISMISS).
        if action.action_id in ("OBSERVE_AND_REPORT", "DISMISS"):
            return 1.0
        return 0.4

    custody = track.custody_state
    threat = track.threat_level
    visual = _has_recent_visual(track)
    pos_id = _is_positive_hostile_id(track)

    # Custody-state-driven boosts
    if action.action_id == "REACQUIRE_VISUAL":
        if custody == "VISUAL_LOST_RF_PRESENT":
            return 1.4
        if custody == "DETECTED" and not visual:
            return 1.15
        return 0.6

    if action.action_id == "INCREASE_SENSITIVITY":
        if custody in ("DETECTED", "VISUAL_LOST_RF_PRESENT"):
            return 1.1
        return 0.7

    if action.action_id == "OBSERVE_AND_REPORT":
        # Always available; strongest for low-confidence, low-threat tracks.
        if threat == "LOW":
            return 1.2
        if not visual:
            return 1.05
        return 0.85

    if action.action_id == "WARN_AND_QUERY":
        # Best for confirmed-but-not-yet-positive-ID tracks.
        if visual and not pos_id:
            return 1.2
        if pos_id:
            return 0.9
        return 0.6

    if action.action_id == "HAND_OFF_INTERCEPTOR":
        if pos_id and threat == "HIGH":
            return 1.3
        if visual and threat in ("MEDIUM", "HIGH"):
            return 1.0
        return 0.4

    if action.action_id == "JAM_RF":
        if pos_id:
            return 1.25
        if visual:
            return 1.0
        return 0.5

    if action.action_id == "ENGAGE_KINETIC":
        if pos_id and threat == "HIGH":
            return 1.0
        return 0.3  # surfaced but ranked low; operator must escalate ROE first

    if action.action_id == "MARK_FRIENDLY":
        # Strong only when classification looks like a friendly event.
        if track.classification in ("FRIENDLY", "CIVILIAN") or "friendly" in track.classification.lower():
            return 1.3
        return 0.4

    if action.action_id == "DISMISS":
        if threat == "LOW" and not visual:
            return 1.0
        return 0.4

    return 0.7


def _action_blocked_reason(
    action: _ActionDef,
    track: Optional[UASTrack],
    posture_idx: int,
) -> Optional[str]:
    """Return None if action is permitted, else a string reason."""
    if action.min_roe_index > posture_idx:
        return f"ROE posture {ROE_POSTURES[posture_idx]} below minimum {ROE_POSTURES[action.min_roe_index]}"
    return None


def _prereq_check(action: _ActionDef, track: Optional[UASTrack]) -> tuple[list[str], bool]:
    prereqs: list[str] = []
    if action.requires_visual_confirm:
        prereqs.append("EO/IR visual confirmation in last 6 s")
    if action.requires_positive_id:
        prereqs.append("Positive hostile ID (multi-modal CONFIRMED_UAS)")

    met = True
    if action.requires_visual_confirm and not _has_recent_visual(track):
        met = False
    if action.requires_positive_id and not _is_positive_hostile_id(track):
        met = False
    return prereqs, met


def _threat_summary(track: Optional[UASTrack]) -> str:
    if track is None:
        return "No active track. Recommendations are event-only."
    bits = []
    bits.append(f"Track {track.track_id} in {track.sector}.")
    bits.append(f"Custody {track.custody_state}, threat {track.threat_level}, classification {track.classification}.")
    if track.visual_confirmed and track.last_eo_obs and track.last_eo_obs.confirms_rf:
        eo = track.last_eo_obs
        bits.append(f"EO confirmed {eo.frame_kind} (conf {int(round(eo.confidence * 100))}%).")
    elif track.visual_confirmed:
        bits.append("EO had prior confirmation but latest frame did not agree.")
    else:
        bits.append("RF-only — no EO confirmation yet.")
    return " ".join(bits)


def _rationale(action: _ActionDef, track: Optional[UASTrack], appro: float) -> str:
    """Human-readable why-this-action sentence."""
    if track is None:
        return "Event-only context; no live custody track."
    visual = _has_recent_visual(track)
    pos_id = _is_positive_hostile_id(track)
    pieces = []
    if action.action_id == "REACQUIRE_VISUAL" and track.custody_state == "VISUAL_LOST_RF_PRESENT":
        pieces.append("Custody is RF-only — re-cue EO before any escalation.")
    if action.action_id == "OBSERVE_AND_REPORT" and not visual:
        pieces.append("Insufficient evidence for any active response.")
    if action.action_id == "WARN_AND_QUERY" and visual and not pos_id:
        pieces.append("Visual confirmed but ID still ambiguous — challenge first.")
    if action.action_id == "HAND_OFF_INTERCEPTOR" and pos_id:
        pieces.append("Multi-modal CONFIRMED_UAS — interceptor handoff is doctrine.")
    if action.action_id == "JAM_RF" and pos_id:
        pieces.append("Positive hostile ID with active C2 link — directional EW expected to break the link.")
    if action.action_id == "ENGAGE_KINETIC":
        if pos_id and track.threat_level == "HIGH":
            pieces.append("Highest-threat positive ID — kinetic option available if WEAPONS_FREE authorised.")
        else:
            pieces.append("Surfaced for completeness; prerequisites not met or ROE insufficient.")
    if action.action_id == "MARK_FRIENDLY" and "friendly" in track.classification.lower():
        pieces.append("Classification signals friendly emitter — confirm and clear.")
    if not pieces:
        pieces.append(f"Appropriateness score {appro:.2f} for current track posture.")
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def recommend(
    track: Optional[UASTrack],
    event: Optional[IntelligenceEvent],
    posture: RoePosture = "DEFENSIVE",
    *,
    top_n: int = 4,
) -> CoaRecommendation:
    """Build a ranked CoA recommendation for ``track`` under ``posture``.

    ``event`` is optional — it is only used to fill the ``event_id`` and
    let the caller know which intel event triggered the request. The
    actual ranking is driven by the live ``track`` state.
    """
    if posture not in ROE_POSTURES:
        raise ValueError(f"unknown ROE posture: {posture!r}")

    posture_idx = _roe_index(posture)

    options: list[CoaOption] = []
    filtered: list[dict] = []

    for action in _CATALOGUE.values():
        block = _action_blocked_reason(action, track, posture_idx)
        if block is not None:
            filtered.append({
                "action_id": action.action_id,
                "label": action.label,
                "reason": block,
            })
            continue
        appro = _appropriateness(action, track)
        score = action.base_weight * appro
        prereqs, met = _prereq_check(action, track)
        rationale = _rationale(action, track, appro)
        # If prerequisites not met, dampen the score so it ranks below
        # actions whose prereqs are met. We don't filter it out — the
        # operator should *see* it as "available but not actionable yet".
        if not met:
            score *= 0.4

        options.append(CoaOption(
            action_id=action.action_id,
            label=action.label,
            description=action.description,
            score=round(score, 4),
            rationale=rationale,
            roe_citation=action.roe_citation,
            prerequisites=prereqs,
            prerequisites_met=met,
            expected_outcome=action.expected_outcome,
            risk_level=action.risk_level,
            reversible=action.reversible,
            estimated_time_seconds=action.estimated_time_seconds,
        ))

    # Sort: score desc, then reversible-first, then min ETA, then label.
    options.sort(key=lambda o: (-o.score, not o.reversible, o.estimated_time_seconds, o.label))

    # Trim to top_n; ENGAGE_KINETIC always shown if not filtered out (it's
    # the doctrinally important "do not omit" rung even when score is low).
    top = options[:top_n]
    if any(o.action_id == "ENGAGE_KINETIC" for o in options) and not any(
        o.action_id == "ENGAGE_KINETIC" for o in top
    ):
        engage = next(o for o in options if o.action_id == "ENGAGE_KINETIC")
        top.append(engage)

    notes: list[str] = []
    if track is None:
        notes.append("No live track found; recommendations restricted to event-only actions.")
    if track is not None and not _has_recent_visual(track):
        notes.append("No fresh EO confirmation. Engagement-class actions will require it.")
    if posture == "HOLD_FIRE":
        notes.append("HOLD_FIRE posture — only passive actions are surfaced.")

    return CoaRecommendation(
        track_id=track.track_id if track is not None else None,
        event_id=event.id if event is not None else None,
        roe_posture=posture,
        roe_description=ROE_DESCRIPTIONS[posture],
        threat_summary=_threat_summary(track),
        options=top,
        filtered_out=filtered,
        notes=notes,
    )


# Convenience for the API layer / frontend: convert a recommendation to a
# JSON-serialisable dict (Pydantic-free so we don't fight model_dump on
# nested dataclasses).
def recommendation_to_dict(rec: CoaRecommendation) -> dict:
    return {
        "track_id": rec.track_id,
        "event_id": rec.event_id,
        "roe_posture": rec.roe_posture,
        "roe_description": rec.roe_description,
        "threat_summary": rec.threat_summary,
        "options": [o.__dict__ for o in rec.options],
        "filtered_out": rec.filtered_out,
        "notes": rec.notes,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from app.schemas import EOObservation, UASTrack, utc_now

    eo = EOObservation(
        track_id="TRK-DRON-NW-001",
        sector="NW",
        bearing_deg=314.0,
        slew_time_ms=620,
        frame_kind="quadcopter",
        classification="quadcopter",
        confidence=0.88,
        bbox=(0.4, 0.4, 0.1, 0.1),
        confirms_rf=True,
    )
    track = UASTrack(
        track_id="TRK-DRON-NW-001",
        custody_state="TRACKING",
        threat_level="HIGH",
        classification="CONFIRMED_UAS",
        confidence=0.9,
        sector="NW",
        last_known_lat=34.05, last_known_lon=-118.24, last_known_alt_m=120,
        n_detections=8,
        first_seen=utc_now(), last_seen=utc_now(),
        visual_confirmed=True,
        last_eo_obs=eo,
    )

    print("\n=== DEFENSIVE posture ===")
    rec = recommend(track, None, posture="DEFENSIVE")
    for o in rec.options:
        print(f"  {o.score:.2f}  {o.action_id:<22} prereq_met={o.prereqs_met if hasattr(o,'prereqs_met') else o.prerequisites_met}")
        print(f"      {o.rationale}")
    print(f"\nfiltered: {[f['action_id'] for f in rec.filtered_out]}")

    print("\n=== HOLD_FIRE posture ===")
    rec = recommend(track, None, posture="HOLD_FIRE")
    for o in rec.options:
        print(f"  {o.score:.2f}  {o.action_id}")
    print(f"filtered: {[f['action_id'] for f in rec.filtered_out]}")
