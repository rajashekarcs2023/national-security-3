"""Phase E — Attribution engine.

Inputs:
  * one ``RFSignalReading``         — raw RF feature event
  * one ``ClassifiedSignal``        — what the edge ML classifier thinks
  * (optional) (lat, lon)           — best-known emitter position, e.g. from the TDOA fix

Output:
  * one ``AttributionResult``       — verdict + confidence + best library match
                                      + (if BLUE_ATTRIBUTED) friendly unit
                                      + per-feature scores + reason

Design
------
For every ``EmitterProfile`` in ``STATE.emitter_library`` we compute four
sub-scores in [0, 1]:

  * ``freq``       — how well the detection's centre frequency falls inside
                     the profile's expected band. 1.0 inside, exponential
                     decay outside.
  * ``bw``         — match between detection bandwidth and profile's
                     ``nominal_bandwidth_mhz``. Wide-tolerance gaussian.
  * ``pattern``    — exact / soft match of burst pattern strings.
  * ``class``      — soft prior from the edge ML classifier ("does the
                     classifier's predicted class look like this profile?").

We blend them with a fixed weight vector and pick the best profile. Then
we apply *blue-force-proximity gating*: if the best profile is friendly,
we look up the closest blue unit operating that emitter id and require it
to be inside ``ATTRIBUTION_RADIUS_M`` metres of the detection. If yes →
``BLUE_ATTRIBUTED``. If no → ``UNEXPLAINED`` with reason
``friendly_profile_no_friendly_in_range`` — this is the impersonation /
deception case the mentor highlighted.

Verdict thresholds:
  * ``best_score >= RED_THRESHOLD``   and side==adversary_simulated → RED_KNOWN
  * ``best_score >= BLUE_THRESHOLD``  and side==friendly + unit nearby → BLUE_ATTRIBUTED
  * ``best_score >= AMBIG_THRESHOLD`` (any side) → AMBIGUOUS
  * else                                            → UNEXPLAINED
"""

from __future__ import annotations

import math
from typing import Optional

from app.pipeline.blue_force import BlueForceFeed
from app.schemas import (
    AttributionResult,
    BlueForceUnit,
    ClassifiedSignal,
    EmitterProfile,
    RFSignalReading,
)
from app.state import STATE

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------

# Blue unit must be within this many metres of the detection for the
# verdict to be BLUE_ATTRIBUTED. 800 m is roughly a city block in the demo
# AO and matches typical man-pack VHF / UHF operating range when the
# emitter is near the ground.
ATTRIBUTION_RADIUS_M: float = 800.0

# Score thresholds. Calibrated so:
#   * a clean DJI 2.4 GHz hit scores ~0.85 → RED_KNOWN
#   * a friendly PRC-148 burst scores ~0.80 + blue unit at 100 m → BLUE_ATTRIBUTED
#   * a 15 GHz unknown-Ku burst with no profile match scores ~0.30 → UNEXPLAINED
RED_THRESHOLD: float = 0.65
BLUE_THRESHOLD: float = 0.55
AMBIG_THRESHOLD: float = 0.40

# Weighted-average weights for the four sub-scores. They sum to 1.
WEIGHTS = {
    "freq": 0.40,
    "bw": 0.25,
    "pattern": 0.15,
    "class": 0.20,
}


# ----------------------------------------------------------------------------
# Class -> profile-side priors
# ----------------------------------------------------------------------------
# The edge ML classifier already tells us something about the signal.
# These priors translate its predicted_class into a soft preference for
# certain library entries. Not a hard gate — we still let library
# matching dominate.

_FRIENDLY_HINT_CLASSES = {"friendly_radio_burst"}
_ADVERSARY_HINT_CLASSES = {"drone_control_repeated_burst", "frequency_hopping"}
_CIVILIAN_HINT_CLASSES = {"commercial_continuous"}
# An impersonation / mismatch flag from the classifier — biases AWAY from
# friendly attribution even if frequencies look friendly.
_IMPERSONATION_HINT_CLASSES = {"friendly_profile_mismatch"}


# ----------------------------------------------------------------------------
# Sub-score helpers
# ----------------------------------------------------------------------------

def _freq_score(detection_mhz: float, p_min: float, p_max: float) -> float:
    """1.0 if inside [p_min, p_max], exp-decay outside (1 MHz half-life)."""
    if p_min <= detection_mhz <= p_max:
        return 1.0
    if detection_mhz < p_min:
        delta = p_min - detection_mhz
    else:
        delta = detection_mhz - p_max
    # Tighten the decay for narrowband emitters, loosen for wideband.
    band = max(p_max - p_min, 0.05)
    decay = math.exp(-delta / max(band * 0.25, 1.0))
    return max(decay, 0.0)


def _bw_score(detection_bw_mhz: float, profile_bw_mhz: Optional[float]) -> float:
    """Gaussian-ish match around profile.nominal_bandwidth_mhz."""
    if profile_bw_mhz is None or profile_bw_mhz <= 0:
        return 0.5  # no opinion, neutral
    if detection_bw_mhz <= 0:
        return 0.3
    # Use log ratio so 2x off and 0.5x off are equally penalised.
    ratio = detection_bw_mhz / profile_bw_mhz
    log_r = math.log(ratio)
    # σ of 0.7 means: 1.0 at exact match, ~0.6 at ±50%, ~0.1 at 4x/0.25x.
    return float(math.exp(-(log_r ** 2) / (2.0 * 0.7 ** 2)))


# Tiny normalisation: synthesised burst patterns from the emulator vs.
# library expected_pattern. The two vocabularies are similar but not
# identical — soft-merge the synonyms.
_PATTERN_SYNONYMS = {
    "single_burst": "short_burst",
    "repeated_short_bursts": "repeated_short_bursts",
    "short_burst": "short_burst",
    "continuous": "continuous",
    "frequency_hopping": "frequency_hopping",
    "none": "none",
}


def _pattern_score(detection_pattern: str, profile_pattern: str) -> float:
    a = _PATTERN_SYNONYMS.get(detection_pattern, detection_pattern)
    b = _PATTERN_SYNONYMS.get(profile_pattern, profile_pattern)
    if a == b:
        return 1.0
    # Pattern families overlap softly (a single burst could be filed under
    # repeated bursts if the gap is long).
    soft_pairs = {
        ("short_burst", "repeated_short_bursts"),
        ("repeated_short_bursts", "short_burst"),
    }
    if (a, b) in soft_pairs:
        return 0.7
    return 0.3


def _class_score(predicted_class: str, profile: EmitterProfile) -> float:
    """Soft prior from the edge ML classifier."""
    side = profile.side
    if predicted_class in _IMPERSONATION_HINT_CLASSES:
        # Classifier flagged this as impersonation. Strongly prefer NOT
        # attributing it to a friendly profile.
        return 0.2 if side == "friendly" else 0.6
    if predicted_class in _FRIENDLY_HINT_CLASSES:
        return 0.95 if side == "friendly" else 0.4
    if predicted_class in _ADVERSARY_HINT_CLASSES:
        return 0.95 if side == "adversary_simulated" else 0.4
    if predicted_class in _CIVILIAN_HINT_CLASSES:
        return 0.95 if side == "civilian" else 0.4
    # No strong prior (background_noise, chirp, unknown_ood, ...) —
    # weak but non-zero.
    return 0.5


# ----------------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------------

def _score_profile(
    reading: RFSignalReading,
    classified: ClassifiedSignal,
    profile: EmitterProfile,
) -> dict[str, float]:
    bw_mhz = reading.bandwidth_khz / 1000.0
    sub = {
        "freq": _freq_score(reading.center_frequency_mhz, profile.expected_freq_min_mhz, profile.expected_freq_max_mhz),
        "bw": _bw_score(bw_mhz, profile.nominal_bandwidth_mhz),
        "pattern": _pattern_score(reading.burst_pattern, profile.expected_pattern),
        "class": _class_score(classified.predicted_class, profile),
    }
    sub["combined"] = sum(WEIGHTS[k] * sub[k] for k in WEIGHTS)
    return sub


def attribute(
    reading: RFSignalReading,
    classified: ClassifiedSignal,
    blue_force: Optional[BlueForceFeed] = None,
    fix_lat: Optional[float] = None,
    fix_lon: Optional[float] = None,
    attribution_radius_m: float = ATTRIBUTION_RADIUS_M,
) -> AttributionResult:
    """Attribute one detection to a library entry + return verdict.

    ``fix_lat`` / ``fix_lon`` come from the TDOA solver. If absent we fall
    back to the reading's own ``lat`` / ``lon`` (sensor position) which is
    a much weaker estimate of where the emitter actually is.
    """
    library = STATE.emitter_library
    if not library:
        return AttributionResult(
            verdict="UNEXPLAINED",
            confidence=0.0,
            reason="no_library",
        )

    fix_lat = fix_lat if fix_lat is not None else reading.lat
    fix_lon = fix_lon if fix_lon is not None else reading.lon

    # Score every profile, sort descending.
    scored: list[tuple[EmitterProfile, dict[str, float]]] = []
    for p in library:
        scored.append((p, _score_profile(reading, classified, p)))
    scored.sort(key=lambda x: x[1]["combined"], reverse=True)

    best, best_scores = scored[0]
    best_score = best_scores["combined"]

    # Build runner-ups for the UI.
    runner_ups: list[dict] = []
    for p, s in scored[1:4]:
        runner_ups.append({
            "emitter_id": p.id,
            "name": p.name,
            "side": p.side,
            "score": round(s["combined"], 3),
            "feature_scores": {k: round(s[k], 3) for k in WEIGHTS},
        })

    feature_scores = {k: round(best_scores[k], 3) for k in WEIGHTS}

    # No reasonable match → UNEXPLAINED.
    if best_score < AMBIG_THRESHOLD:
        return AttributionResult(
            verdict="UNEXPLAINED",
            confidence=round(1.0 - best_score, 3),
            best_emitter_id=best.id,
            best_emitter_name=best.name,
            best_score=round(best_score, 3),
            feature_scores=feature_scores,
            runner_ups=runner_ups,
            reason=(
                f"No library entry scored above {AMBIG_THRESHOLD:.2f}. "
                f"Closest miss: {best.name} at {best_score:.2f}."
            ),
        )

    # ---- Hard guard: if the classifier flagged the signal as a friendly
    # profile mismatch, refuse to attribute to blue *even if* a friendly
    # unit is right there. This is the deception / impersonation case
    # ("adversary is sitting on Blue-1's freq pretending to be Blue-1") —
    # surfacing it as UNEXPLAINED is what kicks off the geolocation +
    # COA workflow downstream.
    classifier_flagged_mismatch = classified.predicted_class in _IMPERSONATION_HINT_CLASSES
    if classifier_flagged_mismatch and best.side == "friendly":
        return AttributionResult(
            verdict="UNEXPLAINED",
            confidence=round(best_score, 3),
            best_emitter_id=best.id,
            best_emitter_name=best.name,
            best_score=round(best_score, 3),
            feature_scores=feature_scores,
            runner_ups=runner_ups,
            reason=(
                f"Classifier flagged this as friendly_profile_mismatch on the "
                f"{best.name} band — possible deception. Refusing blue attribution."
            ),
        )

    # ---- Friendly path: gate on blue-force proximity ----
    if best.side == "friendly" and best_score >= BLUE_THRESHOLD:
        unit: Optional[BlueForceUnit] = None
        distance: Optional[float] = None
        if blue_force is not None:
            unit, distance = blue_force.closest_unit_with_emitter(
                best.id, fix_lat, fix_lon
            )
        if unit is not None and distance is not None and distance <= attribution_radius_m:
            confidence = float(min(1.0, best_score * (1.0 - distance / (2 * attribution_radius_m))))
            return AttributionResult(
                verdict="BLUE_ATTRIBUTED",
                confidence=round(confidence, 3),
                best_emitter_id=best.id,
                best_emitter_name=best.name,
                best_score=round(best_score, 3),
                attributed_unit_id=unit.unit_id,
                attributed_unit_callsign=unit.callsign,
                distance_to_attributed_unit_m=round(distance, 1),
                feature_scores=feature_scores,
                runner_ups=runner_ups,
                reason=(
                    f"Match {best.name} at {best_score:.2f}; "
                    f"{unit.callsign} ({unit.unit_id}) is {distance:.0f} m away "
                    f"and is operating this radio."
                ),
            )
        # Friendly profile match BUT no friendly unit nearby → suspicious.
        # Possible impersonation / spoofing — surface as UNEXPLAINED.
        return AttributionResult(
            verdict="UNEXPLAINED",
            confidence=round(best_score, 3),
            best_emitter_id=best.id,
            best_emitter_name=best.name,
            best_score=round(best_score, 3),
            feature_scores=feature_scores,
            runner_ups=runner_ups,
            reason=(
                f"Looks like {best.name} but no friendly unit operating it "
                f"is within {attribution_radius_m:.0f} m. "
                "Possible impersonation or out-of-position friendly."
            ),
        )

    # ---- Adversary-simulated path ----
    if best.side == "adversary_simulated" and best_score >= RED_THRESHOLD:
        return AttributionResult(
            verdict="RED_KNOWN",
            confidence=round(best_score, 3),
            best_emitter_id=best.id,
            best_emitter_name=best.name,
            best_score=round(best_score, 3),
            feature_scores=feature_scores,
            runner_ups=runner_ups,
            reason=f"High-confidence library match: {best.name} ({best_score:.2f}).",
        )

    # ---- Civilian / mid-confidence / mixed ----
    if best.side == "civilian" and best_score >= AMBIG_THRESHOLD:
        return AttributionResult(
            verdict="AMBIGUOUS",
            confidence=round(best_score, 3),
            best_emitter_id=best.id,
            best_emitter_name=best.name,
            best_score=round(best_score, 3),
            feature_scores=feature_scores,
            runner_ups=runner_ups,
            reason=(
                f"Best match is civilian background ({best.name} at {best_score:.2f}). "
                "Continue monitoring."
            ),
        )

    # Catch-all AMBIGUOUS for mid-tier scores or weird side mixes.
    return AttributionResult(
        verdict="AMBIGUOUS",
        confidence=round(best_score, 3),
        best_emitter_id=best.id,
        best_emitter_name=best.name,
        best_score=round(best_score, 3),
        feature_scores=feature_scores,
        runner_ups=runner_ups,
        reason=(
            f"Mid-confidence match: {best.name} ({best.side}) at {best_score:.2f}. "
            "Operator review recommended."
        ),
    )
