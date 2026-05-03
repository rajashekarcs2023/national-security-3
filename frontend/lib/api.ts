"use client";

// The backend runs on 8765. We call it directly from the browser — CORS is
// wide-open in the FastAPI app. In production we'd use a reverse proxy.
const BASE = "http://127.0.0.1:8765";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

export const api = {
  health: () => call<any>("/api/health"),
  state: () => call<any>("/api/state"),

  toggleNetwork: (online: boolean) =>
    call<any>("/api/network/toggle", {
      method: "POST",
      body: JSON.stringify({ online }),
    }),

  setSensitivity: (mode: "normal" | "high" | "low") =>
    call<any>("/api/command/sensitivity", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),

  watchBand: (freq_min_mhz: number, freq_max_mhz: number, duration_minutes = 5) =>
    call<any>("/api/command/watch_band", {
      method: "POST",
      body: JSON.stringify({ freq_min_mhz, freq_max_mhz, duration_minutes }),
    }),

  // Dial the fraction of free-run ticks pulled from real RadioML I/Q data.
  // Backend silently ignores values > 0 if the dataset isn't on disk.
  setRealDataMix: (mix: number) =>
    call<{ real_data_mix: number; real_data_available: boolean }>(
      "/api/command/real_data_mix",
      {
        method: "POST",
        body: JSON.stringify({ mix: Math.max(0, Math.min(1, mix)) }),
      }
    ),

  operatorAction: (req: {
    track_id?: string | null;
    event_id?: string | null;
    action_type:
      | "CONFIRM"
      | "DISMISS"
      | "ESCALATE"
      | "INCREASE_SENSITIVITY"
      | "GENERATE_REPORT"
      | "REQUEST_VISUAL"
      | "MARK_FRIENDLY";
    details?: string;
  }) =>
    call<any>("/api/operator/action", {
      method: "POST",
      body: JSON.stringify(req),
    }),

  scenarioRun: (
    scenario:
      | "full_demo"
      | "quick_anomaly"
      | "drone_swarm"
      | "cross_cue_demo"
      | "persistent_unknown_demo" = "full_demo",
  ) =>
    call<any>("/api/scenario/run", {
      method: "POST",
      body: JSON.stringify({ scenario }),
    }),
  scenarioStop: () => call<any>("/api/scenario/stop", { method: "POST" }),

  // Phase B — Cursor on Target (CoT) preview / publish.
  // Preview returns the wire-format XML and structured dict without
  // logging or broadcasting; publish persists the message and fires a
  // `cot_published` WS event to every dashboard subscriber.
  cotPreview: (eventId: string, staleSeconds = 60) =>
    call<{
      event_id: string;
      cot_dict: Record<string, any>;
      xml: string;
    }>(`/api/intel/${encodeURIComponent(eventId)}/cot?stale_seconds=${staleSeconds}`),

  cotPublish: (
    eventId: string,
    body: { stale_seconds?: number; transport?: "log" | "broadcast" | "udp" } = {},
  ) =>
    call<any>(`/api/intel/${encodeURIComponent(eventId)}/cot/publish`, {
      method: "POST",
      body: JSON.stringify({
        stale_seconds: body.stale_seconds ?? 60,
        transport: body.transport ?? "broadcast",
      }),
    }),

  cotList: (limit = 30) => call<any>(`/api/cot?limit=${limit}`),

  // Phase C — COA recommender + ROE posture.
  coaGetPosture: () =>
    call<{
      posture: import("./types").RoePosture;
      description: string;
      options: { posture: import("./types").RoePosture; description: string }[];
    }>("/api/coa/posture"),

  coaSetPosture: (posture: import("./types").RoePosture) =>
    call<any>("/api/coa/posture", {
      method: "POST",
      body: JSON.stringify({ posture }),
    }),

  coaRecommend: (body: {
    track_id?: string | null;
    event_id?: string | null;
    posture?: import("./types").RoePosture | null;
    top_n?: number;
  }) =>
    call<import("./types").CoaRecommendation>("/api/coa/recommend", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  coaExecute: (body: {
    track_id?: string | null;
    event_id?: string | null;
    action_id: import("./types").CoaActionId;
    posture?: import("./types").RoePosture | null;
    notes?: string;
  }) =>
    call<{
      decision: import("./types").CoaDecision;
      side_effect: { action: string; executed: boolean; effect?: string };
    }>("/api/coa/execute", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  coaDecisions: (limit = 30) =>
    call<{ count: number; items: import("./types").CoaDecision[] }>(
      `/api/coa/decisions?limit=${limit}`,
    ),

  llmBrief: () => call<{ brief: string; source: string }>("/api/llm/brief", { method: "POST" }),
  llmQuery: (query: string) =>
    call<{ query: string; filter: Record<string, unknown>; source: string }>(
      "/api/llm/query",
      { method: "POST", body: JSON.stringify({ query }) }
    ),

  foundryExportUrl: `${BASE}/api/exports/foundry`,

  // Phase E — embedded Foundry-compatible sink. Lives at /foundry on the
  // same backend; the frontend hits it directly via this BASE URL.
  foundrySinkStats: () =>
    call<{
      counts: Record<string, number>;
      bytes: Record<string, number>;
      last_received_ts: Record<string, number | null>;
      total_objects: number;
      total_bytes: number;
    }>("/foundry/stats"),

  foundrySinkRead: (type_: string, limit = 50) =>
    call<{ type: string; n: number; objects: Record<string, unknown>[] }>(
      `/foundry/objects/${type_}?limit=${limit}`,
    ),

  // Phase F — real Palantir Foundry remote-tenant transport.
  foundryRemoteStatus: () => call<unknown>("/api/foundry/remote/status"),
  foundryRemoteReplay: () =>
    call<{ drained: Record<string, number>; snapshot: unknown }>(
      "/api/foundry/remote/replay",
      { method: "POST" },
    ),
};
