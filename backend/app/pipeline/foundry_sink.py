"""Phase E — Local Foundry sink.

A pseudo-Foundry receiver that runs **inside the same FastAPI process** as
the edge node. It is mounted under ``/foundry`` on the main app and accepts
the exact JSON shapes our schemas emit. The shape of what we POST to this
sink is the same shape a real Foundry transform would expect, so a future
swap to the actual platform is just a hostname change.

Endpoints
---------

* ``POST /foundry/object/{type}``       — write one object of ``type``
* ``POST /foundry/objects/{type}/bulk`` — write a batch
* ``GET  /foundry/stats``               — per-type count + last-seen + bytes
* ``GET  /foundry/objects/{type}``      — read back the last ``limit`` objects
* ``DELETE /foundry/objects/{type}``    — clear a type's JSONL (debug)

Object types
------------
``intelligence_event``, ``attribution``, ``tdoa_fix``, ``persistent_emitter``,
``blue_force_unit``, ``sensor_node``, ``emitter_profile``.

Storage
-------
Newline-delimited JSON under ``foundry_data/<type>.jsonl`` (path is
configurable via the ``FOUNDRY_SINK_DIR`` env var). Each row is one object
plus a ``_received_at`` ISO timestamp. The store is append-only, mirroring
how Foundry datasets behave for streaming sources.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Object-type registry
# ----------------------------------------------------------------------------

OBJECT_TYPES = {
    "intelligence_event",
    "attribution",
    "tdoa_fix",
    "persistent_emitter",
    "blue_force_unit",
    "sensor_node",
    "emitter_profile",
}


def _data_dir() -> Path:
    base = os.environ.get("FOUNDRY_SINK_DIR", "foundry_data")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path_for(type_: str) -> Path:
    return _data_dir() / f"{type_}.jsonl"


# ----------------------------------------------------------------------------
# In-memory stats — kept tiny so stats hits stay O(1)
# ----------------------------------------------------------------------------

class _SinkStats:
    """Per-type counters with a lock so concurrent writers don't clobber."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count: dict[str, int] = {t: 0 for t in OBJECT_TYPES}
        self.bytes: dict[str, int] = {t: 0 for t in OBJECT_TYPES}
        self.last_received_ts: dict[str, Optional[float]] = {t: None for t in OBJECT_TYPES}

    def record(self, type_: str, n_objects: int, n_bytes: int) -> None:
        with self._lock:
            self.count[type_] = self.count.get(type_, 0) + n_objects
            self.bytes[type_] = self.bytes.get(type_, 0) + n_bytes
            self.last_received_ts[type_] = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counts": dict(self.count),
                "bytes": dict(self.bytes),
                "last_received_ts": dict(self.last_received_ts),
                "total_objects": sum(self.count.values()),
                "total_bytes": sum(self.bytes.values()),
            }


STATS = _SinkStats()


# ----------------------------------------------------------------------------
# Append-only writer
# ----------------------------------------------------------------------------

_WRITE_LOCK = threading.Lock()


def _write_objects(type_: str, objects: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Append ``objects`` to the JSONL file for ``type_``. Returns (n, bytes)."""
    if type_ not in OBJECT_TYPES:
        raise HTTPException(status_code=404, detail=f"unknown object type: {type_}")
    path = _path_for(type_)
    n = 0
    nbytes = 0
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as f:
            for obj in objects:
                row = dict(obj)
                row.setdefault("_received_at", now_iso)
                line = json.dumps(row, default=str)
                f.write(line + "\n")
                nbytes += len(line) + 1
                n += 1
    STATS.record(type_, n, nbytes)
    return n, nbytes


def _read_objects(type_: str, limit: int = 100) -> list[dict[str, Any]]:
    if type_ not in OBJECT_TYPES:
        raise HTTPException(status_code=404, detail=f"unknown object type: {type_}")
    path = _path_for(type_)
    if not path.exists():
        return []
    # Cheap tail: read all and slice. Fine for demo scale (<10k rows).
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


# ----------------------------------------------------------------------------
# FastAPI router — mounted at /foundry
# ----------------------------------------------------------------------------

router = APIRouter(prefix="/foundry", tags=["foundry"])


class _BulkReq(BaseModel):
    objects: list[dict[str, Any]]


@router.post("/object/{type_}")
def write_one(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
    n, nbytes = _write_objects(type_, [payload])
    return {"ok": True, "n": n, "bytes": nbytes}


@router.post("/objects/{type_}/bulk")
def write_bulk(type_: str, payload: _BulkReq) -> dict[str, Any]:
    n, nbytes = _write_objects(type_, payload.objects)
    return {"ok": True, "n": n, "bytes": nbytes}


@router.get("/stats")
def stats() -> dict[str, Any]:
    return STATS.snapshot()


@router.get("/objects/{type_}")
def read_objects(type_: str, limit: int = 100) -> dict[str, Any]:
    rows = _read_objects(type_, limit=limit)
    return {"type": type_, "n": len(rows), "objects": rows}


@router.delete("/objects/{type_}")
def clear_objects(type_: str) -> dict[str, Any]:
    if type_ not in OBJECT_TYPES:
        raise HTTPException(status_code=404, detail=f"unknown object type: {type_}")
    path = _path_for(type_)
    n = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            for _ in f:
                n += 1
        path.unlink()
    with STATS._lock:  # noqa: SLF001
        STATS.count[type_] = 0
        STATS.bytes[type_] = 0
        STATS.last_received_ts[type_] = None
    return {"ok": True, "deleted": n}
