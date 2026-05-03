"""Pydantic models for SpectrumCustody.

All schemas are intentionally shaped to mirror the Palantir Foundry ontology
defined in context.md. The same JSON we emit over WebSocket can be ingested
into Foundry datasets without transformation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ID + timestamp helpers
# ---------------------------------------------------------------------------

def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Sensor + classifier output (edge layer)
# ---------------------------------------------------------------------------

Priority = Literal["low", "medium", "high", "critical"]
SyncStatus = Literal["local_only", "queued", "synced", "failed"]
NetworkState = Literal["online", "offline"]


class RFSignalReading(BaseModel):
    """Raw RF feature event from the sensor emulator (or future SDR/CASK)."""

    id: str = Field(default_factory=lambda: new_id("rd_"))
    timestamp: datetime = Field(default_factory=utc_now)
    sensor_id: str = "EDGE-ALPHA-01"
    site_id: str = "Alpha Site - Forward OP"
    lat: float = 34.0522
    lon: float = -118.2437
    center_frequency_mhz: float
    bandwidth_khz: int
    power_dbm: float
    duration_ms: int
    burst_pattern: str
    dominant_freq_bin: int
    energy: float
    raw_source: str = "emulator"
    # Quantised uint8 spectrogram (64x64), only attached when needed for UI.
    spectrogram_u8: Optional[list[list[int]]] = None


class ClassifiedSignal(BaseModel):
    """Output of the edge ML classifier for a single reading."""

    id: str = Field(default_factory=lambda: new_id("cls_"))
    reading_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    predicted_class: str
    confidence: float
    embedding: list[float]
    softmax: list[float]
    nearest_known_class: str
    distance_to_nearest_centroid: float
    reconstruction_error: float
    ood_score: float
    baseline_deviation: float
    is_anomaly: bool
    priority: Priority
    action: Literal["ignore", "log", "queue", "sync"]
    explanation: str


# ---------------------------------------------------------------------------
# Intelligence + custody (these are the things that sync upward)
# ---------------------------------------------------------------------------

EventType = Literal[
    "RF_ANOMALY",
    "FRIENDLY_EMISSION",
    "POSSIBLE_UAS_ACTIVITY",
    "PROFILE_MISMATCH",
    "PERSISTENT_UNKNOWN",
]


class IntelligenceEvent(BaseModel):
    """Compact, ontology-shaped event. This is what crosses the edge->cloud boundary."""

    id: str = Field(default_factory=lambda: new_id("evt_"))
    timestamp: datetime = Field(default_factory=utc_now)
    site_id: str = "Alpha Site - Forward OP"
    sensor_id: str = "EDGE-ALPHA-01"
    track_id: str
    event_type: EventType
    title: str
    summary: str
    classification: str  # the predicted_class
    confidence: float
    ood_score: float
    priority: Priority
    evidence: list[str]
    recommended_action: str
    sync_status: SyncStatus = "local_only"
    network_state_at_detection: NetworkState
    payload_size_bytes: int = 0
    llm_brief: Optional[str] = None  # filled in by GenAI layer if available
    raw_size_estimate_bytes: int = 0  # what would have been sent without edge filtering


CustodyState = Literal[
    "DETECTED",
    "TRACKING",
    "VISUAL_LOST_RF_PRESENT",
    "REACQUIRED",
    "TRACK_LOST",
    "CLEARED",
    "DISMISSED",
]


# Multi-modal evidence types backing a custody-state transition. RF is the
# primary modality this edge node ships with; EO is the simulated tipping
# camera (Phase A). ACOUSTIC is reserved for a future microphone array.
SensorModality = Literal["RF", "EO", "ACOUSTIC"]


class CustodyStateLog(BaseModel):
    id: str = Field(default_factory=lambda: new_id("log_"))
    track_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    previous_state: Optional[CustodyState]
    new_state: CustodyState
    action_cue: str
    evidence_summary: str
    triggering_event_id: Optional[str] = None
    # Which sensor modalities supported this state transition. Defaults to
    # RF because every transition before Phase A is RF-only. Multi-modal
    # transitions (e.g. RF + EO confirmation) carry both.
    evidence_modalities: list[SensorModality] = Field(default_factory=lambda: ["RF"])


# ---------------------------------------------------------------------------
# EO / tipping camera observations (Phase A — cross-sensor cueing)
# ---------------------------------------------------------------------------
#
# An EO sensor is a tipping electro-optical / IR camera that an RF detection
# can cue toward a sector. In a real deployment this would be a gimballed
# camera + a small object-detection model (YOLOv8n-class, ~6 MB int8). For
# this demo, the EO subsystem is a deterministic simulator that models the
# *behaviour* of a real tipping camera: gimbal slew latency, imperfect
# agreement with RF, occasional "no visual" (occlusion / fog / range), and
# occasional "contradiction" (RF says drone, EO sees a bird — possible
# spoofing / deception, or just sensor mismatch). The simulator is clearly
# labelled as such in the README and dashboard.

EOFrameKind = Literal[
    "quadcopter",         # Group-1 UAS, the most common adversary class
    "fixed_wing",         # commercial / aircraft / Group-3 UAS
    "bird",               # avian false alarm
    "person",             # personnel / dismount
    "no_visual",          # occluded, fog, out of range, gimbal-blocked
    "contradiction",      # visual disagrees with RF → possible deception
]


class EOObservation(BaseModel):
    """One frame of EO/IR observation triggered by an RF custody event.

    Mirrors what a real tipping-camera + visual-classifier pipeline would
    return: a bbox, a class label, a confidence, plus pose metadata
    (bearing, range estimate, sector) that an operator needs to decide.
    """

    id: str = Field(default_factory=lambda: new_id("obs_"))
    timestamp: datetime = Field(default_factory=utc_now)
    sensor_id: str = "EO-GIMBAL-01"
    site_id: str = "Alpha Site - Forward OP"
    track_id: str
    triggering_event_id: Optional[str] = None
    sector: str
    bearing_deg: float                    # 0-360, where camera is pointed
    slew_time_ms: int                     # how long the gimbal took to slew
    frame_kind: EOFrameKind
    classification: str                   # human-readable label for UI
    confidence: float                     # 0-1
    bbox: tuple[float, float, float, float]  # normalised x, y, w, h
    range_m_estimate: Optional[float] = None
    notes: str = ""
    # Whether this observation is considered to confirm the RF detection.
    # True when frame_kind is one of the recognised target classes
    # (quadcopter, fixed_wing, person) and confidence >= 0.6. False for
    # bird / no_visual / contradiction. Computed by the EO sensor.
    confirms_rf: bool = False


class UASTrack(BaseModel):
    """A tracked target (the 'object' the operator interacts with)."""

    track_id: str
    site_id: str = "Alpha Site - Forward OP"
    custody_state: CustodyState
    threat_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    classification: Literal["CONFIRMED_UAS", "POSSIBLE_UAS", "FALSE_ALARM", "UNKNOWN"] = "UNKNOWN"
    confidence: float
    sector: str = "NE"
    last_known_lat: float
    last_known_lon: float
    last_known_alt_m: float = 50.0
    n_detections: int = 1
    first_seen: datetime
    last_seen: datetime
    # Multi-modal custody bookkeeping (Phase A). Set when the EO sensor has
    # produced an observation for this track. `visual_confirmed` flips True
    # the first time EO returns a recognised target with confidence >= 0.6.
    last_eo_obs: Optional[EOObservation] = None
    visual_confirmed: bool = False
    # Timestamp of last EO observation that confirmed RF; used by the
    # custody manager to detect "visual went stale" → VISUAL_LOST_RF_PRESENT.
    last_eo_confirm_ts: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Emitter library + commands
# ---------------------------------------------------------------------------

class EmitterProfile(BaseModel):
    """Fingerprint of a known emitter, used by the attribution engine.

    The first four fields are the original "where in the spectrum does this
    sit and who operates it" description. The rest of the fields below are
    Phase E additions that let the attribution scorer match a single
    detection against the library with real waveform parameters, not just
    a coarse band + pattern string.

    Any of the optional fields can be ``None`` — the scorer gracefully
    down-weights comparisons when a detail is not known.
    """

    id: str
    name: str
    side: Literal["friendly", "unknown", "civilian", "adversary_simulated"]
    expected_freq_min_mhz: float
    expected_freq_max_mhz: float
    expected_pattern: str
    unit_id: Optional[str] = None
    site_id: Optional[str] = None

    # ----- Phase E additions: real waveform parameters -----
    # All optional so existing profiles keep working.
    nominal_bandwidth_mhz: Optional[float] = None
    modulation: Optional[
        Literal["FHSS", "DSSS", "OFDM", "GMSK", "FSK", "PSK", "AM", "FM", "CW", "pulsed", "analog_video", "unknown"]
    ] = None
    # Pulse-repetition interval in microseconds, for pulsed / radar-like
    # emitters. None for continuous emitters.
    pri_us: Optional[float] = None
    # Typical duty cycle in [0, 1]. 1.0 = always on, 0.05 = 5% on time.
    duty_cycle: Optional[float] = None
    # Named hop pattern signature. Matched as a literal string against the
    # detection's inferred hop pattern — we do not try to match hop
    # sequences in the demo; real systems would.
    hop_pattern: Optional[str] = None
    # Typical transmit power at 1 m, in dBm. Used to sanity-check RSS-based
    # range estimates, not strictly required.
    typical_power_dbm: Optional[float] = None
    # Operator / platform notes — e.g. "AN/PRC-117F man-pack radio carried
    # by SF teams". Free-form, shown in the attribution UI.
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase E — Blue-force feed + attribution + TDOA geolocation
# ---------------------------------------------------------------------------

class BlueForceUnit(BaseModel):
    """Simulated position of a friendly unit and the emitters it's operating.

    In real systems this feed comes from TAK / blue-force tracker / PLI over
    ADS-B or Iridium. Here we simulate a handful of units moving at walking
    or vehicle speed. The attribution engine uses this to "explain away" a
    friendly emission: if the closest matching library entry is a
    friendly radio *and* a blue unit operating that radio is within
    ``attribution_radius_m`` metres of the detection, the verdict is
    ``BLUE_ATTRIBUTED``.
    """

    unit_id: str
    callsign: str
    lat: float
    lon: float
    # Metres above ellipsoid; used for 3-D TDOA, safe default for flat
    # ground is 0.
    alt_m: float = 0.0
    # Emitter ids (EmitterProfile.id) the unit is *currently* operating.
    # A unit can hold multiple radios.
    active_emitters: list[str] = Field(default_factory=list)
    # Heading in degrees (true north = 0) and speed in m/s — used by the
    # sim to extrapolate position between updates.
    heading_deg: float = 0.0
    speed_mps: float = 0.0
    last_update: datetime = Field(default_factory=utc_now)


AttributionVerdict = Literal[
    "BLUE_ATTRIBUTED",   # best library match is friendly AND a blue unit is in range
    "RED_KNOWN",         # best match is adversary-simulated with score >= RED threshold
    "AMBIGUOUS",         # best match crosses threshold but affiliation is mixed / civilian
    "UNEXPLAINED",       # no library entry scores high enough
]


class AttributionResult(BaseModel):
    """One attribution decision for one detection.

    Produced by ``app.pipeline.attribution.attribute()`` and embedded in the
    ``ClassifiedSignal`` / ``IntelligenceEvent`` that triggered it. Keeps
    every number the scorer used so a judge / analyst can replay the
    decision end-to-end.
    """

    verdict: AttributionVerdict
    confidence: float                         # 0..1 — scorer's belief in the verdict
    # Best-matching library entry (by id) — populated even for UNEXPLAINED so
    # we can show "closest miss was emitter_dji_control_24 at 0.42".
    best_emitter_id: Optional[str] = None
    best_emitter_name: Optional[str] = None
    best_score: float = 0.0
    # If BLUE_ATTRIBUTED, the unit id / callsign that explained this emission.
    attributed_unit_id: Optional[str] = None
    attributed_unit_callsign: Optional[str] = None
    distance_to_attributed_unit_m: Optional[float] = None
    # Per-feature breakdown so the UI can show "freq ✓, bw ✓, mod ✗".
    feature_scores: dict[str, float] = Field(default_factory=dict)
    # Top N alternative matches for analyst review.
    runner_ups: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""                          # human-readable one-liner
    # Foreign keys back to the intelligence event + track that triggered
    # this attribution. Set by the pipeline at the moment of attribution
    # so the snapshot carries the link without relying on a side-table.
    event_id: Optional[str] = None
    track_id: Optional[str] = None


class SensorNode(BaseModel):
    """One RF sensor node used by the TDOA solver.

    A real deployment places three or more of these at surveyed positions
    with GPS-disciplined µs-accurate clocks. The TDOA solver needs the
    exact 3-D positions to form the hyperbolic equations.
    """

    id: str
    name: str
    lat: float
    lon: float
    alt_m: float = 0.0
    # Clock jitter (1-σ) in nanoseconds. Real GPSDO clocks are ~10 ns.
    clock_jitter_ns: float = 30.0
    status: Literal["online", "offline", "degraded"] = "online"


class TdoaSolution(BaseModel):
    """Result of running Chan's algorithm over a set of TDOA measurements.

    CEP (Circular Error Probable) is the radius of the 50%-confidence
    circle centred on ``(lat, lon)``. It is derived from the solver's
    covariance matrix and the geometric dilution of precision of the
    three sensors. Small values = tight fix; in a demo with 30 ns jitter
    and ~5 km sensor baselines we see ~20-60 m CEP — comparable to a
    well-configured real SIGINT array.
    """

    lat: float
    lon: float
    alt_m: float = 0.0
    cep_m: float                              # 50% error circle radius, metres
    residual_m: float                         # Fit residual — 0 = perfect
    sensor_ids: list[str]
    gdop: float                               # Geometric dilution of precision
    method: Literal["chan_1994", "taylor_series"] = "chan_1994"
    timestamp: datetime = Field(default_factory=utc_now)
    # 2x2 covariance (serialised as 4 floats row-major) for UI ellipse rendering.
    cov_xx: float = 0.0
    cov_xy: float = 0.0
    cov_yx: float = 0.0
    cov_yy: float = 0.0
    # Foreign keys + ground-truth (sim-only) so snapshot rows carry the
    # full provenance without a side-table lookup.
    event_id: Optional[str] = None
    track_id: Optional[str] = None
    truth_lat: Optional[float] = None
    truth_lon: Optional[float] = None


class PersistentEmitter(BaseModel):
    """Cluster of UNEXPLAINED detections flagged by the persistence detector.

    Emitted when 3+ unexplained detections of the same signal class appear
    within ``time_window_s`` seconds inside a ``spatial_window_m``-radius
    circle. This is the "I can't explain this one — get that guy" insight
    the mentor called out.
    """

    id: str
    first_seen: datetime
    last_seen: datetime
    n_detections: int
    lat: float                                # Cluster centroid
    lon: float
    radius_m: float                           # 95%ile distance from centroid
    signal_class: str                         # e.g. "unknown_digital_15ghz"
    detection_event_ids: list[str] = Field(default_factory=list)
    # Derived recommendation for the COA panel.
    recommended_action: str = "INVESTIGATE"
    priority: Literal["low", "medium", "high"] = "high"


CommandType = Literal[
    "INCREASE_SENSITIVITY",
    "REDUCE_SENSITIVITY",
    "WATCH_BAND",
    "MARK_FALSE_POSITIVE",
    "MARK_FRIENDLY",
    "REQUEST_VISUAL",
]


class EdgeCommand(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cmd_"))
    timestamp: datetime = Field(default_factory=utc_now)
    target_sensor_id: str = "EDGE-ALPHA-01"
    command_type: CommandType
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "delivered", "executed"] = "pending"
    issued_by: str = "operator"


# ---------------------------------------------------------------------------
# Operator action (what the human did)
# ---------------------------------------------------------------------------

OperatorActionType = Literal[
    "CONFIRM",
    "DISMISS",
    "ESCALATE",
    "INCREASE_SENSITIVITY",
    "GENERATE_REPORT",
    "REQUEST_VISUAL",
    "MARK_FRIENDLY",
]


class OperatorAction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("act_"))
    timestamp: datetime = Field(default_factory=utc_now)
    track_id: Optional[str] = None
    event_id: Optional[str] = None
    action_type: OperatorActionType
    # Matches the operator_id format in datfromfoundry/operator_actions.csv
    operator_id: str = "OPR-SGT-JONES"
    details: str = ""


# ---------------------------------------------------------------------------
# Edge device telemetry
# ---------------------------------------------------------------------------

class EdgeDeviceStatus(BaseModel):
    device_id: str = "EDGE-ALPHA-01"
    device_name: str = "Alpha Site - Forward OP"
    site_id: str = "Alpha Site - Forward OP"
    site_lat: float = 34.0522
    site_lon: float = -118.2437
    deployment_type: str = "FORWARD_OBSERVATION_POST"
    # Phase A added the simulated EO/IR tipping camera. ACOUSTIC remains a
    # planned modality for a future microphone array on the same edge node.
    sensors_equipped: list[str] = Field(default_factory=lambda: ["RF", "EO"])
    firmware_version: str = "1.4.2"
    network_status: Literal["CONNECTED", "DEGRADED", "DISCONNECTED"] = "CONNECTED"
    sensitivity_mode: Literal["normal", "high", "low"] = "normal"
    watch_band_mhz: Optional[tuple[float, float]] = None
    battery_pct: float = 87.0
    sync_queue_depth: int = 0
    active_tracks: int = 0
    total_readings_processed: int = 0
    total_filtered_local: int = 0
    total_events_synced: int = 0
    bytes_saved_at_edge: int = 0
    bytes_actually_synced: int = 0
    model_loaded: bool = False
    model_summary: Optional[dict] = None
    # Real-data injection telemetry (RadioML 2016.10A). When the dataset is
    # available on disk, the operator can dial real_data_mix from 0 to 1 to
    # blend genuine I/Q recordings into the live stream alongside synthetic
    # signals. Useful for proving the open-set OOD layer on real input.
    real_data_available: bool = False
    real_data_mix: float = 0.0


# ---------------------------------------------------------------------------
# WebSocket envelope
# ---------------------------------------------------------------------------

WSMessageType = Literal[
    "hello",
    "reading",
    "classified",
    "intelligence_event",
    "intelligence_event_update",
    "custody_log",
    "edge_status",
    "queue_update",
    "sync_complete",
    "command",
    "scenario_step",
    "llm_brief",
    "eo_observation",  # Phase A: tipping-camera observation cued by an RF custody open
    "cot_published",   # Phase B: a CoT XML message was published to ATAK / FreeTAK / TAK clients
    "coa_posture_changed",  # Phase C: sitewide ROE posture was updated
    "coa_executed",         # Phase C: operator committed to a specific COA option
    # Phase E: attribution, TDOA geolocation, persistence clustering, blue-force
    "attribution_result",
    "tdoa_fix",
    "persistent_emitter",
    "blue_force_update",
    "error",
]


class WSMessage(BaseModel):
    type: WSMessageType
    payload: dict[str, Any]
    ts: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# REST request bodies
# ---------------------------------------------------------------------------

class NetworkToggleReq(BaseModel):
    online: bool


class SensitivityReq(BaseModel):
    mode: Literal["normal", "high", "low"]


class WatchBandReq(BaseModel):
    freq_min_mhz: float
    freq_max_mhz: float
    duration_minutes: int = 5


class RealDataMixReq(BaseModel):
    """Set the fraction of free-run ticks pulled from real RadioML I/Q data."""

    mix: float = Field(0.0, ge=0.0, le=1.0)


class MarkFriendlyReq(BaseModel):
    track_id: str


class OperatorActionReq(BaseModel):
    track_id: Optional[str] = None
    event_id: Optional[str] = None
    action_type: OperatorActionType
    details: str = ""


class NLQueryReq(BaseModel):
    query: str


class ScenarioRunReq(BaseModel):
    scenario: Literal[
        "full_demo",
        "quick_anomaly",
        "drone_swarm",
        "cross_cue_demo",  # Phase A — RF \u2192 EO multi-modal cueing
        "persistent_unknown_demo",  # Phase E — DBSCAN persistence demo
    ] = "full_demo"


# ---------------------------------------------------------------------------
# CoT / ATAK (Phase B)
# ---------------------------------------------------------------------------

class CotPublishReq(BaseModel):
    """Request body for /api/intel/{event_id}/cot/publish.

    ``stale_seconds`` lets the operator override how long the contact
    sticks on the ATAK map before greying out. ``transport`` is reserved
    for a future Phase B3 FreeTAKServer UDP / TCP path; for the in-demo
    flow we always log + broadcast and let the dashboard render the XML.
    """

    stale_seconds: int = Field(60, ge=5, le=3600)
    transport: Literal["log", "broadcast", "udp"] = "broadcast"


# ---------------------------------------------------------------------------
# COA recommender (Phase C)
# ---------------------------------------------------------------------------

RoePosture = Literal[
    "HOLD_FIRE",       # observe / report only
    "WARNING_ONLY",    # + warnings + positive ID
    "DEFENSIVE",       # + non-kinetic (jam, hand-off)
    "WEAPONS_FREE",    # + kinetic on positive hostile ID
]


class CoaRecommendReq(BaseModel):
    """Ask the recommender for a ranked COA list for a track or event."""

    track_id: Optional[str] = None
    event_id: Optional[str] = None
    posture: Optional[RoePosture] = None  # if None, uses current STATE.roe_posture
    top_n: int = Field(4, ge=1, le=9)


class CoaPostureReq(BaseModel):
    """Update the sitewide ROE posture. Changes are broadcast on the WS."""

    posture: RoePosture


class CoaExecuteReq(BaseModel):
    """Operator commits to a specific COA option.

    The backend records the commitment as an OperatorAction and, where
    applicable, fires the matching side-effect (e.g. INCREASE_SENSITIVITY
    nudges the classifier; MARK_FRIENDLY updates the emitter library).
    """

    track_id: Optional[str] = None
    event_id: Optional[str] = None
    action_id: str                       # CoaActionId string
    posture: Optional[RoePosture] = None  # for audit; defaults to current
    notes: str = ""


class CotPublication(BaseModel):
    """One published CoT message — stored in a rolling buffer for replay.

    The dashboard's CoT panel renders ``cot_dict`` for human-readable
    fields and offers a copy-button on ``xml`` for an operator who wants
    to paste the wire bytes into a TAK ingest tool. The ``status`` field
    is set to ``broadcast`` whenever the WS broadcast went out (the
    fallback when no FreeTAKServer is running).
    """

    id: str = Field(default_factory=lambda: new_id("cot_"))
    timestamp: datetime = Field(default_factory=utc_now)
    event_id: str
    track_id: Optional[str] = None
    site_id: str = "Alpha Site - Forward OP"
    sensor_id: str = "EDGE-ALPHA-01"
    cot_uid: str
    cot_type: str         # e.g. a-h-A-M-H-Q
    sidc: str             # e.g. SHAPMHQ---
    callsign: str
    icon_name: str
    stale_seconds: int = 60
    xml: str              # full wire-format XML (UTF-8 string)
    cot_dict: dict[str, Any]  # structured form for the UI preview
    transport: Literal["log", "broadcast", "udp"] = "broadcast"
    status: Literal["published", "failed"] = "published"
    note: str = ""
