"""Unit tests for ``app.pipeline.foundry_sink``.

Round-trip: write one and bulk → read back → stats update correctly.
Uses an isolated temporary directory so we don't pollute the repo's
``foundry_data/`` folder during tests. Mounts the router on a throw-away
FastAPI app and uses ``starlette.testclient`` for requests.
"""

from __future__ import annotations

import importlib
import os
import pathlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


@pytest.fixture()
def isolated_sink(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Point FOUNDRY_SINK_DIR at a fresh tmp dir and reload the module.

    The sink reads ``FOUNDRY_SINK_DIR`` lazily inside ``_data_dir()``, so we
    only need to set the env var. We also reset the in-memory ``STATS``
    counters by reloading the module so counts from other tests never leak.
    """
    monkeypatch.setenv("FOUNDRY_SINK_DIR", str(tmp_path))
    # Reload so STATS is fresh and the router uses the new dir.
    import app.pipeline.foundry_sink as sink_mod

    importlib.reload(sink_mod)

    app = FastAPI()
    app.include_router(sink_mod.router)
    client = TestClient(app)

    yield sink_mod, client, tmp_path


def test_write_then_read_single_object(isolated_sink) -> None:
    sink_mod, client, tmp_path = isolated_sink
    obj = {"id": "evt_123", "title": "demo event", "priority": "high"}

    r = client.post("/foundry/object/intelligence_event", json=obj)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["n"] == 1
    assert body["bytes"] > 0

    # On disk
    path = tmp_path / "intelligence_event.jsonl"
    assert path.exists()
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1

    # Read back via API
    r = client.get("/foundry/objects/intelligence_event?limit=10")
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "intelligence_event"
    assert data["n"] == 1
    assert data["objects"][0]["id"] == "evt_123"
    # Sink stamps a receive time on every row.
    assert "_received_at" in data["objects"][0]


def test_bulk_write_and_stats(isolated_sink) -> None:
    sink_mod, client, _ = isolated_sink
    batch = {
        "objects": [
            {"id": f"tdoa_{i}", "lat": 34.0 + i * 0.001, "lon": -118.2, "cep_m": 10.0 + i}
            for i in range(5)
        ]
    }
    r = client.post("/foundry/objects/tdoa_fix/bulk", json=batch)
    assert r.status_code == 200
    assert r.json()["n"] == 5

    # Stats should reflect the bulk write.
    r = client.get("/foundry/stats")
    stats = r.json()
    assert stats["counts"]["tdoa_fix"] == 5
    assert stats["bytes"]["tdoa_fix"] > 0
    assert stats["last_received_ts"]["tdoa_fix"] is not None
    assert stats["total_objects"] >= 5


def test_unknown_object_type_returns_404(isolated_sink) -> None:
    _, client, _ = isolated_sink
    r = client.post("/foundry/object/not_a_real_type", json={"foo": "bar"})
    assert r.status_code == 404


def test_limit_parameter_tails_the_file(isolated_sink) -> None:
    _, client, _ = isolated_sink
    for i in range(10):
        client.post("/foundry/object/attribution", json={"id": f"att_{i}"})

    r = client.get("/foundry/objects/attribution?limit=3")
    data = r.json()
    assert data["n"] == 3
    # Tail returns the latest rows.
    ids = [o["id"] for o in data["objects"]]
    assert ids == ["att_7", "att_8", "att_9"]


def test_delete_clears_file_and_stats(isolated_sink) -> None:
    sink_mod, client, tmp_path = isolated_sink
    # Write three.
    for i in range(3):
        client.post("/foundry/object/persistent_emitter", json={"id": f"pe_{i}"})

    r = client.delete("/foundry/objects/persistent_emitter")
    assert r.status_code == 200
    assert r.json()["deleted"] == 3

    # File should be gone.
    assert not (tmp_path / "persistent_emitter.jsonl").exists()

    # Stats reset for this type.
    r = client.get("/foundry/stats")
    assert r.json()["counts"]["persistent_emitter"] == 0


def test_malformed_jsonl_rows_are_skipped_on_read(isolated_sink, tmp_path: pathlib.Path) -> None:
    sink_mod, client, tmp_path = isolated_sink
    # Drop one valid + one malformed row manually.
    path = tmp_path / "sensor_node.jsonl"
    path.write_text('{"id":"ok_1"}\nnot json\n{"id":"ok_2"}\n')

    r = client.get("/foundry/objects/sensor_node")
    data = r.json()
    assert [o["id"] for o in data["objects"]] == ["ok_1", "ok_2"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
