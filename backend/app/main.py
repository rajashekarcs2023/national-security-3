"""SpectrumCustody edge backend.

FastAPI app that runs the entire edge pipeline:

  RF emulator  ->  CNN classifier  ->  baseline + custody  ->  action engine
                                                                     |
                                                              network online?
                                                              |              |
                                                       sync to UI     queue locally
                                                                     |
                                                              when net returns
                                                                     |
                                                              drain in priority

Streams everything to the dashboard via WebSocket. Exposes REST endpoints for
operator commands (toggle network, change sensitivity, run scenarios, get
Foundry-shaped exports, etc.).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Auto-load .env BEFORE any os.environ reads downstream.
# python-dotenv walks up from the cwd to find .env, so this works whether
# uvicorn is launched from repo root or from backend/. We do this at the
# very top so all imports below see FOUNDRY_API / FOUNDRY_STACK_URL etc.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:
    # python-dotenv is in requirements.txt but missing it must not break
    # the demo — env vars can still be exported manually.
    pass

import asyncio
import contextlib
import io
import json
import logging
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Optional

import numpy as np
import torch
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app.ml.baseline import LocalBaselineTracker
from app.ml.classifier import EdgeClassifier
from app.ml.radioml import RadioMLPool
from app.pipeline import action_engine, foundry_export
from app.pipeline.attribution import attribute as attribution_attribute
from app.pipeline.blue_force import BlueForceFeed
from app.pipeline.coa import (
    ROE_DESCRIPTIONS,
    ROE_POSTURES,
    recommend as coa_recommend,
    recommendation_to_dict,
)
from app.pipeline.cot import build_cot_dict, build_cot_xml
from app.pipeline.custody import CustodyManager
from app.pipeline.emulator import DEFAULT_CLASS_WEIGHTS, RFEmulator
from app.pipeline.eo_sensor import EOSensor
from app.pipeline.llm import EdgeLLM, template_after_action, template_brief
from app.pipeline.foundry_sink import router as foundry_router
from app.pipeline import foundry_push, foundry_remote
from app.pipeline.foundry_push import (
    PUSH_METRICS as FOUNDRY_PUSH_METRICS,
    push_attribution as foundry_push_attribution,
    push_blue_force_units as foundry_push_blue_force,
    push_emitter_profiles as foundry_push_emitter_profiles,
    push_intelligence_event as foundry_push_intelligence_event,
    push_persistent_emitter as foundry_push_persistent_emitter,
    push_sensor_nodes as foundry_push_sensor_nodes,
    push_tdoa_fix as foundry_push_tdoa_fix,
)
from app.pipeline.foundry_remote import FoundryRemoteTransport
from app.pipeline.persistence import PersistenceTracker
from app.pipeline.queue import OfflineQueue
from app.pipeline.tdoa import simulate_and_solve as tdoa_simulate_and_solve
from app.pipeline.scenario import (
    AnnounceStep,
    EmitStep,
    EOFailStep,
    NetworkStep,
    SCENARIOS,
    Scenario,
    SensitivityStep,
    WaitStep,
)
from app.schemas import (
    CoaExecuteReq,
    CoaPostureReq,
    CoaRecommendReq,
    CotPublication,
    CotPublishReq,
    EdgeCommand,
    IntelligenceEvent,
    NLQueryReq,
    NetworkToggleReq,
    OperatorAction,
    OperatorActionReq,
    RealDataMixReq,
    RFSignalReading,
    RoePosture,
    ScenarioRunReq,
    SensitivityReq,
    WSMessage,
    WatchBandReq,
    new_id,
    utc_now,
)
from app.state import STATE


logger = logging.getLogger("spectrumcustody")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")


# Approximate "raw bytes" this reading would have cost to ship to the cloud.
RAW_READING_BYTES = 64 * 64 * 4 + 256


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

async def _send(client: WebSocket, msg: dict) -> bool:
    try:
        await client.send_text(json.dumps(msg, default=str))
        return True
    except Exception:
        return False


async def broadcast(msg_type: str, payload: dict[str, Any]) -> None:
    msg = {"type": msg_type, "payload": payload, "ts": utc_now().isoformat()}
    async with STATE.ws_lock:
        clients = list(STATE.ws_clients)
    dead: list[WebSocket] = []
    for c in clients:
        ok = await _send(c, msg)
        if not ok:
            dead.append(c)
    if dead:
        async with STATE.ws_lock:
            for c in dead:
                STATE.ws_clients.discard(c)


# ---------------------------------------------------------------------------
# LLM enrichment (async, doesn't block the main pipeline)
# ---------------------------------------------------------------------------

async def enrich_event_with_llm(event: IntelligenceEvent) -> None:
    llm: EdgeLLM = STATE.llm  # type: ignore[assignment]
    if not llm:
        return
    brief: Optional[str] = None
    if llm.available:
        brief = await llm.brief_for_event(event.model_dump())
    if not brief:
        brief = template_brief(event.model_dump())
    event.llm_brief = brief
    await broadcast(
        "intelligence_event_update",
        {"event_id": event.id, "llm_brief": brief},
    )


# ---------------------------------------------------------------------------
# Phase A — cross-sensor cueing helpers (EO tipping)
# ---------------------------------------------------------------------------

# Minimum seconds between successive EO observations for the same track. The
# gimbal has moving parts and the operator attention budget is finite — we do
# not slew on every single RF tick for a track that already has a fresh
# visual within this window.
EO_TIP_MIN_INTERVAL_SECONDS = 2.0


def _should_tip_eo(track) -> bool:  # type: ignore[no-untyped-def]
    """Return True iff the EO gimbal should slew for this track right now.

    Rules:
      1. Only MEDIUM/HIGH threat tracks are worth an EO slew. LOW threat
         tracks (FALSE_ALARM family) never open tracks anyway; keeping the
         check makes the helper future-proof for added threat levels.
      2. If the track already has an EO observation within the last
         ``EO_TIP_MIN_INTERVAL_SECONDS``, skip — don't thrash the gimbal.
    """
    if track.threat_level not in ("MEDIUM", "HIGH"):
        return False
    if track.last_eo_obs is not None:
        age = (utc_now() - track.last_eo_obs.timestamp).total_seconds()
        if age < EO_TIP_MIN_INTERVAL_SECONDS:
            return False
    return True


async def tip_eo_for_track(
    track,
    *,
    triggering_event_id: Optional[str],
    custody: CustodyManager,
) -> None:
    """Slew the simulated EO gimbal to the track's sector and fold the
    observation back into custody. Fire-and-forget from ``process_tick``.

    Broadcasts:
      - ``eo_observation`` (always, for the CrossSensorPanel UI)
      - ``custody_log`` and ``track_update`` (only if the observation caused
        a custody state transition, e.g. DETECTED→TRACKING on first visual).
    """
    eo: EOSensor = app.state.eo_sensor  # type: ignore[attr-defined]
    try:
        obs = await eo.observe(track, triggering_event_id=triggering_event_id)
    except Exception:
        logger.exception("EO observe failed for track %s", track.track_id)
        return

    # Fold into custody. This mutates the track in place if the observation
    # confirms, reacquires, or contradicts. It emits a CustodyStateLog only
    # when there's a meaningful state transition or an explicit flag (like
    # contradiction) worth showing in the timeline.
    log = custody.on_eo_observation(obs)

    # Always ship the observation to the UI (the CrossSensorPanel wants it
    # even when there's no state change, to render the camera frame).
    STATE.eo_observations.append(obs)
    await broadcast("eo_observation", obs.model_dump())

    if log is not None:
        STATE.custody_logs.append(log)
        await broadcast("custody_log", log.model_dump())
        await broadcast("track_update", track.model_dump())


# ---------------------------------------------------------------------------
# Phase E — attribution + TDOA helpers
# ---------------------------------------------------------------------------

# Per-tick deterministic RNG seed for emitter position. We don't want the
# "true emitter" to teleport every tick; instead it drifts slowly. This RNG
# is consulted only by ``_pick_true_emitter_position`` and uses the global
# ``STATE.total_readings_processed`` counter as a slow clock so positions
# move smoothly between ticks.
_EMITTER_POS_RNG = np.random.default_rng(0xC0FFEE)


def _pick_true_emitter_position(
    predicted_class: str,
    blue_force: Optional[BlueForceFeed],
) -> tuple[float, float]:
    """Return the simulator's *ground-truth* emitter position for one tick.

    Phase E adds a TDOA layer on top of the existing pipeline. The TDOA
    solver needs a "where is the emitter, really?" answer to synthesise
    noisy time-of-arrival measurements at each sensor. We pick that
    position based on the ML classifier's predicted class so the demo
    looks plausible:

        * friendly_*               → drift around an actual blue unit
        * drone_control_repeated_burst, frequency_hopping, chirp
                                   → "threat axis" 1.5 km north of the AO
        * unknown_ood              → varied location near the AO (the
                                     persistence detector wants this to
                                     repeat at the same place to fire)
        * everything else          → near the AO centroid (urban background)
    """
    rng = _EMITTER_POS_RNG
    centroid_lat, centroid_lon = 34.055, -118.225

    if predicted_class.startswith("friendly_") and blue_force is not None:
        # For ``friendly_radio_burst`` the synth emits in 144-148 MHz which
        # matches the PRC-148 profile, so we put the truth at the unit
        # actually operating that radio (Blue-2). For other friendly
        # classes we pick a random unit. This keeps BLUE_ATTRIBUTED
        # demos crisp instead of relying on luck.
        prc148_holders = blue_force.units_with_emitter("emitter_blue_prc148_vhf")
        candidates = (
            prc148_holders
            if predicted_class == "friendly_radio_burst" and prc148_holders
            else blue_force.snapshot()
        )
        if candidates:
            u = candidates[int(rng.integers(0, len(candidates)))]
            # Within 50 m of the unit.
            return (
                u.lat + float(rng.normal(0, 0.0005)),
                u.lon + float(rng.normal(0, 0.0006)),
            )

    if predicted_class in ("drone_control_repeated_burst", "frequency_hopping", "chirp"):
        # Drone-class threats — drift around a "threat axis" 1.5-2 km
        # north-east of the AO centroid so the operator sees them
        # consistently come from the same direction.
        return (
            centroid_lat + 0.013 + float(rng.normal(0, 0.0008)),
            centroid_lon + 0.005 + float(rng.normal(0, 0.0008)),
        )

    if predicted_class == "unknown_ood":
        # Persistent-unknown emitter — picks one of three "lurking" spots
        # so the persistence detector will cluster recurrences. The spot
        # rotates slowly (every ~50 reads, ~45 s @ 0.9 s/tick) so a
        # ``persistent_unknown_demo`` scenario can fire 3-6 unknowns
        # without the spot drifting underneath it.
        spots = [
            (centroid_lat - 0.008, centroid_lon - 0.014),
            (centroid_lat + 0.005, centroid_lon - 0.020),
            (centroid_lat - 0.012, centroid_lon + 0.008),
        ]
        spot = spots[(STATE.total_readings_processed // 50) % len(spots)]
        return (
            spot[0] + float(rng.normal(0, 0.0003)),
            spot[1] + float(rng.normal(0, 0.0003)),
        )

    # Civilian / background classes — random urban-ish position.
    return (
        centroid_lat + float(rng.normal(0, 0.0030)),
        centroid_lon + float(rng.normal(0, 0.0030)),
    )


# ---------------------------------------------------------------------------
# Pipeline tick (one reading -> classification -> event)
# ---------------------------------------------------------------------------

async def process_tick(
    spec: np.ndarray,
    reading: RFSignalReading,
    true_class: str,
    scenario_note: Optional[str],
    source_info: dict,
    queue: OfflineQueue,
    custody: CustodyManager,
) -> None:
    classifier: EdgeClassifier = STATE.classifier  # type: ignore[assignment]
    baseline: LocalBaselineTracker = STATE.baseline  # type: ignore[assignment]

    # 1) Pre-encode to compute baseline deviation.
    if baseline.n_observed >= 8:
        with torch.no_grad():
            x = torch.from_numpy(spec).float().unsqueeze(0).unsqueeze(0)
            emb_pre = classifier.model.encode(x).squeeze(0).numpy()  # type: ignore[union-attr]
        baseline_dev = baseline.deviation(emb_pre)
    else:
        baseline_dev = 0.0

    # 2) Classify (full forward pass, returns the canonical embedding too).
    classifier.sensitivity = STATE.sensitivity_mode  # apply sensitivity mode
    classified = classifier.classify(spec, reading, baseline_deviation=baseline_dev)

    emb_arr = np.array(classified.embedding, dtype=np.float32)
    baseline.update(
        emb_arr,
        reading.dominant_freq_bin,
        reading.power_dbm,
        classified.is_anomaly,
    )

    # 3) Bookkeeping
    STATE.recent_readings.append(reading)
    STATE.recent_classifications.append(classified)
    STATE.total_readings_processed += 1

    # 4) Broadcast the live reading + classification
    spec_payload = reading.spectrogram_u8 if classified.is_anomaly or true_class != "background_noise" else None
    payload = {
        "reading": {
            **reading.model_dump(exclude={"spectrogram_u8"}),
            "spectrogram_u8": spec_payload,
        },
        "classified": classified.model_dump(),
        "scenario_note": scenario_note,
        "true_class": true_class,
        # Provenance: did this tick come from our synthetic generator or
        # from a real RadioML 2016.10A I/Q recording? The dashboard shows
        # this as a SYNTH/REAL badge so judges can see the system handle
        # genuine out-of-distribution input live.
        "source": source_info.get("source", "synth"),
        "source_meta": {k: v for k, v in source_info.items() if k != "source"},
    }
    await broadcast("classified", payload)

    # 5) Decide whether to emit an intelligence event
    event = action_engine.evaluate(
        reading,
        classified,
        network_online=STATE.network_online,
    )
    if event is None:
        # Filtered locally
        STATE.total_filtered_local += 1
        STATE.bytes_saved_at_edge += RAW_READING_BYTES
        await broadcast(
            "edge_status",
            STATE.device_status().model_dump(),
        )
        return

    # 6) Custody bookkeeping. on_event returns None for FALSE_ALARM classes
    # (friendly_radio_burst, commercial_continuous, background_noise) which
    # we still record as intelligence events but never tie to a track.
    custody_result = custody.on_event(
        event,
        dominant_freq_bin=reading.dominant_freq_bin,
        lat=reading.lat,
        lon=reading.lon,
    )
    STATE.intelligence_events.append(event)
    STATE.bytes_saved_at_edge += RAW_READING_BYTES
    if custody_result is not None:
        _track, _log = custody_result
        STATE.custody_logs.append(_log)

    # 6.5) Phase E — TDOA geolocation + attribution + persistence.
    #
    # We synthesise a "ground-truth" emitter position based on the ML
    # classifier's predicted class, simulate noisy TOAs at all 4 sensors,
    # and run the closed-form-then-iterated TDOA solver to recover a
    # noisy (lat, lon) + CEP. The recovered position is what attribution
    # uses, *not* the ground truth — same as in a real deployment where
    # the operator only ever sees the fix.
    blue_force: Optional[BlueForceFeed] = getattr(app.state, "blue_force", None)
    persistence: Optional[PersistenceTracker] = getattr(app.state, "persistence", None)

    truth_lat, truth_lon = _pick_true_emitter_position(classified.predicted_class, blue_force)
    try:
        tdoa_solution = tdoa_simulate_and_solve(
            truth_lat, truth_lon, STATE.sensor_array, rng=_EMITTER_POS_RNG
        )
    except Exception:
        logger.exception("TDOA solve failed")
        tdoa_solution = None

    attribution_result = attribution_attribute(
        reading,
        classified,
        blue_force=blue_force,
        fix_lat=tdoa_solution.lat if tdoa_solution else reading.lat,
        fix_lon=tdoa_solution.lon if tdoa_solution else reading.lon,
    )

    # Stamp FKs onto both records so rolling-buffer rows carry their event
    # id without a side-table lookup. The attribution_by_event map is the
    # primary lookup; attribution_recent is the time-ordered feed.
    early_track_id = custody_result[0].track_id if custody_result is not None else None
    attribution_result.event_id = event.id
    attribution_result.track_id = early_track_id
    if tdoa_solution is not None:
        tdoa_solution.event_id = event.id
        tdoa_solution.track_id = early_track_id
        tdoa_solution.truth_lat = truth_lat
        tdoa_solution.truth_lon = truth_lon

    # Bookkeeping + counters.
    STATE.attribution_by_event[event.id] = attribution_result
    STATE.attribution_recent.append(attribution_result)
    if attribution_result.verdict == "BLUE_ATTRIBUTED":
        STATE.counter_blue_attributed += 1
    elif attribution_result.verdict == "RED_KNOWN":
        STATE.counter_red_known += 1
    elif attribution_result.verdict == "UNEXPLAINED":
        STATE.counter_unexplained += 1
    else:
        STATE.counter_ambiguous += 1

    if tdoa_solution is not None:
        track_id = custody_result[0].track_id if custody_result is not None else None
        if track_id:
            STATE.tdoa_by_track[track_id] = tdoa_solution
        STATE.tdoa_recent.append(tdoa_solution)

    persistent_event = None
    if persistence is not None and tdoa_solution is not None:
        persistent_event = persistence.add(
            attribution_result,
            classified,
            tdoa_solution.lat,
            tdoa_solution.lon,
            timestamp=event.timestamp,
            event_id=event.id,
        )
        if persistent_event is not None:
            STATE.persistent_emitters[persistent_event.id] = persistent_event

    # 7) Sync or queue
    if event.sync_status == "queued":
        if event not in STATE.offline_queue:
            queue.enqueue(event)
    elif event.sync_status == "synced":
        STATE.total_events_synced += 1
        STATE.bytes_actually_synced += event.payload_size_bytes

    # 8) Broadcast
    await broadcast("intelligence_event", event.model_dump())
    if custody_result is not None:
        await broadcast("custody_log", _log.model_dump())
        await broadcast("track_update", _track.model_dump())
    await broadcast(
        "queue_update",
        {"depth": queue.size, "summary": queue.queue_summary()},
    )
    await broadcast("edge_status", STATE.device_status().model_dump())

    # 8.5) Phase E broadcasts — paired with the intelligence event above so
    # the dashboard can render attribution + CEP overlay on first paint.
    track_id = custody_result[0].track_id if custody_result is not None else None
    await broadcast(
        "attribution_result",
        {
            "event_id": event.id,
            "track_id": track_id,
            **attribution_result.model_dump(),
            "counters": {
                "blue_attributed": STATE.counter_blue_attributed,
                "red_known": STATE.counter_red_known,
                "unexplained": STATE.counter_unexplained,
                "ambiguous": STATE.counter_ambiguous,
            },
        },
    )
    if tdoa_solution is not None:
        await broadcast(
            "tdoa_fix",
            {
                "event_id": event.id,
                "track_id": track_id,
                "truth_lat": truth_lat,
                "truth_lon": truth_lon,
                **tdoa_solution.model_dump(),
            },
        )
    if persistent_event is not None:
        await broadcast("persistent_emitter", persistent_event.model_dump())

    # 8.6) Phase E — fire-and-forget pushes to the embedded Foundry sink
    # AND (Phase F) the real Palantir Foundry tenant if configured.
    # These never block the pipeline. Failures are recorded in
    # FOUNDRY_PUSH_METRICS (local sink) and on ``foundry_remote.snapshot()``
    # (real tenant), and surfaced on the Foundry sync indicator.
    # ``reading`` is passed through so the Foundry stream row carries the
    # RF features (freq / bw / power / duration) that aren't on the
    # compact IntelligenceEvent itself.
    foundry_push.schedule(foundry_push_intelligence_event(event, reading=reading))
    foundry_push.schedule(
        foundry_push_attribution(attribution_result, event_id=event.id, track_id=track_id)
    )
    if tdoa_solution is not None:
        foundry_push.schedule(
            foundry_push_tdoa_fix(
                tdoa_solution,
                event_id=event.id,
                track_id=track_id,
                truth_lat=truth_lat,
                truth_lon=truth_lon,
            )
        )
    if persistent_event is not None:
        foundry_push.schedule(foundry_push_persistent_emitter(persistent_event))

    # 9) Async LLM enrichment for high-priority events (non-blocking)
    if event.priority in ("high", "critical"):
        asyncio.create_task(enrich_event_with_llm(event))

    # 10) Phase A — cross-sensor cueing. If the RF custody manager opened or
    # updated a track that warrants visual attention (MEDIUM/HIGH threat, no
    # recent EO observation), tip the EO gimbal and fold its observation
    # back into the custody state machine. Fire-and-forget so the 400-1200 ms
    # simulated slew latency does not block the next RF tick.
    if custody_result is not None:
        _track, _log = custody_result
        if _should_tip_eo(_track):
            asyncio.create_task(
                tip_eo_for_track(_track, triggering_event_id=event.id, custody=custody)
            )


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def emulator_loop(
    emulator: RFEmulator,
    queue: OfflineQueue,
    custody: CustodyManager,
    stop_event: asyncio.Event,
    period_seconds: float = 0.9,
) -> None:
    """Single async loop that drives the entire edge pipeline."""
    STATE.emulator_running = True
    try:
        while not stop_event.is_set():
            if STATE.emulator_paused:
                await asyncio.sleep(0.1)
                continue
            spec, reading, true_class, scenario_note, source_info = emulator.tick()
            try:
                await process_tick(spec, reading, true_class, scenario_note, source_info, queue, custody)
            except Exception as e:
                logger.exception("process_tick failed: %s", e)
            await asyncio.sleep(period_seconds)
    finally:
        STATE.emulator_running = False


async def custody_sweep_loop(custody: CustodyManager, stop_event: asyncio.Event) -> None:
    # Sweep period was 5 s, but Phase A's VISUAL_LOST_RF_PRESENT degradation
    # has a 4 s freshness threshold. With a 5 s sweep the degrade can lag the
    # visual-loss event by up to 5 s, which is too coarse for an operator
    # display and breaks the cross_cue_demo timing (re-acquire emits land
    # before degrade fires). 2 s gives sub-state-machine-window resolution
    # while still being effectively free (small dict iteration).
    SWEEP_PERIOD_SECONDS = 2.0
    while not stop_event.is_set():
        try:
            logs = custody.sweep_timeouts()
            for log in logs:
                STATE.custody_logs.append(log)
                await broadcast("custody_log", log.model_dump())
        except Exception:
            logger.exception("custody_sweep failed")
        await asyncio.sleep(SWEEP_PERIOD_SECONDS)


async def blue_force_broadcast_loop(
    blue_force: BlueForceFeed,
    stop_event: asyncio.Event,
    period_seconds: float = 4.0,
) -> None:
    """Periodically broadcast the live blue-force feed to the dashboard
    and re-push to the Foundry sink so positions stay synchronized.
    """
    while not stop_event.is_set():
        try:
            units = blue_force.snapshot()
            await broadcast(
                "blue_force_update",
                {"units": [u.model_dump() for u in units]},
            )
            # Re-push to Foundry so positions stay current. Best-effort.
            foundry_push.schedule(foundry_push_blue_force(units))
        except Exception:
            logger.exception("blue_force_broadcast failed")
        await asyncio.sleep(period_seconds)


async def persistence_prune_loop(
    persistence: PersistenceTracker,
    stop_event: asyncio.Event,
    period_seconds: float = 60.0,
) -> None:
    """Periodically drop stale (>1 hr) detections from persistence clusters."""
    while not stop_event.is_set():
        try:
            persistence.prune()
        except Exception:
            logger.exception("persistence_prune failed")
        await asyncio.sleep(period_seconds)


# ---------------------------------------------------------------------------
# Lifespan (startup/shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SpectrumCustody starting up")

    # 1) Load classifier
    classifier = EdgeClassifier()
    classifier.load()
    STATE.classifier = classifier
    logger.info("classifier loaded: %s", classifier.summary())

    # 2) Local baseline tracker
    STATE.baseline = LocalBaselineTracker(window=120, embed_dim=classifier.embed_dim)

    # 3) LLM client (best-effort health check, non-blocking)
    llm = EdgeLLM()
    STATE.llm = llm
    asyncio.create_task(llm.health())  # caches availability in background
    logger.info("LLM endpoint configured: %s", llm.base_url)

    # 4) Pipeline state. The RadioMLPool wraps the optional DeepSig RML2016.10A
    # dataset on disk. If the pickle isn't present the pool is harmless: the
    # emulator skips real-data injection and stays purely synthetic.
    real_pool = RadioMLPool()
    STATE.real_data_available = real_pool.available
    if real_pool.available:
        logger.info("RadioML 2016.10A pool available: %s", real_pool.path)
    else:
        logger.info("RadioML 2016.10A not found at %s — staying synth-only", real_pool.path)
    emulator = RFEmulator(real_pool=real_pool, real_data_mix=STATE.real_data_mix)
    queue = OfflineQueue(STATE.offline_queue)
    custody = CustodyManager(STATE.tracks)
    # Phase A — simulated EO/IR tipping camera. The sensor has no network
    # dependency; it runs entirely on-device just like the RF pipeline.
    eo_sensor = EOSensor()
    logger.info("EO sensor initialised: %s", eo_sensor.sensor_id)

    # Phase E — blue-force feed + persistence tracker.
    blue_force = BlueForceFeed(update_period_s=4.0)
    persistence = PersistenceTracker()
    await blue_force.start()
    logger.info(
        "Phase E ready: blue_force=%d units, sensors=%d, library=%d emitters",
        len(blue_force.snapshot()),
        len(STATE.sensor_array),
        len(STATE.emitter_library),
    )

    # Phase F — start the real Palantir Foundry remote transport BEFORE
    # we push reference data, so reference data also flows to the tenant.
    # If FOUNDRY_STACK_URL / FOUNDRY_TOKEN aren't set, the transport stays
    # disabled and every remote push is a no-op (local sink keeps working).
    foundry_remote_transport = FoundryRemoteTransport()
    await foundry_remote_transport.start()
    foundry_remote.set_transport(foundry_remote_transport)
    app.state.foundry_remote = foundry_remote_transport
    if foundry_remote_transport.is_enabled():
        snap = foundry_remote_transport.snapshot()
        logger.info(
            "Foundry REMOTE enabled: stack=%s, streams_configured=%d, missing=%s",
            snap.stack_url, len(snap.configured_streams), snap.missing_streams,
        )

    # Phase E — push reference data to the Foundry sink at startup so the
    # ontology has a complete record from the first tick (sensor positions
    # and emitter profiles never change at runtime; blue-force units do —
    # they get re-pushed in their own broadcast loop).
    try:
        await foundry_push_sensor_nodes(STATE.sensor_array)
        await foundry_push_emitter_profiles(STATE.emitter_library)
        await foundry_push_blue_force(blue_force.snapshot())
        logger.info("Foundry reference data pushed (sensors + emitters + blue-force)")
    except Exception:
        logger.exception("foundry reference push failed (sink may be down)")

    # Stash for endpoints
    app.state.emulator = emulator
    app.state.queue = queue
    app.state.custody = custody
    app.state.eo_sensor = eo_sensor
    app.state.blue_force = blue_force
    app.state.persistence = persistence
    app.state.stop_event = asyncio.Event()

    # 5) Background loops
    task_pipeline = asyncio.create_task(
        emulator_loop(emulator, queue, custody, app.state.stop_event, period_seconds=0.9)
    )
    task_custody = asyncio.create_task(custody_sweep_loop(custody, app.state.stop_event))
    task_blueforce = asyncio.create_task(
        blue_force_broadcast_loop(blue_force, app.state.stop_event)
    )
    task_persistence = asyncio.create_task(
        persistence_prune_loop(persistence, app.state.stop_event)
    )

    try:
        yield
    finally:
        logger.info("SpectrumCustody shutting down")
        app.state.stop_event.set()
        with contextlib.suppress(BaseException):
            await blue_force.stop()
        with contextlib.suppress(BaseException):
            await foundry_remote_transport.stop()
        foundry_remote.set_transport(None)
        for t in (task_pipeline, task_custody, task_blueforce, task_persistence):
            t.cancel()
            with contextlib.suppress(BaseException):
                await t


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="SpectrumCustody Edge", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phase E — embedded Foundry-compatible sink. In production this would be a
# remote Foundry endpoint; for the demo it's mounted in-process under
# /foundry/* so the dashboard can render a Foundry sync indicator.
app.include_router(foundry_router)


# ---------------------------------------------------------------------------
# Health + state snapshot
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    llm: EdgeLLM = STATE.llm  # type: ignore[assignment]
    return {
        "ok": True,
        "model_loaded": STATE.classifier is not None,
        "model_summary": STATE.classifier.summary() if STATE.classifier else None,
        "llm_endpoint": llm.base_url,
        "llm_available": bool(llm.available),
        "llm_model": llm.model,
        "network_online": STATE.network_online,
        "sensitivity_mode": STATE.sensitivity_mode,
    }


@app.get("/api/state")
async def state_snapshot() -> dict:
    return {
        "device": STATE.device_status().model_dump(),
        "baseline": STATE.baseline.baseline_summary() if STATE.baseline else {},
        "tracks": [t.model_dump() for t in STATE.tracks.values()],
        "recent_classifications": [
            c.model_dump() for c in list(STATE.recent_classifications)[-30:]
        ],
        "intelligence_events": [
            e.model_dump() for e in list(STATE.intelligence_events)[-30:]
        ],
        "custody_logs": [c.model_dump() for c in list(STATE.custody_logs)[-30:]],
        # Phase A — recent EO/IR tipping-camera observations so a freshly
        # connected client can immediately render the CrossSensorPanel.
        "eo_observations": [o.model_dump() for o in list(STATE.eo_observations)[-30:]],
        # Phase B — recent CoT publications. Trim to last 20 in the snapshot
        # to keep the hello payload small; the dashboard top-up via WS.
        "cot_publications": [
            p.model_dump() for p in list(STATE.cot_publications)[-20:]
        ],
        # Phase C — ROE posture + recent COA decisions. The posture drives
        # what the COA recommender will surface; the decisions list is the
        # audit trail for what the operator has actually done.
        "roe": {
            "posture": STATE.roe_posture,
            "description": ROE_DESCRIPTIONS.get(STATE.roe_posture, ""),
            "options": [
                {"posture": p, "description": ROE_DESCRIPTIONS[p]}
                for p in ROE_POSTURES
            ],
        },
        "coa_decisions": list(STATE.coa_decisions)[-30:],
        "queue": {
            "depth": len(STATE.offline_queue),
            "items": [e.model_dump() for e in list(STATE.offline_queue)[-30:]],
        },
        "scenario_active": STATE.scenario_active,
        "emitter_library": [e.model_dump() for e in STATE.emitter_library],
        "available_scenarios": [
            {"name": s.name, "description": s.description} for s in SCENARIOS.values()
        ],
        "raw_class_weights": DEFAULT_CLASS_WEIGHTS,
        # Phase E — sensor array, blue-force feed, attribution + TDOA buffers,
        # persistent-emitter clusters, and aggregated counters.
        "sensor_array": [s.model_dump() for s in STATE.sensor_array],
        "blue_force": [u.model_dump() for u in STATE.blue_force.values()],
        "attribution_recent": [
            a.model_dump() for a in list(STATE.attribution_recent)[-30:]
        ],
        "tdoa_recent": [t.model_dump() for t in list(STATE.tdoa_recent)[-30:]],
        "persistent_emitters": [
            p.model_dump() for p in STATE.persistent_emitters.values()
        ],
        "attribution_counters": {
            "blue_attributed": STATE.counter_blue_attributed,
            "red_known": STATE.counter_red_known,
            "unexplained": STATE.counter_unexplained,
            "ambiguous": STATE.counter_ambiguous,
        },
        # Phase E + F — embedded Foundry sink metrics + real-tenant remote
        # transport status. The dashboard renders both: the local sink keeps
        # the demo running with no creds, and the remote section lights up
        # the moment FOUNDRY_STACK_URL / FOUNDRY_TOKEN / FOUNDRY_STREAM_RIDS
        # are configured.
        "foundry_sync": {
            "transport": foundry_push._transport_mode(),  # noqa: SLF001
            "push_metrics": FOUNDRY_PUSH_METRICS.snapshot(),
            "remote": _foundry_remote_snapshot_dict(),
        },
    }


def _foundry_remote_snapshot_dict() -> dict[str, Any]:
    """Return the remote transport snapshot as a JSON-friendly dict."""
    transport = foundry_remote.get_transport()
    if transport is None:
        return {
            "enabled": False,
            "online": False,
            "stack_url": None,
            "configured_streams": [],
            "missing_streams": list(foundry_remote.STREAM_KEYS),
            "streams": {},
            "ddil_buffer_dir": None,
            "last_flush_ts": None,
        }
    snap = transport.snapshot()
    return {
        "enabled": snap.enabled,
        "online": snap.online,
        "stack_url": snap.stack_url,
        "configured_streams": snap.configured_streams,
        "missing_streams": snap.missing_streams,
        "streams": snap.streams,
        "ddil_buffer_dir": snap.ddil_buffer_dir,
        "last_flush_ts": snap.last_flush_ts,
    }


# ---------------------------------------------------------------------------
# Network + sensitivity controls
# ---------------------------------------------------------------------------

async def _drain_queue_and_sync(queue: OfflineQueue) -> int:
    drained = queue.drain()
    n_synced = 0
    for e in drained:
        STATE.intelligence_events.append(e)
        STATE.total_events_synced += 1
        STATE.bytes_actually_synced += e.payload_size_bytes
        n_synced += 1
        await broadcast("intelligence_event_update", {"event_id": e.id, "sync_status": "synced"})
    if n_synced:
        await broadcast(
            "sync_complete",
            {"n_synced": n_synced, "events": [e.model_dump() for e in drained]},
        )
        await broadcast("queue_update", {"depth": queue.size, "summary": queue.queue_summary()})
        await broadcast("edge_status", STATE.device_status().model_dump())
    return n_synced


@app.post("/api/network/toggle")
async def toggle_network(req: NetworkToggleReq) -> dict:
    prev = STATE.network_online
    STATE.network_online = bool(req.online)
    cmd = EdgeCommand(
        id=new_id("cmd_"),
        timestamp=utc_now(),
        command_type="MARK_FRIENDLY" if req.online else "REDUCE_SENSITIVITY",  # placeholder
        params={"network_online": req.online},
        status="executed",
        issued_by="operator",
    )
    STATE.commands.append(cmd)
    await broadcast(
        "command",
        {"command_type": "NETWORK_TOGGLE", "network_online": req.online},
    )
    n_synced = 0
    if not prev and req.online:
        # Coming back online -> drain queue
        queue: OfflineQueue = app.state.queue
        n_synced = await _drain_queue_and_sync(queue)
    await broadcast("edge_status", STATE.device_status().model_dump())
    return {"network_online": STATE.network_online, "n_synced_on_return": n_synced}


@app.post("/api/command/sensitivity")
async def set_sensitivity(req: SensitivityReq) -> dict:
    STATE.sensitivity_mode = req.mode
    if STATE.classifier:
        STATE.classifier.sensitivity = req.mode
    await broadcast(
        "command",
        {"command_type": "SET_SENSITIVITY", "mode": req.mode},
    )
    await broadcast("edge_status", STATE.device_status().model_dump())
    return {"sensitivity_mode": STATE.sensitivity_mode}


@app.post("/api/command/watch_band")
async def watch_band(req: WatchBandReq) -> dict:
    STATE.watch_band_mhz = (req.freq_min_mhz, req.freq_max_mhz)
    await broadcast(
        "command",
        {
            "command_type": "WATCH_BAND",
            "freq_min_mhz": req.freq_min_mhz,
            "freq_max_mhz": req.freq_max_mhz,
            "duration_minutes": req.duration_minutes,
        },
    )
    await broadcast("edge_status", STATE.device_status().model_dump())
    return {"watch_band_mhz": STATE.watch_band_mhz}


@app.post("/api/command/real_data_mix")
async def set_real_data_mix(req: RealDataMixReq) -> dict:
    """Dial the fraction of free-run ticks that come from real RadioML I/Q.

    Setting this above 0 only has an effect when the dataset pickle is on
    disk (`STATE.real_data_available == True`). The setting is silently
    ignored otherwise so the call doesn't error during demos on machines
    without the dataset.
    """
    STATE.real_data_mix = req.mix
    emulator: RFEmulator = app.state.emulator
    emulator.real_data_mix = req.mix
    await broadcast(
        "command",
        {"command_type": "REAL_DATA_MIX", "mix": req.mix},
    )
    await broadcast("edge_status", STATE.device_status().model_dump())
    return {
        "real_data_mix": STATE.real_data_mix,
        "real_data_available": STATE.real_data_available,
    }


# ---------------------------------------------------------------------------
# Operator actions (CONFIRM / DISMISS / ESCALATE / etc.)
# ---------------------------------------------------------------------------

@app.post("/api/operator/action")
async def operator_action(req: OperatorActionReq) -> dict:
    action = OperatorAction(
        track_id=req.track_id,
        event_id=req.event_id,
        action_type=req.action_type,
        details=req.details,
    )
    STATE.operator_actions.append(action)
    custody: CustodyManager = app.state.custody

    # Apply to custody state if applicable
    if req.track_id and req.track_id in STATE.tracks:
        if req.action_type == "DISMISS":
            log = custody.transition(
                req.track_id, "DISMISSED",
                action_cue=f"Operator dismissed track: {req.details or 'no notes'}",
            )
        elif req.action_type == "CONFIRM":
            log = custody.transition(
                req.track_id, "REACQUIRED",
                action_cue=f"Operator confirmed visual: {req.details or 'no notes'}",
            )
        elif req.action_type == "ESCALATE":
            log = None  # no state change but record action
            STATE.tracks[req.track_id].threat_level = "HIGH"
        elif req.action_type == "MARK_FRIENDLY":
            log = custody.transition(
                req.track_id, "CLEARED",
                action_cue=f"Operator marked friendly: {req.details or ''}",
            )
        else:
            log = None
        if log:
            STATE.custody_logs.append(log)
            await broadcast("custody_log", log.model_dump())
        await broadcast("track_update", STATE.tracks[req.track_id].model_dump())

    await broadcast("operator_action", action.model_dump())
    return {"action": action.model_dump()}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

async def run_scenario(scenario: Scenario) -> None:
    """Replace the emulator's default behaviour with a scripted sequence."""
    emulator: RFEmulator = app.state.emulator
    queue: OfflineQueue = app.state.queue
    STATE.scenario_active = True
    await broadcast(
        "scenario_step",
        {"phase": "start", "name": scenario.name, "description": scenario.description},
    )

    # Build emit-only steps for the emulator script.
    emit_steps_only = []
    for step in scenario.steps:
        if isinstance(step, EmitStep):
            from app.pipeline.emulator import ScriptedStep
            emit_steps_only.append(
                ScriptedStep(class_name=step.class_name, dwell_ticks=step.dwell_ticks, note=step.note)
            )
    emulator.load_script(emit_steps_only)

    # Walk all step types in order, executing control steps inline.
    # The emulator consumes EmitSteps through its own queue; we just sleep for
    # roughly the right duration between control steps.
    for step in scenario.steps:
        if isinstance(step, EmitStep):
            # Wait roughly dwell_ticks * tick_period; emulator is at ~0.9s/tick.
            await asyncio.sleep(step.dwell_ticks * 0.95)
        elif isinstance(step, NetworkStep):
            STATE.network_online = step.online
            await broadcast("scenario_step", {"phase": "network", "online": step.online, "note": step.note})
            if step.online:
                await _drain_queue_and_sync(queue)
            await broadcast("edge_status", STATE.device_status().model_dump())
            await asyncio.sleep(0.4)
        elif isinstance(step, SensitivityStep):
            STATE.sensitivity_mode = step.mode
            if STATE.classifier:
                STATE.classifier.sensitivity = step.mode
            await broadcast("scenario_step", {"phase": "sensitivity", "mode": step.mode, "note": step.note})
            await broadcast("edge_status", STATE.device_status().model_dump())
            await asyncio.sleep(0.3)
        elif isinstance(step, AnnounceStep):
            await broadcast("scenario_step", {"phase": "announce", "text": step.text})
            await asyncio.sleep(0.3)
        elif isinstance(step, EOFailStep):
            # Phase A — force the simulated EO sensor into no_visual for the
            # configured duration. The sensor self-re-enables when its
            # internal timer expires; we don't await it here.
            eo: EOSensor = app.state.eo_sensor  # type: ignore[attr-defined]
            eo.disable_for(step.seconds)
            await broadcast(
                "scenario_step",
                {
                    "phase": "eo_fail",
                    "seconds": step.seconds,
                    "note": step.note or "EO subsystem masked",
                    "text": (
                        f"EO subsystem masked for {step.seconds:.0f}s "
                        "(scenario step / hardware fault sim)."
                    ),
                },
            )
            await asyncio.sleep(0.3)
        elif isinstance(step, WaitStep):
            await asyncio.sleep(step.seconds)

    emulator.clear_script()
    STATE.scenario_active = False
    await broadcast("scenario_step", {"phase": "done", "name": scenario.name})


@app.post("/api/scenario/run")
async def scenario_run(req: ScenarioRunReq) -> dict:
    if STATE.scenario_active:
        raise HTTPException(409, "scenario already running")
    scenario = SCENARIOS.get(req.scenario)
    if not scenario:
        raise HTTPException(404, f"unknown scenario {req.scenario}")
    asyncio.create_task(run_scenario(scenario))
    return {"started": scenario.name, "n_steps": len(scenario.steps)}


@app.post("/api/scenario/stop")
async def scenario_stop() -> dict:
    emulator: RFEmulator = app.state.emulator
    emulator.clear_script()
    STATE.scenario_active = False
    return {"stopped": True}


# ---------------------------------------------------------------------------
# CoT / ATAK (Phase B)
# ---------------------------------------------------------------------------

def _find_event(event_id: str) -> IntelligenceEvent:
    """Locate an IntelligenceEvent in the rolling buffer or raise 404."""
    # We search newest-first because operators almost always publish the
    # event they just saw; iterating the deque from the right end keeps
    # the worst case small for fresh events.
    for evt in reversed(STATE.intelligence_events):
        if evt.id == event_id:
            return evt
    raise HTTPException(404, f"event {event_id} not found")


def _track_for_event(event: IntelligenceEvent) -> Optional[Any]:
    """Return the live track associated with this event, if any.

    The track may have been pruned (TRACK_LOST + purge), in which case we
    publish a CoT off the event-only data — still useful for an operator
    looking at historical contacts.
    """
    return STATE.tracks.get(event.track_id)


@app.get("/api/intel/{event_id}/cot")
async def cot_preview(event_id: str, stale_seconds: int = 60) -> dict:
    """Return the CoT XML + structured dict for an existing intel event.

    This is a *preview* — nothing is broadcast or logged. Used by the
    Publish-to-ATAK modal so the operator can review the wire bytes
    before committing to a publish.
    """
    event = _find_event(event_id)
    track = _track_for_event(event)
    cot_d = build_cot_dict(event, track, stale_seconds=stale_seconds)
    cot_x = build_cot_xml(event, track, stale_seconds=stale_seconds)
    return {"event_id": event.id, "cot_dict": cot_d, "xml": cot_x}


@app.post("/api/intel/{event_id}/cot/publish")
async def cot_publish(event_id: str, req: CotPublishReq) -> dict:
    """Publish a CoT for an intel event.

    The actual transport for the demo is a WebSocket broadcast (so all
    connected dashboards see the publication and can render it on the
    map / in the CoT panel) plus an info-level log. Phase B3 will add
    UDP broadcast to a FreeTAKServer instance behind the same endpoint.
    """
    event = _find_event(event_id)
    track = _track_for_event(event)

    cot_d = build_cot_dict(event, track, stale_seconds=req.stale_seconds)
    cot_x = build_cot_xml(event, track, stale_seconds=req.stale_seconds)

    publication = CotPublication(
        event_id=event.id,
        track_id=track.track_id if track is not None else None,
        site_id=event.site_id,
        sensor_id=event.sensor_id,
        cot_uid=cot_d["uid"],
        cot_type=cot_d["type"],
        sidc=cot_d["detail"]["_spectrumcustody"]["sidc"],
        callsign=cot_d["detail"]["contact"]["callsign"],
        icon_name=cot_d["detail"]["_spectrumcustody"]["icon_name"],
        stale_seconds=req.stale_seconds,
        xml=cot_x,
        cot_dict=cot_d,
        transport=req.transport,
        status="published",
        note=(
            "Broadcast over WebSocket to dashboard subscribers. "
            "Connect FreeTAKServer to /api/cot/udp_target for full TAK."
            if req.transport == "broadcast"
            else "Logged only; no broadcast."
        ),
    )

    STATE.cot_publications.append(publication)
    logger.info(
        "CoT published: uid=%s type=%s sidc=%s event=%s",
        publication.cot_uid,
        publication.cot_type,
        publication.sidc,
        publication.event_id,
    )
    if req.transport in ("broadcast", "udp"):
        await broadcast("cot_published", publication.model_dump())

    return publication.model_dump()


@app.get("/api/cot")
async def cot_list(limit: int = 30) -> dict:
    """Return the recent CoT publications buffer."""
    items = list(STATE.cot_publications)[-max(1, min(limit, 100)):]
    return {"count": len(items), "items": [p.model_dump() for p in items]}


# ---------------------------------------------------------------------------
# COA recommender (Phase C)
# ---------------------------------------------------------------------------

def _resolve_coa_target(req: CoaRecommendReq) -> tuple[Optional[Any], Optional[IntelligenceEvent]]:
    """Resolve ``(track, event)`` from the request.

    Rules:
      * If ``track_id`` given, look it up in STATE.tracks.
      * If ``event_id`` given, look it up and use its track (if present).
      * At least one of ``track_id`` / ``event_id`` must be supplied.
    """
    if not req.track_id and not req.event_id:
        raise HTTPException(400, "track_id or event_id required")

    event: Optional[IntelligenceEvent] = None
    if req.event_id:
        for e in reversed(STATE.intelligence_events):
            if e.id == req.event_id:
                event = e
                break
        if event is None:
            raise HTTPException(404, f"event {req.event_id} not found")

    track = None
    if req.track_id:
        track = STATE.tracks.get(req.track_id)
    elif event is not None:
        track = STATE.tracks.get(event.track_id)
    return track, event


@app.get("/api/coa/posture")
async def coa_get_posture() -> dict:
    """Return the current ROE posture + all selectable options."""
    return {
        "posture": STATE.roe_posture,
        "description": ROE_DESCRIPTIONS.get(STATE.roe_posture, ""),
        "options": [
            {"posture": p, "description": ROE_DESCRIPTIONS[p]}
            for p in ROE_POSTURES
        ],
    }


@app.post("/api/coa/posture")
async def coa_set_posture(req: CoaPostureReq) -> dict:
    """Change the sitewide ROE posture.

    The change is broadcast as ``coa_posture_changed`` so every dashboard
    UI updates its ROE indicator and its COA recommendation view at the
    same time (dashboards re-request recommendations on posture change).
    """
    if req.posture not in ROE_POSTURES:
        raise HTTPException(400, f"unknown posture {req.posture!r}")
    prev = STATE.roe_posture
    STATE.roe_posture = req.posture
    payload = {
        "previous": prev,
        "posture": req.posture,
        "description": ROE_DESCRIPTIONS[req.posture],
    }
    logger.info("ROE posture: %s -> %s", prev, req.posture)
    await broadcast("coa_posture_changed", payload)
    return payload


@app.post("/api/coa/recommend")
async def coa_recommend_endpoint(req: CoaRecommendReq) -> dict:
    """Return a ranked list of COA options for a track / event + posture.

    The recommender is pure-function (see ``app.pipeline.coa``); this
    endpoint is a thin shell that resolves the target, falls back to the
    live ROE posture if none was supplied, and serialises the result.
    """
    track, event = _resolve_coa_target(req)
    posture: RoePosture = req.posture or STATE.roe_posture  # type: ignore[assignment]
    rec = coa_recommend(track, event, posture=posture, top_n=req.top_n)
    return recommendation_to_dict(rec)


@app.post("/api/coa/execute")
async def coa_execute(req: CoaExecuteReq) -> dict:
    """Record an operator's COA commitment and fire side-effects.

    This is deliberately audit-first: every execute call appends an
    immutable record to ``STATE.coa_decisions`` and broadcasts it, so
    the full decision trail is visible on every dashboard and in the
    after-action brief. Side-effects (sensitivity nudge, mark-friendly)
    are delegated to the existing operator-action path for consistency.
    """
    posture: RoePosture = req.posture or STATE.roe_posture  # type: ignore[assignment]
    decision = {
        "id": new_id("coa_"),
        "timestamp": utc_now().isoformat().replace("+00:00", "Z"),
        "track_id": req.track_id,
        "event_id": req.event_id,
        "action_id": req.action_id,
        "posture": posture,
        "notes": req.notes,
    }
    STATE.coa_decisions.append(decision)

    # Optional side-effects — mapped onto the existing operator-action
    # surface so the rest of the system (custody manager, emitter
    # library, audit log) picks them up without special-casing COA.
    side_effect: dict[str, Any] = {"action": req.action_id, "executed": False}
    if req.action_id == "INCREASE_SENSITIVITY":
        STATE.sensitivity_mode = "high"
        if STATE.classifier:
            STATE.classifier.sensitivity = "high"
        side_effect = {"action": req.action_id, "executed": True, "effect": "sensitivity -> high"}
    elif req.action_id == "MARK_FRIENDLY" and req.track_id:
        op = OperatorAction(
            track_id=req.track_id,
            event_id=req.event_id,
            action_type="MARK_FRIENDLY",
            details=req.notes or "Marked friendly via COA recommender",
        )
        STATE.operator_actions.append(op)
        side_effect = {"action": req.action_id, "executed": True, "effect": "marked friendly"}
    elif req.action_id == "DISMISS" and req.track_id:
        op = OperatorAction(
            track_id=req.track_id,
            event_id=req.event_id,
            action_type="DISMISS",
            details=req.notes or "Dismissed via COA recommender",
        )
        STATE.operator_actions.append(op)
        side_effect = {"action": req.action_id, "executed": True, "effect": "dismissed"}

    logger.info(
        "COA execute: track=%s event=%s action=%s posture=%s",
        req.track_id,
        req.event_id,
        req.action_id,
        posture,
    )
    await broadcast("coa_executed", {"decision": decision, "side_effect": side_effect})
    return {"decision": decision, "side_effect": side_effect}


@app.get("/api/coa/decisions")
async def coa_decisions(limit: int = 30) -> dict:
    items = list(STATE.coa_decisions)[-max(1, min(limit, 200)):]
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# LLM endpoints
# ---------------------------------------------------------------------------

@app.post("/api/llm/brief")
async def llm_brief() -> dict:
    """Generate an after-action brief over recent events."""
    llm: EdgeLLM = STATE.llm  # type: ignore[assignment]
    events = [e.model_dump() for e in list(STATE.intelligence_events)[-24:]]
    brief: Optional[str] = None
    if llm and llm.available:
        brief = await llm.after_action_brief(events)
    if not brief:
        brief = template_after_action(events)
    await broadcast("llm_brief", {"brief": brief})
    return {"brief": brief, "source": "llm" if (llm and llm.available) else "template"}


@app.post("/api/llm/query")
async def llm_query(req: NLQueryReq) -> dict:
    llm: EdgeLLM = STATE.llm  # type: ignore[assignment]
    parsed: dict = {}
    if llm and llm.available:
        parsed = await llm.parse_query(req.query)
    return {"query": req.query, "filter": parsed, "source": "llm" if (llm and llm.available) else "fallback"}


# ---------------------------------------------------------------------------
# Foundry-shaped exports
# ---------------------------------------------------------------------------

@app.get("/api/foundry/remote/status")
async def foundry_remote_status() -> dict:
    """Live status of the real Palantir Foundry remote transport.

    Returns the same dict shape that ``state_snapshot()`` includes under
    ``foundry_sync.remote``. Useful for the dashboard's Foundry status
    panel (polled every few seconds) without pulling the full state.
    """
    return _foundry_remote_snapshot_dict()


@app.post("/api/foundry/remote/replay")
async def foundry_remote_replay() -> dict:
    """Force a flush of all DDIL-buffered rows to the remote tenant.

    Useful when comms have just returned and the operator wants the
    catch-up to be visible immediately, instead of waiting for the next
    background flush cycle.
    """
    transport = foundry_remote.get_transport()
    if transport is None or not transport.is_enabled():
        raise HTTPException(503, "remote Foundry transport is not enabled")
    # The transport's flush loop walks all stream keys; we just nudge it
    # by calling _replay_one_stream() for each configured stream so the
    # caller sees the immediate effect.
    drained: dict[str, int] = {}
    for key in foundry_remote.STREAM_KEYS:
        if not transport.is_configured(key):
            continue
        before = transport.snapshot().streams.get(key, {}).get("queued", 0)
        await transport._replay_one_stream(key)  # noqa: SLF001
        after = transport.snapshot().streams.get(key, {}).get("queued", 0)
        drained[key] = max(0, before - after)
    return {"drained": drained, "snapshot": _foundry_remote_snapshot_dict()}


@app.get("/api/exports/foundry")
async def foundry_zip() -> StreamingResponse:
    classifications = {
        c.reading_id: c.predicted_class for c in list(STATE.recent_classifications)
    }
    paths = foundry_export.export_full_state(
        readings=list(STATE.recent_readings),
        classifications=classifications,
        tracks=STATE.tracks,
        custody_logs=list(STATE.custody_logs),
        events=list(STATE.intelligence_events) + list(STATE.offline_queue),
        operator_actions=list(STATE.operator_actions),
        device_status=STATE.device_status(),
    )
    # Bundle into a zip stream
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, fname in paths.items():
            full = foundry_export.EXPORT_DIR / fname
            z.write(full, arcname=fname)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=spectrumcustody_foundry.zip"},
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    async with STATE.ws_lock:
        STATE.ws_clients.add(websocket)
    # Send initial hello with snapshot.
    snapshot = await state_snapshot()
    try:
        await websocket.send_text(
            json.dumps(
                {"type": "hello", "payload": snapshot, "ts": utc_now().isoformat()},
                default=str,
            )
        )
        while True:
            # Keep the connection alive; ignore inbound text for now.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        async with STATE.ws_lock:
            STATE.ws_clients.discard(websocket)
