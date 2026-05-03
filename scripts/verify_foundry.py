#!/usr/bin/env python3
"""Pre-flight check for the real Palantir Foundry push.

Run this *before* launching the full backend so you find config bugs
in 3 seconds instead of 3 minutes:

    set -a; source .env; set +a
    python scripts/verify_foundry.py

It does exactly one thing: send a single fake ``IntelligenceEvent``-shaped
row to your ``intelligence_events_stream`` and report the HTTP response.

Exit codes
----------
* 0  — row accepted (HTTP 2xx).
* 1  — config missing / malformed (no env vars, bad JSON, etc.).
* 2  — auth failed (HTTP 401 / 403). Token is wrong or lacks scope.
* 3  — RID not found (HTTP 404). RID typo, or stream is on a different
       branch than ``master``.
* 4  — request timed out or connection refused. Network / VPN / DNS.
* 5  — server error (HTTP 5xx). Foundry-side; usually transient.
* 6  — schema rejection (HTTP 4xx other than auth/404). Inspect the
       response body for the offending field.

The script imports nothing from the running backend — it stands alone so
you can run it on any machine that has ``httpx`` + a token.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

# Auto-load .env so the operator doesn't have to remember `set -a; source .env`.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


VERIFY_STREAM_KEY = "intelligence_event"
DEFAULT_STACK_URL = "https://nshackathon.palantirfoundry.com"


def _abort(code: int, msg: str) -> None:
    print(f"FAIL — {msg}", file=sys.stderr)
    sys.exit(code)


def _classify_status(status: int, body: str) -> tuple[int, str]:
    """Map an HTTP status into our exit-code vocabulary + a friendly message."""
    if status // 100 == 2:
        return 0, "OK"
    if status in (401, 403):
        return 2, f"auth rejected (HTTP {status}). Check FOUNDRY_TOKEN scope."
    if status == 404:
        return 3, (
            f"stream not found (HTTP 404). Verify the RID for "
            f"'{VERIFY_STREAM_KEY}' and that the stream is on the master branch."
        )
    if status // 100 == 5:
        return 5, f"server error (HTTP {status}). Usually transient. Body: {body[:200]}"
    return 6, f"schema or request rejected (HTTP {status}). Body: {body[:400]}"


def _resolve_rid(stream_key: str) -> str | None:
    """Resolve the stream RID for ``stream_key`` from env vars.

    Mirrors the resolution in ``app.pipeline.foundry_remote``: the JSON
    map ``FOUNDRY_STREAM_RIDS`` is the base, the per-stream env var
    ``FOUNDRY_STREAM_RID_<KEY>`` overrides if set.
    """
    raw = os.environ.get("FOUNDRY_STREAM_RIDS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and stream_key in parsed:
                rid = str(parsed[stream_key]).strip()
                if rid:
                    pass  # fall through to override check
                    base_rid = rid
                else:
                    base_rid = ""
            else:
                base_rid = ""
        except json.JSONDecodeError:
            _abort(1, "FOUNDRY_STREAM_RIDS is not valid JSON")
    else:
        base_rid = ""
    override = os.environ.get(
        f"FOUNDRY_STREAM_RID_{stream_key.upper()}", ""
    ).strip()
    return (override or base_rid) or None


def _build_test_row() -> dict[str, Any]:
    """A minimal row that satisfies the ``intelligence_events_stream`` schema.

    Field names + types match exactly what ``foundry_adapter.intelligence_event_row``
    emits in production. Numeric fields the Foundry tenant inferred as
    ``integer`` are sent as ``int`` here so the record passes schema
    validation (otherwise we get ``Api:RecordDoesNotMatchStreamSchema``).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "event_id": f"verify_{int(datetime.now(timezone.utc).timestamp())}",
        "timestamp": now,
        "sensor_id": "EDGE-VERIFY-01",
        "center_frequency_mhz": 2412,         # int per stream schema
        "bandwidth_khz": 20000,
        "power_dbm": -45,                     # int per stream schema
        "duration_ms": 250,
        "modulation_hint": "short_burst",
        "classification": "verify_fake_event",
        "priority": "low",
        "confidence": 0.5,
        "latitude": 34.0522,
        "longitude": -118.2437,
        "source_label": "verify_script",
        "anomaly_score": 0.0,
        "action_cue": "ignore — pre-flight verification only",
    }


def main() -> None:
    stack = (
        os.environ.get("FOUNDRY_STACK_URL", "") or DEFAULT_STACK_URL
    ).rstrip("/")
    # Canonical name is FOUNDRY_API; fall back to FOUNDRY_TOKEN for compat.
    token = (
        os.environ.get("FOUNDRY_API", "")
        or os.environ.get("FOUNDRY_TOKEN", "")
    )
    if not token:
        _abort(
            1,
            "FOUNDRY_API is not set. Add it to .env (FOUNDRY_API=eyJ...) "
            "or export it. See .env.example.",
        )

    rid = _resolve_rid(VERIFY_STREAM_KEY)
    if not rid:
        _abort(
            1,
            f"no RID configured for '{VERIFY_STREAM_KEY}'. Set "
            "FOUNDRY_STREAM_RIDS (JSON map) or "
            f"FOUNDRY_STREAM_RID_{VERIFY_STREAM_KEY.upper()} in .env.",
        )

    # Foundry Streams v2 high-scale publishRecord endpoint, one record
    # per call, body shape ``{"record": <flat-row>}``.
    url = (
        f"{stack}/api/v2/highScale/streams/datasets/{rid}"
        f"/streams/master/publishRecord"
    )
    test_row = _build_test_row()
    payload = {"record": test_row}

    print(f"→ POST {url}")
    print(f"  body: 1 record, {len(json.dumps(payload))} bytes")

    try:
        with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
            r = client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as e:
        _abort(4, f"request failed: {type(e).__name__}: {e}")

    code, msg = _classify_status(r.status_code, r.text)
    if code == 0:
        print(f"OK — 1 row accepted by {VERIFY_STREAM_KEY}_stream "
              f"(HTTP {r.status_code})")
        if r.text:
            print(f"  response: {r.text[:200]}")
        sys.exit(0)
    _abort(code, msg)


if __name__ == "__main__":
    main()
