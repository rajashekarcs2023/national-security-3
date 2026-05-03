"""End-to-end verification: build a fake instance of every Pydantic
model, run it through ``foundry_adapter``, and push the row to its
real Foundry stream. Reports per-stream pass/fail.

Run from repo root:

    backend/.venv/bin/python scripts/verify_all_streams.py
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Make ``app`` importable from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

load_dotenv()

from app.pipeline import foundry_adapter  # noqa: E402
from app.schemas import (  # noqa: E402
    AttributionResult,
    BlueForceUnit,
    EmitterProfile,
    IntelligenceEvent,
    PersistentEmitter,
    RFSignalReading,
    SensorNode,
    TdoaSolution,
)

stack = (
    os.environ.get("FOUNDRY_STACK_URL") or "https://nshackathon.palantirfoundry.com"
).rstrip("/")
token = os.environ.get("FOUNDRY_API") or os.environ.get("FOUNDRY_TOKEN") or ""
rids_raw = os.environ.get("FOUNDRY_STREAM_RIDS", "")
rids: dict[str, str] = json.loads(rids_raw) if rids_raw else {}

if not token or not rids:
    print("Missing FOUNDRY_API or FOUNDRY_STREAM_RIDS in .env", file=sys.stderr)
    sys.exit(1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------
# Build a synthetic instance of every Pydantic model + adapt it.
# -----------------------------------------------------------------

reading = RFSignalReading(
    id="r_verify_001",
    sensor_id="EDGE-VERIFY-01",
    timestamp=_now(),
    center_frequency_mhz=2412.0,
    bandwidth_khz=20000,
    power_dbm=-45.0,
    duration_ms=250,
    burst_pattern="short_burst",
    dominant_freq_bin=12,
    energy=0.42,
    lat=34.0522,
    lon=-118.2437,
    raw_source="emulator",
)

event = IntelligenceEvent(
    id="evt_verify_001",
    track_id="trk_verify_001",
    sensor_id="EDGE-VERIFY-01",
    timestamp=_now(),
    event_type="RF_ANOMALY",
    title="verify event",
    summary="end-to-end verification fake event",
    classification="commercial_wifi",
    priority="low",
    confidence=0.92,
    ood_score=0.05,
    evidence=["verify-only"],
    recommended_action="ignore",
    network_state_at_detection="online",
    payload_size_bytes=600,
    sync_status="synced",
)

attribution = AttributionResult(
    verdict="BLUE_ATTRIBUTED",
    confidence=0.72,
    best_emitter_id="emitter_blue_radio",
    best_emitter_name="Blue Radio",
    best_score=0.78,
    attributed_unit_id="RAIDER-1",
    attributed_unit_callsign="RAIDER-1",
    distance_to_attributed_unit_m=240.0,
    feature_scores={"freq_match": 0.8, "spatial_match": 0.6},
    reason="verify",
)

tdoa = TdoaSolution(
    lat=34.0522,
    lon=-118.2437,
    cep_m=120.7,
    residual_m=5.4,
    sensor_ids=["EDGE-01", "EDGE-02", "EDGE-03"],
    gdop=2.1,
    method="chan_1994",
    timestamp=_now(),
)

persistent = PersistentEmitter(
    id="pe_verify_001",
    first_seen=_now(),
    last_seen=_now(),
    n_detections=4,
    lat=34.05,
    lon=-118.24,
    radius_m=180.0,
    signal_class="unknown_pulsed",
    priority="medium",
    detection_event_ids=["evt_verify_001"],
)

unit = BlueForceUnit(
    unit_id="RAIDER-1",
    callsign="RAIDER-1",
    lat=34.06,
    lon=-118.23,
    last_update=_now(),
)

sensor = SensorNode(
    id="EDGE-VERIFY-01",
    name="Verify edge",
    lat=34.05,
    lon=-118.24,
)

profile = EmitterProfile(
    id="prof_verify_001",
    name="Verify profile",
    side="adversary_simulated",
    expected_freq_min_mhz=2400.0,
    expected_freq_max_mhz=2500.0,
    expected_pattern="frequency_hopping",
    modulation="PSK",
)


# -----------------------------------------------------------------
# Adapt + push.
# -----------------------------------------------------------------

cases: list[tuple[str, dict]] = [
    ("intelligence_event", foundry_adapter.intelligence_event_row(event, reading)),
    ("attribution", foundry_adapter.attribution_row(attribution, event_id=event.id)),
    ("tdoa_fix", foundry_adapter.tdoa_fix_row(tdoa, event_id=event.id)),
    ("persistent_emitter", foundry_adapter.persistent_emitter_row(persistent)),
    ("blue_force_unit", foundry_adapter.blue_force_row(unit)),
    ("sensor_node", foundry_adapter.sensor_node_row(sensor)),
    ("emitter_profile", foundry_adapter.emitter_profile_row(profile)),
]

print(f"stack: {stack}")
print(f"token: ********{token[-6:] if len(token) > 6 else ''}")
print()

passes = 0
failures: list[tuple[str, str]] = []

with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
    for stream_key, row in cases:
        rid = rids.get(stream_key)
        if not rid:
            print(f"SKIP {stream_key:20s}  (no RID configured)")
            continue
        url = (
            f"{stack}/api/v2/highScale/streams/datasets/{rid}"
            f"/streams/master/publishRecord"
        )
        try:
            r = client.post(
                url,
                json={"record": row},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            ok = r.status_code // 100 == 2
            tag = "OK " if ok else "FAIL"
            print(f"{tag} {stream_key:20s}  HTTP {r.status_code}")
            if not ok:
                err_body = r.text[:300] if r.text else "(no body)"
                print(f"      url:  {url[len(stack):]}")
                print(f"      row:  {json.dumps(row)[:200]}")
                print(f"      err:  {err_body}")
                failures.append((stream_key, err_body))
            else:
                passes += 1
        except httpx.RequestError as e:
            print(f"FAIL {stream_key:20s}  {type(e).__name__}: {e}")
            failures.append((stream_key, f"{type(e).__name__}: {e}"))

print()
print(f"Result: {passes}/{len(cases)} streams accepted the push.")
if failures:
    print()
    print("Failures:")
    for k, msg in failures:
        print(f"  - {k}: {msg[:200]}")
    sys.exit(1)
sys.exit(0)
