"""Phase F — Pydantic → Foundry stream-row adapter.

The Foundry FDE specified a flat, primitive-typed schema for each of the
seven streams (see ``foundry.md`` at the repo root). Our internal Pydantic
models are richer and use slightly different field names (e.g. ``id`` vs
``event_id``, ``ood_score`` vs ``anomaly_score``, ``recommended_action``
vs ``action_cue``).

This module is the **only** place that translation happens, so the rest
of the codebase keeps using natural Python names while the wire format
stays exactly what Foundry expects.

One mapper per stream, all pure functions, all return ``dict[str, Any]``.

Field-mapping notes
-------------------

For ``intelligence_event_row`` we accept an optional ``RFSignalReading``
because the Foundry stream schema includes RF features
(center_frequency_mhz, bandwidth_khz, power_dbm, duration_ms) that live
on the *reading*, not on our compact ``IntelligenceEvent``. The adapter
copies these across when the reading is available; otherwise the Foundry
row leaves them ``None`` and the streaming dataset's column accepts the
null.

Timestamps are emitted as ISO-8601 strings with ``Z`` suffix because
Foundry's streaming dataset parser is strictest about that shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.schemas import (
    AttributionResult,
    BlueForceUnit,
    EmitterProfile,
    IntelligenceEvent,
    PersistentEmitter,
    RFSignalReading,
    SensorNode,
    TdoaSolution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(ts: datetime | None) -> Optional[str]:
    """Render a datetime as a UTC ISO-8601 string with the trailing ``Z``.

    Foundry's streaming dataset Timestamp columns accept this exact shape
    without further parsing. Returns ``None`` if ``ts`` is None.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Stream 1 — intelligence_events_stream
# ---------------------------------------------------------------------------

def intelligence_event_row(
    event: IntelligenceEvent,
    reading: Optional[RFSignalReading] = None,
) -> dict[str, Any]:
    """Map an IntelligenceEvent (+ its triggering reading) to the wire row.

    Schema reference (from ``foundry.md``):

        event_id, timestamp, sensor_id, center_frequency_mhz,
        bandwidth_khz, power_dbm, duration_ms, modulation_hint,
        classification, priority, confidence, latitude, longitude,
        source_label, anomaly_score, action_cue
    """
    # Lat/lon: prefer the reading's GPS-tagged position; fall back to
    # the event's site fixed coords if the reading is missing.
    lat = reading.lat if reading is not None else None
    lon = reading.lon if reading is not None else None
    # NOTE on integer coercion: when these streams were created on the
    # hackathon Foundry tenant from a JSON sample, Foundry's type inferrer
    # saw e.g. ``2412.0`` and ``-45.0`` and assigned them as ``integer``
    # columns. We round to int here so push records pass schema validation.
    # ``power_dbm`` rounding loses sub-dB resolution — acceptable for the
    # demo; if the schema is later widened to float, change ``int(round(...))``
    # back to ``float(...)``.
    return {
        "event_id": event.id,
        "timestamp": _iso(event.timestamp),
        "sensor_id": event.sensor_id,
        "center_frequency_mhz": int(round(reading.center_frequency_mhz)) if reading else None,
        "bandwidth_khz": int(reading.bandwidth_khz) if reading else None,
        "power_dbm": int(round(reading.power_dbm)) if reading else None,
        "duration_ms": int(reading.duration_ms) if reading else None,
        # The Foundry schema asks for "modulation_hint". Our reading has a
        # ``burst_pattern`` (e.g. "short_burst", "frequency_hopping") which
        # is the closest hint we expose at this layer.
        "modulation_hint": reading.burst_pattern if reading else None,
        "classification": event.classification,
        "priority": event.priority,
        "confidence": float(event.confidence),
        "latitude": lat,
        "longitude": lon,
        # Provenance: where the reading came from (emulator vs RadioML real I/Q).
        "source_label": (reading.raw_source if reading else None) or "emulator",
        "anomaly_score": float(event.ood_score),
        "action_cue": event.recommended_action,
    }


# ---------------------------------------------------------------------------
# Stream 2 — attribution_results_stream
# ---------------------------------------------------------------------------

def attribution_row(
    attribution: AttributionResult,
    *,
    event_id: str,
    track_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Schema:
        attribution_id, event_id, timestamp, verdict, attributed_unit_id,
        feature_scores, confidence
    """
    # ``feature_scores`` is a JSON-string column in Foundry — flatten the
    # dict into a string the analyst can re-parse downstream.
    return {
        "attribution_id": f"att_{event_id}",
        "event_id": event_id,
        "timestamp": _iso(timestamp or datetime.now(timezone.utc)),
        "verdict": attribution.verdict,
        "attributed_unit_id": attribution.attributed_unit_id,
        "feature_scores": json.dumps(attribution.feature_scores, sort_keys=True),
        "confidence": float(attribution.confidence),
    }


# ---------------------------------------------------------------------------
# Stream 3 — tdoa_fixes_stream
# ---------------------------------------------------------------------------

def tdoa_fix_row(
    fix: TdoaSolution,
    *,
    event_id: str,
) -> dict[str, Any]:
    """Schema:
        fix_id, event_id, timestamp, latitude, longitude, cep_meters, method
    """
    return {
        "fix_id": f"fix_{event_id}",
        "event_id": event_id,
        "timestamp": _iso(fix.timestamp),
        "latitude": float(fix.lat),
        "longitude": float(fix.lon),
        # Foundry stream column inferred ``integer`` from sample values.
        "cep_meters": int(round(fix.cep_m)),
        "method": fix.method,
    }


# ---------------------------------------------------------------------------
# Stream 4 — persistent_emitters_stream
# ---------------------------------------------------------------------------

def persistent_emitter_row(pe: PersistentEmitter) -> dict[str, Any]:
    """Schema:
        emitter_id, first_seen, last_seen, center_frequency_mhz,
        latitude, longitude, event_count, classification, threat_level
    """
    # Our PersistentEmitter doesn't yet carry a representative frequency
    # (the cluster spans many readings). Leave it null — the streaming
    # dataset's column type allows it; Foundry analysts can join back via
    # ``event_count`` references against ``intelligence_events_stream``.
    return {
        "emitter_id": pe.id,
        "first_seen": _iso(pe.first_seen),
        "last_seen": _iso(pe.last_seen),
        "center_frequency_mhz": None,
        "latitude": float(pe.lat),
        "longitude": float(pe.lon),
        "event_count": int(pe.n_detections),
        "classification": pe.signal_class,
        # Map our priority into the more military-flavoured label the
        # FDE schema asked for.
        "threat_level": _priority_to_threat(pe.priority),
    }


def _priority_to_threat(p: str) -> str:
    return {
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }.get(p, p.upper())


# ---------------------------------------------------------------------------
# Stream 5 — blue_force_units_stream
# ---------------------------------------------------------------------------

def blue_force_row(unit: BlueForceUnit) -> dict[str, Any]:
    """Schema:
        unit_id, timestamp, callsign, latitude, longitude, unit_type, status
    """
    return {
        "unit_id": unit.unit_id,
        "timestamp": _iso(unit.last_update),
        "callsign": unit.callsign,
        "latitude": float(unit.lat),
        "longitude": float(unit.lon),
        # We don't model unit_type explicitly; infer from callsign prefix
        # to give the analyst something useful.
        "unit_type": _unit_type_from_callsign(unit.callsign),
        "status": "ACTIVE",
    }


def _unit_type_from_callsign(callsign: str) -> str:
    """Heuristic mapping from callsign prefix to unit type for Foundry."""
    cs = callsign.upper()
    if cs.startswith("RAIDER"):
        return "GROUND_TEAM"
    if cs.startswith("HAWK") or cs.startswith("EAGLE"):
        return "AIR"
    if cs.startswith("VICTOR"):
        return "VEHICLE"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Stream 6 — sensor_nodes_stream
# ---------------------------------------------------------------------------

def sensor_node_row(node: SensorNode) -> dict[str, Any]:
    """Schema:
        sensor_id, sensor_name, latitude, longitude, sensor_types,
        status, firmware_version
    """
    return {
        "sensor_id": node.id,
        "sensor_name": node.name,
        "latitude": float(node.lat),
        "longitude": float(node.lon),
        # Comma-separated list — keeps the streaming column simple while
        # still being parseable by a downstream transform.
        "sensor_types": "RF",
        "status": node.status.upper(),
        "firmware_version": "1.4.2",
    }


# ---------------------------------------------------------------------------
# Stream 7 — emitter_profiles_stream
# ---------------------------------------------------------------------------

def emitter_profile_row(p: EmitterProfile) -> dict[str, Any]:
    """Schema:
        profile_id, emitter_name, frequency_min_mhz, frequency_max_mhz,
        modulation, affiliation, threat_category
    """
    return {
        "profile_id": p.id,
        "emitter_name": p.name,
        # Foundry stream columns inferred ``integer`` from sample values.
        "frequency_min_mhz": int(round(p.expected_freq_min_mhz)),
        "frequency_max_mhz": int(round(p.expected_freq_max_mhz)),
        "modulation": p.modulation or "unknown",
        "affiliation": p.side,
        # Threat category derives from affiliation: friendly emitters are
        # never threats; civilian is benign; adversary_simulated is the
        # only thing a commander needs to act on.
        "threat_category": _affiliation_to_threat(p.side),
    }


def _affiliation_to_threat(side: str) -> str:
    return {
        "friendly": "BLUE",
        "civilian": "BENIGN",
        "adversary_simulated": "RED",
        "unknown": "UNKNOWN",
    }.get(side, "UNKNOWN")
