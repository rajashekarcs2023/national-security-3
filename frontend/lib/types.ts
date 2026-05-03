// TypeScript mirrors of the Pydantic schemas in backend/app/schemas.py.
// Keep these in sync when schemas change.

export type Priority = "low" | "medium" | "high" | "critical";
export type SyncStatus = "local_only" | "queued" | "synced" | "failed";
export type NetworkState = "online" | "offline";
export type CustodyState =
  | "DETECTED"
  | "TRACKING"
  | "VISUAL_LOST_RF_PRESENT"
  | "REACQUIRED"
  | "TRACK_LOST"
  | "CLEARED"
  | "DISMISSED";

export type EventType =
  | "RF_ANOMALY"
  | "FRIENDLY_EMISSION"
  | "POSSIBLE_UAS_ACTIVITY"
  | "PROFILE_MISMATCH"
  | "PERSISTENT_UNKNOWN";

// Provenance of a single tick: synthesized in our generator vs. drawn
// from the real DeepSig RadioML 2016.10A I/Q dataset. Surfaces in the UI
// as a SYNTH/REAL badge so judges can see real signals flowing live.
export type SignalSource = "synth" | "real";

export interface RealSampleMeta {
  modulation?: string;        // e.g. "GFSK"
  snr_db?: number;            // e.g. 8
  threat_label?: string;      // operator-language mapping rationale
}

export interface RFSignalReading {
  id: string;
  timestamp: string;
  sensor_id: string;
  site_id: string;
  lat: number;
  lon: number;
  center_frequency_mhz: number;
  bandwidth_khz: number;
  power_dbm: number;
  duration_ms: number;
  burst_pattern: string;
  dominant_freq_bin: number;
  energy: number;
  raw_source: string;
  spectrogram_u8: number[][] | null;
}

export interface ClassifiedSignal {
  id: string;
  reading_id: string;
  timestamp: string;
  predicted_class: string;
  confidence: number;
  embedding: number[];
  softmax: number[];
  nearest_known_class: string;
  distance_to_nearest_centroid: number;
  reconstruction_error: number;
  ood_score: number;
  baseline_deviation: number;
  is_anomaly: boolean;
  priority: Priority;
  action: "ignore" | "log" | "queue" | "sync";
  explanation: string;
}

export interface IntelligenceEvent {
  id: string;
  timestamp: string;
  site_id: string;
  sensor_id: string;
  track_id: string;
  event_type: EventType;
  title: string;
  summary: string;
  classification: string;
  confidence: number;
  ood_score: number;
  priority: Priority;
  evidence: string[];
  recommended_action: string;
  sync_status: SyncStatus;
  network_state_at_detection: NetworkState;
  payload_size_bytes: number;
  raw_size_estimate_bytes: number;
  llm_brief: string | null;
}

export interface CustodyStateLog {
  id: string;
  track_id: string;
  timestamp: string;
  previous_state: CustodyState | null;
  new_state: CustodyState;
  action_cue: string;
  evidence_summary: string;
  triggering_event_id: string | null;
}

// Phase A — EO/IR tipping-camera observation triggered by an RF custody open.
// Mirrors backend/app/schemas.py::EOObservation. The CrossSensorPanel renders
// these as camera frames with bbox overlays so the operator can verify the
// RF detection visually before authorising any action.
export type EOFrameKind =
  | "quadcopter"        // Group-1 UAS
  | "fixed_wing"        // commercial / aircraft / Group-3 UAS
  | "bird"              // avian false alarm
  | "person"            // dismount / personnel
  | "no_visual"         // gimbal-blocked / occluded / out of range
  | "contradiction";    // visual disagrees with RF → possible deception

export interface EOObservation {
  id: string;
  timestamp: string;
  sensor_id: string;
  site_id: string;
  track_id: string;
  triggering_event_id: string | null;
  sector: string;
  bearing_deg: number;
  slew_time_ms: number;
  frame_kind: EOFrameKind;
  classification: string;
  confidence: number;
  bbox: [number, number, number, number]; // normalised x, y, w, h
  range_m_estimate: number | null;
  notes: string;
  confirms_rf: boolean;
}

// Phase B — Cursor on Target (CoT) publication record. Mirrors
// backend/app/schemas.py::CotPublication. Whenever an operator clicks
// "Publish to ATAK" on an intelligence event, the backend builds a CoT
// 2.0 XML payload, stores a record here, and broadcasts it on the WS
// so every connected dashboard can render the publication in real time.
export interface CotPublication {
  id: string;
  timestamp: string;
  event_id: string;
  track_id: string | null;
  site_id: string;
  sensor_id: string;
  cot_uid: string;       // SPECTRUMCUSTODY.<sensor>.<track-or-event>
  cot_type: string;      // e.g. a-h-A-M-H-Q
  sidc: string;          // MIL-STD-2525C 15-char code, e.g. SHAPMHQ---
  callsign: string;
  icon_name: string;
  stale_seconds: number;
  xml: string;
  cot_dict: Record<string, unknown>;
  transport: "log" | "broadcast" | "udp";
  status: "published" | "failed";
  note: string;
}

// What /api/intel/{event_id}/cot returns: a *preview* of the wire bytes
// + structured form. Used by the CotPreviewModal before publishing.
export interface CotPreviewResponse {
  event_id: string;
  cot_dict: Record<string, any>;
  xml: string;
}

// ---------------------------------------------------------------------
// Phase C — COA recommender / ROE
// ---------------------------------------------------------------------

export type RoePosture =
  | "HOLD_FIRE"
  | "WARNING_ONLY"
  | "DEFENSIVE"
  | "WEAPONS_FREE";

export type CoaActionId =
  | "OBSERVE_AND_REPORT"
  | "INCREASE_SENSITIVITY"
  | "REACQUIRE_VISUAL"
  | "INVESTIGATE_AND_GEOLOCATE"
  | "WARN_AND_QUERY"
  | "HAND_OFF_INTERCEPTOR"
  | "JAM_RF"
  | "MARK_FRIENDLY"
  | "ENGAGE_KINETIC"
  | "DISMISS";

export interface CoaOption {
  action_id: CoaActionId;
  label: string;
  description: string;
  score: number;
  rationale: string;
  roe_citation: string;
  prerequisites: string[];
  prerequisites_met: boolean;
  expected_outcome: string;
  risk_level: "low" | "medium" | "high";
  reversible: boolean;
  estimated_time_seconds: number;
}

export interface CoaRecommendation {
  track_id: string | null;
  event_id: string | null;
  roe_posture: RoePosture;
  roe_description: string;
  threat_summary: string;
  options: CoaOption[];
  filtered_out: { action_id: string; label: string; reason: string }[];
  notes: string[];
}

export interface RoeOption {
  posture: RoePosture;
  description: string;
}

export interface RoeState {
  posture: RoePosture;
  description: string;
  options: RoeOption[];
}

export interface CoaDecision {
  id: string;
  timestamp: string;
  track_id: string | null;
  event_id: string | null;
  action_id: CoaActionId;
  posture: RoePosture;
  notes: string;
}

export interface UASTrack {
  track_id: string;
  site_id: string;
  custody_state: CustodyState;
  threat_level: "LOW" | "MEDIUM" | "HIGH";
  classification: string;
  confidence: number;
  sector: string;
  last_known_lat: number;
  last_known_lon: number;
  last_known_alt_m: number;
  n_detections: number;
  first_seen: string;
  last_seen: string;
  // Multi-modal custody bookkeeping (Phase A).
  last_eo_obs?: EOObservation | null;
  visual_confirmed?: boolean;
  last_eo_confirm_ts?: string | null;
}

export interface EdgeDeviceStatus {
  device_id: string;
  device_name: string;
  site_id: string;
  site_lat: number;
  site_lon: number;
  deployment_type: string;
  sensors_equipped: string[];
  firmware_version: string;
  network_status: "CONNECTED" | "DEGRADED" | "DISCONNECTED";
  sensitivity_mode: "normal" | "high" | "low";
  watch_band_mhz: [number, number] | null;
  battery_pct: number;
  sync_queue_depth: number;
  active_tracks: number;
  total_readings_processed: number;
  total_filtered_local: number;
  total_events_synced: number;
  bytes_saved_at_edge: number;
  bytes_actually_synced: number;
  model_loaded: boolean;
  model_summary: ModelSummary | null;
  real_data_available: boolean;
  real_data_mix: number;
}

export interface ModelSummary {
  loaded: boolean;
  classes?: string[];
  val_acc?: number;
  // Field names mirror backend/app/ml/model.py::parameter_summary().
  total_params?: number;
  size_fp32_bytes?: number;
  size_int8_bytes?: number;
  embed_dim?: number;
  num_classes?: number;
}

export interface EmitterProfile {
  id: string;
  name: string;
  side: "friendly" | "unknown" | "civilian" | "adversary_simulated";
  expected_freq_min_mhz: number;
  expected_freq_max_mhz: number;
  expected_pattern: string;
  unit_id: string | null;
  site_id: string | null;
  // Phase E waveform parameters (all optional).
  nominal_bandwidth_mhz?: number | null;
  modulation?: string | null;
  pri_us?: number | null;
  duty_cycle?: number | null;
  hop_pattern?: string | null;
  typical_power_dbm?: number | null;
  notes?: string | null;
}

// ----------------------------------------------------------------------
// Phase E — Blue-force feed, attribution, TDOA geolocation, persistence
// ----------------------------------------------------------------------

export interface BlueForceUnit {
  unit_id: string;
  callsign: string;
  lat: number;
  lon: number;
  alt_m: number;
  active_emitters: string[];
  heading_deg: number;
  speed_mps: number;
  last_update: string;
}

export type AttributionVerdict =
  | "BLUE_ATTRIBUTED"
  | "RED_KNOWN"
  | "AMBIGUOUS"
  | "UNEXPLAINED";

export interface AttributionFeatureScores {
  freq?: number;
  bw?: number;
  pattern?: number;
  class?: number;
}

export interface AttributionRunnerUp {
  emitter_id: string;
  name: string;
  side: string;
  score: number;
  feature_scores?: AttributionFeatureScores;
}

export interface AttributionResult {
  verdict: AttributionVerdict;
  confidence: number;
  best_emitter_id?: string | null;
  best_emitter_name?: string | null;
  best_score: number;
  attributed_unit_id?: string | null;
  attributed_unit_callsign?: string | null;
  distance_to_attributed_unit_m?: number | null;
  feature_scores: AttributionFeatureScores;
  runner_ups: AttributionRunnerUp[];
  reason: string;
  // Pipeline-side annotations injected into the WS message:
  event_id?: string;
  track_id?: string | null;
  counters?: AttributionCounters;
}

export interface AttributionCounters {
  blue_attributed: number;
  red_known: number;
  unexplained: number;
  ambiguous: number;
}

export interface SensorNode {
  id: string;
  name: string;
  lat: number;
  lon: number;
  alt_m: number;
  clock_jitter_ns: number;
  status: "online" | "offline" | "degraded";
}

export interface TdoaSolution {
  lat: number;
  lon: number;
  alt_m: number;
  cep_m: number;
  residual_m: number;
  sensor_ids: string[];
  gdop: number;
  method: "chan_1994" | "taylor_series";
  timestamp: string;
  cov_xx: number;
  cov_xy: number;
  cov_yx: number;
  cov_yy: number;
  // Pipeline-side annotations:
  event_id?: string;
  track_id?: string | null;
  truth_lat?: number;
  truth_lon?: number;
}

export interface PersistentEmitter {
  id: string;
  first_seen: string;
  last_seen: string;
  n_detections: number;
  lat: number;
  lon: number;
  radius_m: number;
  signal_class: string;
  detection_event_ids: string[];
  recommended_action: string;
  priority: "low" | "medium" | "high";
}

export interface FoundryPushMetrics {
  success_count: Record<string, number>;
  error_count: Record<string, number>;
  last_success_ts: Record<string, number | null>;
  last_error: Record<string, string | null>;
}

// Phase F — real Palantir Foundry remote-tenant transport state. Surfaced
// alongside the local-sink push metrics so the dashboard can show "LIVE →
// stack URL" vs "local mirror only".
export interface FoundryRemoteStreamStat {
  configured: boolean;
  pushed: number;
  queued: number;
  failed: number;
  bytes_sent: number;
  last_success_ts: number | null;
  last_attempt_ts: number | null;
  last_error: string | null;
}

export interface FoundryRemoteState {
  enabled: boolean;
  online: boolean;
  stack_url: string | null;
  configured_streams: string[];
  missing_streams: string[];
  streams: Record<string, FoundryRemoteStreamStat>;
  ddil_buffer_dir: string | null;
  last_flush_ts: number | null;
}

export interface FoundrySyncState {
  transport: "in_process" | "http";
  push_metrics: FoundryPushMetrics;
  remote?: FoundryRemoteState;
}

export interface QueueSummary {
  depth: number;
  by_priority?: Record<string, number>;
  oldest_ts?: string;
}

export interface BaselineSummary {
  n_observed: number;
  anomaly_rate: number;
  mean_power_dbm: number | null;
  dominant_band_histogram: number[];
}

export interface ScenarioStep {
  phase: string;
  text?: string;
  name?: string;
  description?: string;
  online?: boolean;
  mode?: string;
  note?: string | null;
}

export type WSMessage =
  | { type: "hello"; payload: StateSnapshot; ts: string }
  | { type: "classified"; payload: ClassifiedPayload; ts: string }
  | { type: "intelligence_event"; payload: IntelligenceEvent; ts: string }
  | { type: "intelligence_event_update"; payload: { event_id: string; llm_brief?: string; sync_status?: SyncStatus }; ts: string }
  | { type: "custody_log"; payload: CustodyStateLog; ts: string }
  | { type: "track_update"; payload: UASTrack; ts: string }
  | { type: "edge_status"; payload: EdgeDeviceStatus; ts: string }
  | { type: "queue_update"; payload: { depth: number; summary: QueueSummary }; ts: string }
  | { type: "sync_complete"; payload: { n_synced: number; events: IntelligenceEvent[] }; ts: string }
  | { type: "command"; payload: Record<string, unknown>; ts: string }
  | { type: "operator_action"; payload: Record<string, unknown>; ts: string }
  | { type: "scenario_step"; payload: ScenarioStep; ts: string }
  | { type: "llm_brief"; payload: { brief: string }; ts: string }
  | { type: "eo_observation"; payload: EOObservation; ts: string }
  | { type: "cot_published"; payload: CotPublication; ts: string }
  | {
      type: "coa_posture_changed";
      payload: { previous: RoePosture; posture: RoePosture; description: string };
      ts: string;
    }
  | {
      type: "coa_executed";
      payload: {
        decision: CoaDecision;
        side_effect: { action: string; executed: boolean; effect?: string };
      };
      ts: string;
    }
  | { type: "attribution_result"; payload: AttributionResult; ts: string }
  | { type: "tdoa_fix"; payload: TdoaSolution; ts: string }
  | { type: "persistent_emitter"; payload: PersistentEmitter; ts: string }
  | { type: "blue_force_update"; payload: { units: BlueForceUnit[] }; ts: string }
  | { type: "error"; payload: { message: string }; ts: string };

export interface ClassifiedPayload {
  reading: RFSignalReading;
  classified: ClassifiedSignal;
  scenario_note?: string | null;
  true_class?: string;
  source?: SignalSource;
  source_meta?: RealSampleMeta;
}

export interface StateSnapshot {
  device: EdgeDeviceStatus;
  baseline: BaselineSummary;
  tracks: UASTrack[];
  recent_classifications: ClassifiedSignal[];
  intelligence_events: IntelligenceEvent[];
  custody_logs: CustodyStateLog[];
  eo_observations?: EOObservation[];
  cot_publications?: CotPublication[];
  roe?: RoeState;
  coa_decisions?: CoaDecision[];
  queue: { depth: number; items: IntelligenceEvent[] };
  scenario_active: boolean;
  emitter_library: EmitterProfile[];
  available_scenarios: { name: string; description: string }[];
  raw_class_weights: Record<string, number>;
  // Phase E
  sensor_array?: SensorNode[];
  blue_force?: BlueForceUnit[];
  attribution_recent?: AttributionResult[];
  tdoa_recent?: TdoaSolution[];
  persistent_emitters?: PersistentEmitter[];
  attribution_counters?: AttributionCounters;
  foundry_sync?: FoundrySyncState;
}
