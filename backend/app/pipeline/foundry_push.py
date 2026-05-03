"""Phase E — Foundry push.

Edge-side helpers that ship our objects to the local Foundry sink. There
are two transport modes:

  * ``in_process`` (default) — call ``foundry_sink._write_objects`` directly.
    Zero overhead, useful for the all-in-one demo where the sink lives in
    the same FastAPI process.
  * ``http``                — POST to ``http://{host}:{port}/foundry/...``
    using ``httpx``. Used when the sink runs on a different node, e.g.
    a real Foundry instance behind an API gateway. The HTTP client retries
    once on transient failure.

Each helper takes a Pydantic object (or list), converts to dict, augments
with FK fields when needed, and pushes. Synchronous wrappers exist so
callers from non-async contexts (e.g. background tasks) can use them.

Sync status
-----------
We track per-type counts and last-success timestamps in
``app.pipeline.foundry_push.PUSH_METRICS`` so the dashboard can show a
"Foundry sync" indicator. Failures bump ``error_count`` but do not raise
to the caller — pushing to Foundry is best-effort, not on the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Iterable, Literal, Optional

import httpx

from app.pipeline import foundry_adapter, foundry_remote, foundry_sink
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

logger = logging.getLogger("spectrumcustody.foundry_push")

TransportMode = Literal["in_process", "http"]


class _PushMetrics:
    """Per-type push counters with a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        types = list(foundry_sink.OBJECT_TYPES)
        self.success_count: dict[str, int] = {t: 0 for t in types}
        self.error_count: dict[str, int] = {t: 0 for t in types}
        self.last_success_ts: dict[str, Optional[float]] = {t: None for t in types}
        self.last_error: dict[str, Optional[str]] = {t: None for t in types}

    def record_ok(self, type_: str, n: int = 1) -> None:
        with self._lock:
            self.success_count[type_] = self.success_count.get(type_, 0) + n
            self.last_success_ts[type_] = time.time()

    def record_err(self, type_: str, msg: str) -> None:
        with self._lock:
            self.error_count[type_] = self.error_count.get(type_, 0) + 1
            self.last_error[type_] = msg[:200]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "success_count": dict(self.success_count),
                "error_count": dict(self.error_count),
                "last_success_ts": dict(self.last_success_ts),
                "last_error": dict(self.last_error),
            }


PUSH_METRICS = _PushMetrics()


# ----------------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------------

def _transport_mode() -> TransportMode:
    return os.environ.get("FOUNDRY_PUSH_MODE", "in_process")  # type: ignore[return-value]


def _http_url(type_: str) -> str:
    base = os.environ.get("FOUNDRY_PUSH_URL", "http://127.0.0.1:8000")
    return f"{base}/foundry/object/{type_}"


def _http_url_bulk(type_: str) -> str:
    base = os.environ.get("FOUNDRY_PUSH_URL", "http://127.0.0.1:8000")
    return f"{base}/foundry/objects/{type_}/bulk"


async def _http_post(url: str, json_body: Any) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
        r = await client.post(url, json=json_body)
        r.raise_for_status()


# ----------------------------------------------------------------------------
# Core async push
# ----------------------------------------------------------------------------

async def push_object(type_: str, obj: dict[str, Any]) -> bool:
    """Push one object. Returns True iff the sink accepted it.

    Never raises — failures are recorded in ``PUSH_METRICS`` and logged
    at warning level.
    """
    if type_ not in foundry_sink.OBJECT_TYPES:
        PUSH_METRICS.record_err(type_, "unknown_type")
        return False
    mode = _transport_mode()
    try:
        if mode == "http":
            await _http_post(_http_url(type_), obj)
        else:
            foundry_sink._write_objects(type_, [obj])  # noqa: SLF001
        PUSH_METRICS.record_ok(type_, 1)
        return True
    except Exception as e:
        logger.warning("foundry push failed (%s): %s", type_, e)
        PUSH_METRICS.record_err(type_, str(e))
        return False


async def push_bulk(type_: str, objects: list[dict[str, Any]]) -> int:
    """Push a batch. Returns the number of accepted objects (0 on failure)."""
    if not objects:
        return 0
    if type_ not in foundry_sink.OBJECT_TYPES:
        PUSH_METRICS.record_err(type_, "unknown_type")
        return 0
    mode = _transport_mode()
    try:
        if mode == "http":
            await _http_post(_http_url_bulk(type_), {"objects": objects})
        else:
            foundry_sink._write_objects(type_, objects)  # noqa: SLF001
        PUSH_METRICS.record_ok(type_, len(objects))
        return len(objects)
    except Exception as e:
        logger.warning("foundry bulk push failed (%s): %s", type_, e)
        PUSH_METRICS.record_err(type_, str(e))
        return 0


# ----------------------------------------------------------------------------
# Convenience helpers — accept Pydantic objects, augment with FKs.
#
# Each helper writes to TWO places:
#   1. The local Foundry-shaped sink (always, drives the dashboard).
#   2. The real Palantir Foundry tenant via ``foundry_remote`` (only when
#      ``FOUNDRY_STACK_URL`` + ``FOUNDRY_TOKEN`` are configured). The remote
#      transport buffers to disk on failure and replays in the background,
#      so calls here never block and never raise.
#
# The remote-row mapping happens in ``foundry_adapter`` — this module just
# fans the calls out.
# ----------------------------------------------------------------------------

async def _remote_rows(stream_key: str, rows: list[dict[str, Any]]) -> None:
    """Helper: ship rows to the configured Foundry tenant if enabled."""
    transport = foundry_remote.get_transport()
    if transport is None or not transport.is_enabled():
        return
    try:
        await transport.push_rows(stream_key, rows)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("remote push (%s) raised: %s", stream_key, e)


async def push_intelligence_event(
    event: IntelligenceEvent,
    *,
    reading: Optional[RFSignalReading] = None,
) -> bool:
    """Push an intelligence event to local sink + remote Foundry.

    ``reading`` is optional but recommended: the Foundry stream schema
    includes RF features (frequency / bandwidth / power / duration) that
    live on the reading, so passing it through enriches the row.
    """
    ok = await push_object("intelligence_event", event.model_dump())
    await _remote_rows(
        "intelligence_event",
        [foundry_adapter.intelligence_event_row(event, reading)],
    )
    return ok


async def push_attribution(
    attribution: AttributionResult, *, event_id: str, track_id: Optional[str]
) -> bool:
    body = attribution.model_dump()
    body["id"] = event_id           # stable PK for the local sink
    body["event_id"] = event_id
    body["track_id"] = track_id
    body["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ok = await push_object("attribution", body)
    await _remote_rows(
        "attribution",
        [foundry_adapter.attribution_row(attribution, event_id=event_id, track_id=track_id)],
    )
    return ok


async def push_tdoa_fix(
    fix: TdoaSolution,
    *,
    event_id: str,
    track_id: Optional[str],
    truth_lat: float,
    truth_lon: float,
) -> bool:
    body = fix.model_dump()
    body["id"] = f"fix_{event_id}"
    body["event_id"] = event_id
    body["track_id"] = track_id
    body["truth_lat"] = truth_lat
    body["truth_lon"] = truth_lon
    ok = await push_object("tdoa_fix", body)
    await _remote_rows("tdoa_fix", [foundry_adapter.tdoa_fix_row(fix, event_id=event_id)])
    return ok


async def push_persistent_emitter(pe: PersistentEmitter) -> bool:
    ok = await push_object("persistent_emitter", pe.model_dump())
    await _remote_rows("persistent_emitter", [foundry_adapter.persistent_emitter_row(pe)])
    return ok


async def push_blue_force_units(units: Iterable[BlueForceUnit]) -> int:
    units_list = list(units)
    n = await push_bulk("blue_force_unit", [u.model_dump() for u in units_list])
    await _remote_rows(
        "blue_force_unit",
        [foundry_adapter.blue_force_row(u) for u in units_list],
    )
    return n


async def push_sensor_nodes(sensors: Iterable[SensorNode]) -> int:
    sensors_list = list(sensors)
    n = await push_bulk("sensor_node", [s.model_dump() for s in sensors_list])
    await _remote_rows(
        "sensor_node",
        [foundry_adapter.sensor_node_row(s) for s in sensors_list],
    )
    return n


async def push_emitter_profiles(profiles: Iterable[EmitterProfile]) -> int:
    profiles_list = list(profiles)
    n = await push_bulk("emitter_profile", [p.model_dump() for p in profiles_list])
    await _remote_rows(
        "emitter_profile",
        [foundry_adapter.emitter_profile_row(p) for p in profiles_list],
    )
    return n


# ----------------------------------------------------------------------------
# Convenience: enqueue a push as a fire-and-forget asyncio task
# ----------------------------------------------------------------------------

def schedule(coro) -> None:
    """Spawn an awaitable as a background task that swallows errors.

    Use this from inside ``process_tick`` so the push never blocks the
    pipeline. Errors are still recorded in ``PUSH_METRICS``.
    """
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        # No running loop — fall back to running synchronously.
        asyncio.run(coro)
