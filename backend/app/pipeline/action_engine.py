"""Action cue engine.

Converts (RFSignalReading, ClassifiedSignal) into an IntelligenceEvent with
a templated, evidence-rich action cue. The Qwen LLM layer (pipeline/llm.py)
can later enrich the event's `llm_brief` field with a polished narrative.
"""

from __future__ import annotations

from typing import Optional

from app.schemas import (
    ClassifiedSignal,
    EventType,
    IntelligenceEvent,
    Priority,
    RFSignalReading,
    SyncStatus,
    new_id,
    utc_now,
)


CLASS_TO_EVENT_TYPE: dict[str, EventType] = {
    "background_noise": "FRIENDLY_EMISSION",
    "friendly_radio_burst": "FRIENDLY_EMISSION",
    "commercial_continuous": "FRIENDLY_EMISSION",
    "drone_control_repeated_burst": "POSSIBLE_UAS_ACTIVITY",
    "frequency_hopping": "RF_ANOMALY",
    "chirp": "RF_ANOMALY",
    "friendly_profile_mismatch": "PROFILE_MISMATCH",
    "unknown_ood": "RF_ANOMALY",
}


CLASS_TO_TITLE: dict[str, str] = {
    "drone_control_repeated_burst": "Possible drone control activity",
    "frequency_hopping": "Unknown frequency-hopping signal",
    "chirp": "RF chirp / sweep detected",
    "friendly_profile_mismatch": "Friendly emitter profile mismatch",
    "unknown_ood": "Unknown out-of-distribution RF pattern",
    "friendly_radio_burst": "Friendly Blue-2 emission",
    "commercial_continuous": "Commercial-band emission",
    "background_noise": "Background RF",
}


CLASS_TO_RECOMMENDED_ACTION: dict[str, str] = {
    "drone_control_repeated_burst": (
        "Increase sensitivity. Request visual / acoustic confirmation from the indicated sector."
    ),
    "frequency_hopping": (
        "Hold edge node in high-sensitivity mode. Sync compact anomaly event when network returns."
    ),
    "chirp": "Continue monitoring. Log anomaly for cross-correlation.",
    "friendly_profile_mismatch": (
        "Verify friendly unit emitter profile. Possible misconfiguration or spoof — flag to S2."
    ),
    "unknown_ood": (
        "Continue monitoring. Request visual confirmation. Sync compact anomaly event."
    ),
    "friendly_radio_burst": "Attribute to friendly Blue-2. No escalation.",
    "commercial_continuous": "Log as commercial; do not escalate.",
    "background_noise": "Filter locally.",
}


def _evidence_lines(reading: RFSignalReading, classified: ClassifiedSignal) -> list[str]:
    return [
        f"center_frequency_mhz={reading.center_frequency_mhz}",
        f"bandwidth_khz={reading.bandwidth_khz}",
        f"power_dbm={reading.power_dbm}",
        f"burst_pattern={reading.burst_pattern}",
        f"predicted_class={classified.predicted_class}",
        f"confidence={classified.confidence:.2f}",
        f"ood_score={classified.ood_score:.2f}",
        f"reconstruction_error={classified.reconstruction_error:.4f}",
        f"distance_to_nearest={classified.nearest_known_class}={classified.distance_to_nearest_centroid:.2f}",
        f"baseline_deviation={classified.baseline_deviation:.2f}",
    ]


def _summary(reading: RFSignalReading, classified: ClassifiedSignal) -> str:
    title = CLASS_TO_TITLE.get(classified.predicted_class, "RF event")
    parts = [
        f"{title} at {reading.center_frequency_mhz:.1f} MHz "
        f"({reading.bandwidth_khz} kHz, {reading.power_dbm:.1f} dBm)."
    ]
    if classified.is_anomaly:
        parts.append(
            f"OOD score {classified.ood_score:.2f}, "
            f"distance {classified.distance_to_nearest_centroid:.2f} from nearest known profile "
            f"({classified.nearest_known_class})."
        )
    if classified.baseline_deviation > 0.3:
        parts.append(f"Local baseline deviation {classified.baseline_deviation:.2f}.")
    return " ".join(parts)


def estimate_payload_size(event: IntelligenceEvent) -> int:
    """Estimate serialized JSON size in bytes (compact)."""
    return len(event.model_dump_json(exclude_none=True).encode("utf-8"))


# Estimate for "raw RF reading we DIDN'T need to send"
# 64x64 spectrogram at uint8 = 4096 bytes + ~256 bytes of metadata.
RAW_READING_BYTES_EST = 4096 + 256


def evaluate(
    reading: RFSignalReading,
    classified: ClassifiedSignal,
    network_online: bool,
) -> Optional[IntelligenceEvent]:
    """Decide whether to emit an intelligence event for this classification.

    Returns the event, or None if classified.action == 'ignore' (we just filter
    the reading out at the edge).
    """
    if classified.action == "ignore":
        return None

    title = CLASS_TO_TITLE.get(classified.predicted_class, "RF event")
    event_type = CLASS_TO_EVENT_TYPE.get(classified.predicted_class, "RF_ANOMALY")
    rec_action = CLASS_TO_RECOMMENDED_ACTION.get(
        classified.predicted_class,
        "Continue monitoring.",
    )

    sync_status: SyncStatus
    if classified.action == "log":
        sync_status = "local_only"
    elif classified.action == "queue":
        sync_status = "queued" if not network_online else "synced"
    else:
        sync_status = "synced" if network_online else "queued"

    event = IntelligenceEvent(
        id=new_id("evt_"),
        timestamp=utc_now(),
        site_id=reading.site_id,
        sensor_id=reading.sensor_id,
        track_id="",  # filled by custody manager
        event_type=event_type,
        title=title,
        summary=_summary(reading, classified),
        classification=classified.predicted_class,
        confidence=classified.confidence,
        ood_score=classified.ood_score,
        priority=classified.priority,  # type: ignore[arg-type]
        evidence=_evidence_lines(reading, classified),
        recommended_action=rec_action,
        sync_status=sync_status,
        network_state_at_detection="online" if network_online else "offline",
        raw_size_estimate_bytes=RAW_READING_BYTES_EST,
    )
    event.payload_size_bytes = estimate_payload_size(event)
    return event
