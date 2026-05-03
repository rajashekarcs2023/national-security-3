"""Foundry-shaped JSONL exports.

Mirrors the Palantir Foundry ontology described in context.md so a teammate
working on the Foundry side can ingest these files directly into the
existing datasets:

  sensor_events       <- RFSignalReading
  uas_tracks          <- UASTrack
  custody_state_log   <- CustodyStateLog
  edge_devices        <- EdgeDeviceStatus snapshot
  sync_queue          <- IntelligenceEvent (with sync_status)
  operator_actions    <- OperatorAction
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.schemas import (
    CustodyStateLog,
    EdgeDeviceStatus,
    IntelligenceEvent,
    OperatorAction,
    RFSignalReading,
    UASTrack,
)


EXPORT_DIR = Path(__file__).resolve().parent.parent.parent / "exports"
EXPORT_DIR.mkdir(exist_ok=True)


def _utc_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


# ---------------------------------------------------------------------------
# Per-row mappers (Pydantic -> Foundry-shaped dict)
# ---------------------------------------------------------------------------

def reading_to_foundry(r: RFSignalReading, classification: str | None = None) -> dict:
    """sensor_events ontology row.

    Strips the heavy spectrogram_u8 field — Foundry only needs the structured features.
    """
    return {
        "event_id": r.id,
        "track_id": None,
        "sensor_id": r.sensor_id,
        "site_id": r.site_id,
        "sensor_type": "RF",
        "timestamp": _utc_iso(r.timestamp),
        "frequency_mhz": r.center_frequency_mhz,
        "bandwidth_khz": r.bandwidth_khz,
        "signal_strength_dbm": r.power_dbm,
        "duration_ms": r.duration_ms,
        "burst_pattern": r.burst_pattern,
        "lat": r.lat,
        "lon": r.lon,
        "raw_source": r.raw_source,
        "predicted_class": classification,
        "confidence": None,
    }


def track_to_foundry(t: UASTrack) -> dict:
    return {
        "track_id": t.track_id,
        "site_id": t.site_id,
        "custody_state": t.custody_state,
        "threat_level": t.threat_level,
        "classification": t.classification,
        "confidence": t.confidence,
        "sector": t.sector,
        "last_known_lat": t.last_known_lat,
        "last_known_lon": t.last_known_lon,
        "last_known_alt_m": t.last_known_alt_m,
        "n_detections": t.n_detections,
        "first_seen": _utc_iso(t.first_seen),
        "last_seen": _utc_iso(t.last_seen),
    }


def event_to_foundry_sync_row(e: IntelligenceEvent) -> dict:
    """sync_queue ontology row."""
    return {
        "queue_id": e.id,
        "device_id": e.sensor_id,
        "track_id": e.track_id,
        "site_id": e.site_id,
        "timestamp": _utc_iso(e.timestamp),
        "event_type": e.event_type,
        "title": e.title,
        "summary": e.summary,
        "classification": e.classification,
        "confidence": e.confidence,
        "ood_score": e.ood_score,
        "sync_priority": e.priority.upper(),
        "status": e.sync_status.upper(),
        "payload_size_bytes": e.payload_size_bytes,
        "network_state_at_detection": e.network_state_at_detection,
        "recommended_action": e.recommended_action,
        "evidence": e.evidence,
        "llm_brief": e.llm_brief,
    }


def custody_log_to_foundry(c: CustodyStateLog) -> dict:
    return {
        "log_id": c.id,
        "track_id": c.track_id,
        "timestamp": _utc_iso(c.timestamp),
        "previous_state": c.previous_state,
        "new_state": c.new_state,
        "action_cue": c.action_cue,
        "evidence_summary": c.evidence_summary,
        "triggering_event_id": c.triggering_event_id,
    }


def device_status_to_foundry(d: EdgeDeviceStatus) -> dict:
    return {
        "device_id": d.device_id,
        "device_name": d.device_name,
        "site_id": d.site_id,
        "site_lat": d.site_lat,
        "site_lon": d.site_lon,
        "network_status": d.network_status,
        "sensitivity_mode": d.sensitivity_mode,
        "battery_pct": d.battery_pct,
        "sync_queue_depth": d.sync_queue_depth,
        "active_tracks": d.active_tracks,
        "total_readings_processed": d.total_readings_processed,
        "total_filtered_local": d.total_filtered_local,
        "total_events_synced": d.total_events_synced,
        "bytes_saved_at_edge": d.bytes_saved_at_edge,
        "bytes_actually_synced": d.bytes_actually_synced,
        "snapshot_ts": _utc_iso(datetime.now(timezone.utc)),
    }


def operator_action_to_foundry(a: OperatorAction) -> dict:
    return {
        "action_id": a.id,
        "track_id": a.track_id,
        "event_id": a.event_id,
        "action_type": a.action_type,
        "operator_id": a.operator_id,
        "timestamp": _utc_iso(a.timestamp),
        "details": a.details,
    }


# ---------------------------------------------------------------------------
# JSONL writers
# ---------------------------------------------------------------------------

def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    return path


def export_full_state(
    readings: list[RFSignalReading],
    classifications: dict[str, str],
    tracks: dict[str, UASTrack],
    custody_logs: list[CustodyStateLog],
    events: list[IntelligenceEvent],
    operator_actions: list[OperatorAction],
    device_status: EdgeDeviceStatus,
) -> dict[str, str]:
    """Dump the current runtime state into Foundry-shaped JSONL files.

    Returns a dict of {dataset_name: relative_path}.
    """
    out_paths: dict[str, str] = {}

    p = EXPORT_DIR / "sensor_events.jsonl"
    write_jsonl(p, [reading_to_foundry(r, classifications.get(r.id)) for r in readings])
    out_paths["sensor_events"] = p.name

    p = EXPORT_DIR / "uas_tracks.jsonl"
    write_jsonl(p, [track_to_foundry(t) for t in tracks.values()])
    out_paths["uas_tracks"] = p.name

    p = EXPORT_DIR / "custody_state_log.jsonl"
    write_jsonl(p, [custody_log_to_foundry(c) for c in custody_logs])
    out_paths["custody_state_log"] = p.name

    p = EXPORT_DIR / "sync_queue.jsonl"
    write_jsonl(p, [event_to_foundry_sync_row(e) for e in events])
    out_paths["sync_queue"] = p.name

    p = EXPORT_DIR / "operator_actions.jsonl"
    write_jsonl(p, [operator_action_to_foundry(a) for a in operator_actions])
    out_paths["operator_actions"] = p.name

    p = EXPORT_DIR / "edge_devices.jsonl"
    write_jsonl(p, [device_status_to_foundry(device_status)])
    out_paths["edge_devices"] = p.name

    return out_paths
