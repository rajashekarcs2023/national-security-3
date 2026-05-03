"""Phase F — Real Palantir Foundry push transport.

This is the *remote* sibling of ``foundry_sink`` / ``foundry_push``. Where
those two ship objects to a local in-process sink (great for the demo even
without Foundry creds), this module ships the same objects to a real
Palantir Foundry tenant via the **Foundry Streams** ingestion endpoint:

    POST  ${FOUNDRY_STACK_URL}/stream/api/streams/${STREAM_RID}/branches/master/jsonRecords
    Authorization:  Bearer ${FOUNDRY_TOKEN}
    Content-Type:   application/json
    Body:           [{"value": <row>}, ...]

Each of our 7 object types maps to its own stream via ``FOUNDRY_STREAM_RIDS``
(a JSON object env var keyed on object type, valued on stream RID).

Design properties
-----------------

* **DDIL-resilient.** Every push that fails (network down, 5xx, timeout)
  is appended to a per-stream JSONL buffer on disk. When the link comes
  back the background flush loop replays them in FIFO order. The buffer
  survives process restarts.
* **Non-blocking.** All I/O is async ``httpx``; pushes are scheduled as
  fire-and-forget tasks from the pipeline. The hot path never waits.
* **Local sink unaffected.** This module is *additive*. The local sink
  keeps writing exactly as before — remote push is a shadow that runs
  alongside it. If creds are not configured the demo is identical to
  before.
* **Observable.** Per-stream stats (pushed / queued / failed / bytes,
  last_success_ts, last_error, online flag) feed a status endpoint so
  the dashboard can show "Foundry: LIVE → tenant.palantirfoundry.com"
  vs "Foundry: local mirror only".

Configuration
-------------

* ``FOUNDRY_STACK_URL``   — e.g. ``https://nshackathon.palantirfoundry.com``
                            (defaults to the hackathon stack if unset).
* ``FOUNDRY_API``         — Bearer token for streaming push. (``FOUNDRY_TOKEN``
                            is also accepted as an alias for compatibility.)
* ``FOUNDRY_STREAM_RIDS`` — JSON map, keys are object types, values are
                            stream RIDs. Example::

    {
      "intelligence_event":  "ri.foundry.main.dataset.aaaa-...",
      "attribution":         "ri.foundry.main.dataset.bbbb-...",
      "tdoa_fix":            "ri.foundry.main.dataset.cccc-...",
      "persistent_emitter":  "ri.foundry.main.dataset.dddd-...",
      "blue_force_unit":     "ri.foundry.main.dataset.eeee-...",
      "sensor_node":         "ri.foundry.main.dataset.ffff-...",
      "emitter_profile":     "ri.foundry.main.dataset.gggg-..."
    }

* ``FOUNDRY_DDIL_BUFFER_DIR`` — directory for offline replay buffers
                                 (default: ``foundry_ddil/``)
* ``FOUNDRY_REMOTE_TIMEOUT_S`` — HTTP timeout per push (default: 5.0)
* ``FOUNDRY_REMOTE_FLUSH_S``   — background flush interval (default: 2.0)

If ``FOUNDRY_API`` (or ``FOUNDRY_TOKEN``) is missing, the transport stays
disabled and ``is_enabled()`` returns ``False``. All push attempts become
no-ops; the pipeline still runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("spectrumcustody.foundry_remote")

# Hackathon-stack default. Overridable via ``FOUNDRY_STACK_URL``. Without
# a token the transport stays disabled, so leaking the URL costs nothing —
# but it means a freshly-pasted token in .env is enough to go live without
# also remembering to set the URL.
DEFAULT_STACK_URL = "https://nshackathon.palantirfoundry.com"


# ---------------------------------------------------------------------------
# Stream-key vocabulary
# ---------------------------------------------------------------------------

# We keep this in sync with ``foundry_sink.OBJECT_TYPES``. The strings here
# are the keys used in ``FOUNDRY_STREAM_RIDS`` and in the per-stream stats.
STREAM_KEYS: tuple[str, ...] = (
    "intelligence_event",
    "attribution",
    "tdoa_fix",
    "persistent_emitter",
    "blue_force_unit",
    "sensor_node",
    "emitter_profile",
)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class StreamStats:
    """Per-stream telemetry exposed to the status endpoint + dashboard."""

    pushed: int = 0          # rows successfully accepted by Foundry
    queued: int = 0          # rows currently waiting in the DDIL buffer
    failed: int = 0          # rows that hit a hard error (4xx other than 408/429)
    bytes_sent: int = 0      # cumulative wire bytes (approx, includes envelope)
    last_success_ts: Optional[float] = None
    last_attempt_ts: Optional[float] = None
    last_error: Optional[str] = None


@dataclass
class RemoteSnapshot:
    """Serialisable view of the transport state for the status endpoint."""

    enabled: bool
    online: bool
    stack_url: Optional[str]
    configured_streams: list[str]
    missing_streams: list[str]
    streams: dict[str, dict[str, Any]]
    ddil_buffer_dir: str
    last_flush_ts: Optional[float]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class FoundryRemoteTransport:
    """Authenticated streaming push to a real Palantir Foundry tenant.

    This object is created at lifespan startup and stashed on
    ``app.state.foundry_remote``. It owns:

    * an ``httpx.AsyncClient`` for connection pooling
    * one DDIL buffer file per stream key
    * per-stream stats
    * an async background loop that flushes new rows + replays buffered rows
    """

    def __init__(
        self,
        stack_url: Optional[str] = None,
        token: Optional[str] = None,
        stream_rids: Optional[dict[str, str]] = None,
        ddil_buffer_dir: Optional[str] = None,
        timeout_s: Optional[float] = None,
        flush_interval_s: Optional[float] = None,
    ) -> None:
        # -- Config (env wins if explicit args not given) ----------------
        # Stack URL falls back to the hackathon tenant when nothing is set
        # so a token alone is enough to go live.
        self.stack_url = (
            stack_url
            or os.environ.get("FOUNDRY_STACK_URL", "")
            or DEFAULT_STACK_URL
        ).rstrip("/")
        # ``FOUNDRY_API`` is the canonical name (matches the Foundry FDE
        # convention); ``FOUNDRY_TOKEN`` is accepted as an alias so older
        # docs and shell exports keep working.
        self.token = (
            token
            or os.environ.get("FOUNDRY_API", "")
            or os.environ.get("FOUNDRY_TOKEN", "")
        )
        self.stream_rids = stream_rids if stream_rids is not None else _load_rids_from_env()
        self.ddil_buffer_dir = Path(
            ddil_buffer_dir or os.environ.get("FOUNDRY_DDIL_BUFFER_DIR", "foundry_ddil")
        )
        self.timeout_s = float(
            timeout_s if timeout_s is not None else os.environ.get("FOUNDRY_REMOTE_TIMEOUT_S", "5.0")
        )
        self.flush_interval_s = float(
            flush_interval_s
            if flush_interval_s is not None
            else os.environ.get("FOUNDRY_REMOTE_FLUSH_S", "2.0")
        )

        # -- Per-stream state -------------------------------------------
        self._stats: dict[str, StreamStats] = {k: StreamStats() for k in STREAM_KEYS}
        # Each stream gets its own asyncio.Lock so a flush and a push don't
        # interleave on the same buffer file.
        self._locks: dict[str, asyncio.Lock] = {}
        # File-path cache.
        self.ddil_buffer_dir.mkdir(parents=True, exist_ok=True)

        # -- HTTP client -------------------------------------------------
        # Use a shared client so we get connection pooling + HTTP/2.
        self._client: Optional[httpx.AsyncClient] = None
        self._online: bool = False
        self._last_flush_ts: Optional[float] = None
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._stopped = False
        # Threading lock for stat reads from sync contexts (status endpoint).
        self._snapshot_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the HTTP client and kick off the background flush loop."""
        if not self.is_enabled():
            logger.info(
                "Foundry remote DISABLED: missing FOUNDRY_API (or FOUNDRY_TOKEN). "
                "Local sink continues to operate; remote push is a no-op."
            )
            return
        # Lazy-init locks for each known stream key.
        for k in STREAM_KEYS:
            self._locks.setdefault(k, asyncio.Lock())

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        self._flush_task = asyncio.create_task(self._flush_loop())
        # Seed the queued counts from anything left over on disk.
        for key in STREAM_KEYS:
            self._stats[key].queued = _count_lines(self._buffer_path(key))
        configured = [k for k, v in self.stream_rids.items() if v]
        missing = [k for k in STREAM_KEYS if k not in configured]
        logger.info(
            "Foundry remote ENABLED. stack=%s, streams_configured=%d, missing=%s",
            self.stack_url, len(configured), missing,
        )

    async def stop(self) -> None:
        """Cancel the flush loop and close the HTTP client."""
        self._stopped = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        """True iff the transport has the minimum config to attempt a push.

        Per-stream RID may still be missing; rows for unconfigured streams
        are silently buffered to disk so they replay once a RID is added.
        """
        return bool(self.stack_url) and bool(self.token)

    def is_configured(self, stream_key: str) -> bool:
        """True iff ``stream_key`` has a non-empty RID configured."""
        return bool(self.stream_rids.get(stream_key))

    async def push_rows(self, stream_key: str, rows: list[dict[str, Any]]) -> bool:
        """Push a batch of rows to a stream. Returns True on success.

        Failed pushes are appended to the DDIL buffer for replay. If the
        transport is disabled or the RID is missing, the rows are still
        buffered (they'll flush the moment config is fixed and the loop
        wakes up).

        Never raises — best-effort by design.
        """
        if stream_key not in STREAM_KEYS:
            logger.warning("foundry_remote.push_rows: unknown stream_key=%s", stream_key)
            return False
        if not rows:
            return True

        # If the transport is fully disabled (no stack/token at all), this
        # is a clean no-op — we don't buffer, because there's no signal
        # the user ever intends to enable Foundry on this run. The
        # pipeline-level gate in ``foundry_push._remote_rows`` already
        # short-circuits so we shouldn't even get here, but be defensive.
        if not self.is_enabled():
            return False

        lock = self._locks.setdefault(stream_key, asyncio.Lock())
        async with lock:
            now = time.time()
            self._stats[stream_key].last_attempt_ts = now

            if not self.is_configured(stream_key):
                # Stack + token are set, but this specific stream's RID
                # isn't yet — buffer to disk so the rows replay the moment
                # the user pastes the RID into ``.env`` and restarts /
                # hits the replay endpoint.
                self._append_buffer(stream_key, rows)
                self._stats[stream_key].queued += len(rows)
                return False

            ok, sent_bytes, err = await self._post_rows(stream_key, rows)
            if ok:
                self._stats[stream_key].pushed += len(rows)
                self._stats[stream_key].bytes_sent += sent_bytes
                self._stats[stream_key].last_success_ts = time.time()
                self._stats[stream_key].last_error = None
                self._online = True
                return True

            # Non-OK: buffer for retry.
            self._online = False
            self._append_buffer(stream_key, rows)
            self._stats[stream_key].queued += len(rows)
            self._stats[stream_key].last_error = err[:200] if err else "unknown"
            logger.warning("foundry_remote push failed (%s): %s", stream_key, err)
            return False

    def snapshot(self) -> RemoteSnapshot:
        """Synchronous snapshot for the status endpoint."""
        with self._snapshot_lock:
            configured = [k for k in STREAM_KEYS if self.is_configured(k)]
            missing = [k for k in STREAM_KEYS if not self.is_configured(k)]
            streams: dict[str, dict[str, Any]] = {}
            for k in STREAM_KEYS:
                s = self._stats[k]
                streams[k] = {
                    "configured": self.is_configured(k),
                    "pushed": s.pushed,
                    "queued": s.queued,
                    "failed": s.failed,
                    "bytes_sent": s.bytes_sent,
                    "last_success_ts": s.last_success_ts,
                    "last_attempt_ts": s.last_attempt_ts,
                    "last_error": s.last_error,
                }
            return RemoteSnapshot(
                enabled=self.is_enabled(),
                online=self._online,
                stack_url=self.stack_url or None,
                configured_streams=configured,
                missing_streams=missing,
                streams=streams,
                ddil_buffer_dir=str(self.ddil_buffer_dir),
                last_flush_ts=self._last_flush_ts,
            )

    # ------------------------------------------------------------------
    # Internal — HTTP
    # ------------------------------------------------------------------

    def _stream_url(self, stream_key: str) -> Optional[str]:
        """Build the Foundry Streams v2 ``publishRecord`` URL.

        Per the Palantir docs, the working endpoint on the hackathon
        tenant is::

            POST /api/v2/highScale/streams/datasets/{datasetRid}
                 /streams/{streamBranchName}/publishRecord

        which accepts a body of ``{"record": {...}}`` (one row at a
        time). We hard-code branch ``master`` since that's what the
        "Connect via API" flow creates by default; if a stream is later
        moved to a different branch, surface the branch via the env
        config alongside the RID and feed it in here.
        """
        rid = self.stream_rids.get(stream_key)
        if not rid:
            return None
        return (
            f"{self.stack_url}/api/v2/highScale/streams/datasets/{rid}"
            f"/streams/master/publishRecord"
        )

    async def _post_rows(
        self, stream_key: str, rows: list[dict[str, Any]]
    ) -> tuple[bool, int, Optional[str]]:
        """Push every row in ``rows`` to the Foundry stream.

        Returns ``(all_ok, bytes_sent, err_msg)``.

        Wire format: ``publishRecord`` is **one row per HTTP call** with
        body ``{"record": <flat-row>}``. We send the rows in parallel
        via ``asyncio.gather`` to keep p99 latency bounded by a single
        round-trip even for the bulk reference-data push at startup.

        ``record`` is a flat dict matching the stream's exact schema
        (the adapter coerces types so ints stay ints, strings stay
        strings). The optional ``viewRid`` body field is omitted; per
        the docs it defaults to "the latest stream on the branch",
        which is what we want.
        """
        if self._client is None:
            return False, 0, "client_not_started"
        url = self._stream_url(stream_key)
        if url is None:
            return False, 0, "missing_rid"
        if not rows:
            return True, 0, None

        async def _push_one(row: dict[str, Any]) -> tuple[bool, int, Optional[str]]:
            body_obj = {"record": row}
            body = json.dumps(body_obj, default=str).encode("utf-8")
            try:
                r = await self._client.post(url, content=body)  # type: ignore[union-attr]
                if r.status_code // 100 == 2:
                    return True, len(body), None
                err = f"HTTP {r.status_code}: {r.text[:200]}"
                return False, 0, err
            except httpx.RequestError as e:
                return False, 0, f"{type(e).__name__}: {e}"
            except Exception as e:  # pragma: no cover
                return False, 0, f"unexpected: {e}"

        results = await asyncio.gather(*(_push_one(row) for row in rows))
        n_ok = sum(1 for ok, _, _ in results if ok)
        bytes_sent = sum(b for ok, b, _ in results if ok)
        first_err: Optional[str] = next((e for ok, _, e in results if not ok), None)

        # Treat the batch as failed if *any* row failed — caller can
        # then re-buffer the whole batch. This is conservative; we lose
        # the rows that DID succeed by retrying them, but Foundry stream
        # rows aren't deduplicated by us anyway and the duplication
        # cost (a few KB) is negligible.
        if n_ok == len(rows):
            return True, bytes_sent, None

        # Hard 4xx (excluding transient 408/429) → bump failed counter.
        # We treat everything else as transient and let the DDIL replay
        # loop pick it up.
        transient_markers = ("HTTP 408", "HTTP 429", "HTTP 5")
        if first_err and not any(m in first_err for m in transient_markers):
            self._stats[stream_key].failed += (len(rows) - n_ok)
        return False, bytes_sent, first_err

    # ------------------------------------------------------------------
    # Internal — DDIL buffer
    # ------------------------------------------------------------------

    def _buffer_path(self, stream_key: str) -> Path:
        return self.ddil_buffer_dir / f"{stream_key}.jsonl"

    def _append_buffer(self, stream_key: str, rows: list[dict[str, Any]]) -> None:
        """Append rows to the JSONL buffer for ``stream_key``. Atomic append."""
        path = self._buffer_path(stream_key)
        try:
            with path.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, default=str))
                    f.write("\n")
        except Exception:
            logger.exception("DDIL buffer write failed for %s", stream_key)

    def _drain_buffer_atomic(self, stream_key: str) -> list[dict[str, Any]]:
        """Read and clear the buffer for one stream, returning the rows."""
        path = self._buffer_path(stream_key)
        if not path.exists():
            return []
        # Rename then read — protects against a concurrent append losing rows.
        tmp = path.with_suffix(".jsonl.draining")
        try:
            path.rename(tmp)
        except FileNotFoundError:
            return []
        rows: list[dict[str, Any]] = []
        try:
            with tmp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("malformed DDIL row dropped from %s", stream_key)
            tmp.unlink(missing_ok=True)
        except Exception:
            # Restore the file if drain failed mid-read so we don't lose data.
            try:
                tmp.rename(path)
            except Exception:
                pass
            raise
        return rows

    # ------------------------------------------------------------------
    # Internal — background flush loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodically replay any buffered rows once the link is healthy.

        We always *attempt* a tiny ping-shaped push: even one buffered row
        per stream tells us whether Foundry is reachable. If a push
        succeeds, we drain the rest of that stream's buffer in chunks.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(self.flush_interval_s)
                if not self.is_enabled():
                    continue
                self._last_flush_ts = time.time()
                for key in STREAM_KEYS:
                    if not self.is_configured(key):
                        continue
                    if self._stats[key].queued <= 0:
                        # Nothing on disk for this stream — skip.
                        if not self._buffer_path(key).exists():
                            continue
                    await self._replay_one_stream(key)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("foundry_remote flush loop iteration failed")

    async def _replay_one_stream(self, stream_key: str) -> None:
        """Replay one stream's DDIL buffer in batches of 100 rows."""
        lock = self._locks.setdefault(stream_key, asyncio.Lock())
        async with lock:
            rows = self._drain_buffer_atomic(stream_key)
            if not rows:
                self._stats[stream_key].queued = 0
                return
            # Replay in chunks so a single failure leaves the rest alone.
            chunk_size = 100
            remaining: list[dict[str, Any]] = []
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                ok, sent_bytes, err = await self._post_rows(stream_key, chunk)
                if ok:
                    self._stats[stream_key].pushed += len(chunk)
                    self._stats[stream_key].bytes_sent += sent_bytes
                    self._stats[stream_key].last_success_ts = time.time()
                    self._stats[stream_key].last_error = None
                    self._online = True
                else:
                    # Stop flushing this stream and put back the rest.
                    self._online = False
                    self._stats[stream_key].last_error = (err or "unknown")[:200]
                    remaining.extend(rows[i:])
                    break
            if remaining:
                self._append_buffer(stream_key, remaining)
            self._stats[stream_key].queued = len(remaining)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rids_from_env() -> dict[str, str]:
    """Parse ``FOUNDRY_STREAM_RIDS`` (a JSON object) from the environment.

    Returns an empty dict if the env var is missing or invalid. Per-stream
    RIDs may also be set individually via ``FOUNDRY_STREAM_RID_<KEY>``,
    e.g. ``FOUNDRY_STREAM_RID_INTELLIGENCE_EVENT=ri.foundry...`` — the
    individual vars override the JSON map.
    """
    raw = os.environ.get("FOUNDRY_STREAM_RIDS", "")
    rids: dict[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, str) and v:
                        rids[str(k)] = v
        except json.JSONDecodeError:
            logger.warning("FOUNDRY_STREAM_RIDS is not valid JSON; ignoring")
    # Per-stream override.
    for key in STREAM_KEYS:
        env_name = f"FOUNDRY_STREAM_RID_{key.upper()}"
        v = os.environ.get(env_name, "").strip()
        if v:
            rids[key] = v
    return rids


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except Exception:
        return 0
    return n


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

# We keep one transport per process. ``main.lifespan`` initialises it.
_transport: Optional[FoundryRemoteTransport] = None


def get_transport() -> Optional[FoundryRemoteTransport]:
    return _transport


def set_transport(t: Optional[FoundryRemoteTransport]) -> None:
    global _transport
    _transport = t
