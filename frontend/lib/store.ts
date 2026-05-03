"use client";

import { create } from "zustand";
import type {
  AttributionCounters,
  AttributionResult,
  BlueForceUnit,
  ClassifiedPayload,
  ClassifiedSignal,
  CoaDecision,
  CotPublication,
  CustodyStateLog,
  EdgeDeviceStatus,
  EOObservation,
  FoundrySyncState,
  IntelligenceEvent,
  PersistentEmitter,
  QueueSummary,
  RFSignalReading,
  RealSampleMeta,
  RoePosture,
  ScenarioStep,
  SensorNode,
  SignalSource,
  StateSnapshot,
  SyncStatus,
  TdoaSolution,
  UASTrack,
} from "./types";

const MAX_EVENTS = 200;
const MAX_CLASSIFIED = 200;
const MAX_CUSTODY = 200;
const MAX_SCENARIO = 40;

interface ClassifiedTick {
  reading: RFSignalReading;
  classified: ClassifiedSignal;
  true_class?: string;
  scenario_note?: string | null;
  source?: SignalSource;
  source_meta?: RealSampleMeta;
}

interface StoreState {
  // WS connectivity to backend
  wsConnected: boolean;
  setWsConnected: (v: boolean) => void;

  // Device + model
  device: EdgeDeviceStatus | null;
  baselineSummary: { n_observed: number; anomaly_rate: number; mean_power_dbm: number | null; dominant_band_histogram: number[] } | null;

  // Live data
  recentTicks: ClassifiedTick[];
  currentTick: ClassifiedTick | null;
  events: IntelligenceEvent[];
  custodyLogs: CustodyStateLog[];
  tracks: UASTrack[];
  // Latest EO observation per track_id (Phase A cross-sensor fusion).
  // Keyed map keeps lookups O(1) for the CrossSensorPanel and avoids the
  // panel having to scan a long event list to find the freshest frame.
  eoObservations: Record<string, EOObservation>;
  // Phase B — rolling list of CoT publications, freshest first. Capped
  // client-side to mirror the backend's 100-item buffer; the dashboard
  // panel only ever shows the top 20 anyway.
  cotPublications: CotPublication[];
  // Phase C — sitewide ROE posture + decision audit log. The panel
  // re-fetches recommendations whenever posture changes.
  roePosture: RoePosture;
  coaDecisions: CoaDecision[];

  // Phase E — attribution, TDOA, persistence, blue-force, Foundry sync.
  // Each AttributionResult is keyed by event_id so the IntelligenceEvents
  // panel can colour cards by verdict in O(1).
  attributionByEvent: Record<string, AttributionResult>;
  // Latest TDOA fix per track_id. Used by the map for CEP circles.
  tdoaByTrack: Record<string, TdoaSolution>;
  // Latest TDOA fix per event_id (covers events that didn't open a track).
  tdoaByEvent: Record<string, TdoaSolution>;
  attributionRecent: AttributionResult[];
  tdoaRecent: TdoaSolution[];
  attributionCounters: AttributionCounters;
  persistentEmitters: Record<string, PersistentEmitter>;
  blueForce: BlueForceUnit[];
  sensorArray: SensorNode[];
  foundrySync: FoundrySyncState | null;

  // Queue
  queueDepth: number;
  queueSummary: QueueSummary | null;

  // Scenario
  scenarioSteps: ScenarioStep[];
  scenarioActive: boolean;

  // LLM
  lastBrief: string | null;

  // --- actions ---
  ingest: (msg: { type: string; payload: any }) => void;
  setSnapshot: (s: StateSnapshot) => void;
  setCurrentTick: (t: ClassifiedTick) => void;
  pushEvent: (e: IntelligenceEvent) => void;
  updateEvent: (id: string, patch: Partial<IntelligenceEvent>) => void;
  pushCustody: (c: CustodyStateLog) => void;
  pushTrack: (t: UASTrack) => void;
  pushEoObservation: (o: EOObservation) => void;
  pushCotPublication: (p: CotPublication) => void;
  setRoePosture: (p: RoePosture) => void;
  pushCoaDecision: (d: CoaDecision) => void;
  // Phase E actions
  pushAttribution: (a: AttributionResult) => void;
  pushTdoaFix: (t: TdoaSolution) => void;
  pushPersistentEmitter: (p: PersistentEmitter) => void;
  setBlueForce: (units: BlueForceUnit[]) => void;
  setDevice: (d: EdgeDeviceStatus) => void;
  setQueue: (q: { depth: number; summary: QueueSummary }) => void;
  pushScenarioStep: (s: ScenarioStep) => void;
  setLastBrief: (b: string) => void;
}

export const useStore = create<StoreState>((set, get) => ({
  wsConnected: false,
  setWsConnected: (v) => set({ wsConnected: v }),

  device: null,
  baselineSummary: null,

  recentTicks: [],
  currentTick: null,
  events: [],
  custodyLogs: [],
  tracks: [],
  eoObservations: {},
  cotPublications: [],
  roePosture: "DEFENSIVE",
  coaDecisions: [],

  // Phase E
  attributionByEvent: {},
  tdoaByTrack: {},
  tdoaByEvent: {},
  attributionRecent: [],
  tdoaRecent: [],
  attributionCounters: { blue_attributed: 0, red_known: 0, unexplained: 0, ambiguous: 0 },
  persistentEmitters: {},
  blueForce: [],
  sensorArray: [],
  foundrySync: null,

  queueDepth: 0,
  queueSummary: null,

  scenarioSteps: [],
  scenarioActive: false,

  lastBrief: null,

  // ---------- Generic ingest (from WS) ----------
  ingest: (msg) => {
    const { type, payload } = msg as any;
    switch (type) {
      case "hello":
        get().setSnapshot(payload as StateSnapshot);
        break;
      case "classified":
        get().setCurrentTick(payload as ClassifiedPayload);
        break;
      case "intelligence_event":
        get().pushEvent(payload as IntelligenceEvent);
        break;
      case "intelligence_event_update":
        get().updateEvent(payload.event_id, {
          llm_brief: payload.llm_brief,
          sync_status: payload.sync_status as SyncStatus,
        });
        break;
      case "custody_log":
        get().pushCustody(payload as CustodyStateLog);
        break;
      case "track_update":
        get().pushTrack(payload as UASTrack);
        break;
      case "edge_status":
        get().setDevice(payload as EdgeDeviceStatus);
        break;
      case "queue_update":
        get().setQueue(payload);
        break;
      case "sync_complete":
        // Mark all listed events synced
        const { events } = payload as { events: IntelligenceEvent[] };
        events.forEach((e) => get().updateEvent(e.id, { sync_status: "synced" }));
        break;
      case "scenario_step":
        get().pushScenarioStep(payload as ScenarioStep);
        if ((payload as ScenarioStep).phase === "start") {
          set({ scenarioActive: true });
        } else if ((payload as ScenarioStep).phase === "done") {
          set({ scenarioActive: false });
        }
        break;
      case "llm_brief":
        get().setLastBrief(payload.brief);
        break;
      case "eo_observation":
        get().pushEoObservation(payload as EOObservation);
        break;
      case "cot_published":
        get().pushCotPublication(payload as CotPublication);
        break;
      case "coa_posture_changed":
        get().setRoePosture((payload as any).posture as RoePosture);
        break;
      case "coa_executed":
        get().pushCoaDecision((payload as any).decision as CoaDecision);
        break;
      case "attribution_result":
        get().pushAttribution(payload as AttributionResult);
        break;
      case "tdoa_fix":
        get().pushTdoaFix(payload as TdoaSolution);
        break;
      case "persistent_emitter":
        get().pushPersistentEmitter(payload as PersistentEmitter);
        break;
      case "blue_force_update":
        get().setBlueForce((payload as { units: BlueForceUnit[] }).units);
        break;
    }
  },

  // ---------- Hydrate from snapshot ----------
  setSnapshot: (s) => {
    // Build the latest-per-track EO map from the snapshot's history. The
    // backend returns observations in chronological order, so a simple
    // forward iteration ends with the freshest observation winning.
    const eoMap: Record<string, EOObservation> = {};
    (s.eo_observations ?? []).forEach((o) => {
      eoMap[o.track_id] = o;
    });
    // Phase E — hydrate attribution + tdoa indexes from the snapshot.
    const attributionRecent = [...(s.attribution_recent ?? [])].reverse();
    const tdoaRecent = [...(s.tdoa_recent ?? [])].reverse();
    const attributionByEvent: Record<string, AttributionResult> = {};
    attributionRecent.forEach((a) => {
      if (a.event_id) attributionByEvent[a.event_id] = a;
    });
    const tdoaByEvent: Record<string, TdoaSolution> = {};
    const tdoaByTrack: Record<string, TdoaSolution> = {};
    tdoaRecent.forEach((t) => {
      if (t.event_id) tdoaByEvent[t.event_id] = t;
      if (t.track_id) tdoaByTrack[t.track_id] = t;
    });
    const persistentEmitters: Record<string, PersistentEmitter> = {};
    (s.persistent_emitters ?? []).forEach((p) => {
      persistentEmitters[p.id] = p;
    });

    set({
      device: s.device,
      baselineSummary: s.baseline as any,
      tracks: s.tracks,
      events: [...s.intelligence_events].reverse(),
      custodyLogs: [...s.custody_logs].reverse(),
      eoObservations: eoMap,
      // Backend ships oldest-first, dashboard wants newest-first.
      cotPublications: [...(s.cot_publications ?? [])].reverse(),
      roePosture: (s.roe?.posture as RoePosture) ?? "DEFENSIVE",
      coaDecisions: [...(s.coa_decisions ?? [])].reverse(),
      // Phase E
      attributionByEvent,
      tdoaByEvent,
      tdoaByTrack,
      attributionRecent,
      tdoaRecent,
      attributionCounters: s.attribution_counters ?? {
        blue_attributed: 0,
        red_known: 0,
        unexplained: 0,
        ambiguous: 0,
      },
      persistentEmitters,
      blueForce: s.blue_force ?? [],
      sensorArray: s.sensor_array ?? [],
      foundrySync: s.foundry_sync ?? null,
      queueDepth: s.queue.depth,
      scenarioActive: s.scenario_active,
      scenarioSteps: [],
    });
  },

  setCurrentTick: (t) => {
    const ticks = [t, ...get().recentTicks].slice(0, MAX_CLASSIFIED);
    set({ currentTick: t, recentTicks: ticks });
  },

  pushEvent: (e) => {
    // Upsert
    const existing = get().events.findIndex((x) => x.id === e.id);
    const next =
      existing >= 0
        ? [...get().events.slice(0, existing), e, ...get().events.slice(existing + 1)]
        : [e, ...get().events].slice(0, MAX_EVENTS);
    set({ events: next });
  },

  updateEvent: (id, patch) => {
    set({
      events: get().events.map((e) => (e.id === id ? { ...e, ...patch } : e)),
    });
  },

  pushCustody: (c) => {
    set({ custodyLogs: [c, ...get().custodyLogs].slice(0, MAX_CUSTODY) });
  },

  pushTrack: (t) => {
    const i = get().tracks.findIndex((x) => x.track_id === t.track_id);
    const next =
      i >= 0
        ? [...get().tracks.slice(0, i), t, ...get().tracks.slice(i + 1)]
        : [t, ...get().tracks];
    set({ tracks: next });
  },

  pushEoObservation: (o) => {
    // Replace any prior frame for this track — the panel always shows the
    // freshest visual. Older frames are not retained client-side because
    // the operator decision lives in the present.
    set({ eoObservations: { ...get().eoObservations, [o.track_id]: o } });
  },

  pushCotPublication: (p) => {
    // Cap at 50 client-side. The backend keeps 100 in its buffer.
    set({ cotPublications: [p, ...get().cotPublications].slice(0, 50) });
  },

  setRoePosture: (p) => set({ roePosture: p }),

  pushCoaDecision: (d) => {
    set({ coaDecisions: [d, ...get().coaDecisions].slice(0, 50) });
  },

  pushAttribution: (a) => {
    const eventId = a.event_id;
    const next = { ...get().attributionByEvent };
    if (eventId) next[eventId] = a;
    set({
      attributionByEvent: next,
      attributionRecent: [a, ...get().attributionRecent].slice(0, 60),
      // Counters arrive embedded in the WS message; if they're missing keep
      // whatever the previous tick set.
      attributionCounters: a.counters ?? get().attributionCounters,
    });
  },

  pushTdoaFix: (t) => {
    const nextByEvent = { ...get().tdoaByEvent };
    if (t.event_id) nextByEvent[t.event_id] = t;
    const nextByTrack = { ...get().tdoaByTrack };
    if (t.track_id) nextByTrack[t.track_id] = t;
    set({
      tdoaByEvent: nextByEvent,
      tdoaByTrack: nextByTrack,
      tdoaRecent: [t, ...get().tdoaRecent].slice(0, 60),
    });
  },

  pushPersistentEmitter: (p) => {
    set({ persistentEmitters: { ...get().persistentEmitters, [p.id]: p } });
  },

  setBlueForce: (units) => set({ blueForce: units }),

  setDevice: (d) => set({ device: d }),

  setQueue: (q) => set({ queueDepth: q.depth, queueSummary: q.summary }),

  pushScenarioStep: (s) => {
    set({ scenarioSteps: [s, ...get().scenarioSteps].slice(0, MAX_SCENARIO) });
  },

  setLastBrief: (b) => set({ lastBrief: b }),
}));
