"use client";

import clsx from "clsx";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import type { FoundryRemoteState } from "@/lib/types";

// Phase E + F — Foundry sync indicator.
//
// Two stacked sections:
//   1. Local sink — always works, drives the demo even with no creds.
//   2. Real Palantir Foundry tenant — lights up when FOUNDRY_STACK_URL +
//      FOUNDRY_TOKEN + FOUNDRY_STREAM_RIDS are configured. Shows live
//      pushed / queued / failed counters + DDIL buffer depth + last error.
//
// The header badge reflects the *most informative* state: LIVE if remote
// is healthy, ONLINE if the local sink is healthy, STANDBY otherwise.

interface SinkStats {
  counts: Record<string, number>;
  bytes: Record<string, number>;
  last_received_ts: Record<string, number | null>;
  total_objects: number;
  total_bytes: number;
}

const TYPES: Array<[string, string]> = [
  ["intelligence_event", "Events"],
  ["attribution", "Attributions"],
  ["tdoa_fix", "TDOA fixes"],
  ["persistent_emitter", "Persistents"],
  ["sensor_node", "Sensors"],
  ["emitter_profile", "Emitter library"],
  ["blue_force_unit", "Blue force"],
];

export default function FoundryStatus() {
  const foundrySync = useStore((s) => s.foundrySync);
  const [sinkStats, setSinkStats] = useState<SinkStats | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [replayResult, setReplayResult] = useState<string | null>(null);

  // Poll the sink's stats endpoint so we can show received-side counts in
  // addition to the edge-side push counts. Cheap (single GET, JSON).
  useEffect(() => {
    let alive = true;
    const fetchStats = async () => {
      try {
        const data = await api.foundrySinkStats();
        if (alive) setSinkStats(data as SinkStats);
      } catch {
        // Sink might be down — keep last good stats.
      }
    };
    fetchStats();
    const id = setInterval(fetchStats, 4000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const transport = foundrySync?.transport ?? "in_process";
  const remote: FoundryRemoteState | undefined = foundrySync?.remote;

  const totalPushed = foundrySync
    ? Object.values(foundrySync.push_metrics.success_count).reduce(
        (a, b) => a + b,
        0,
      )
    : 0;
  const totalErrors = foundrySync
    ? Object.values(foundrySync.push_metrics.error_count).reduce(
        (a, b) => a + b,
        0,
      )
    : 0;
  const lastSuccessSecs = foundrySync
    ? lastSuccessAge(foundrySync.push_metrics.last_success_ts)
    : null;
  const localHealthy =
    lastSuccessSecs !== null && lastSuccessSecs < 30 && totalErrors === 0;

  const remoteEnabled = !!remote?.enabled;
  const remoteOnline = !!remote?.online;
  const remoteStreamCount = remote?.configured_streams.length ?? 0;
  const remoteQueued = remote
    ? Object.values(remote.streams).reduce((a, s) => a + (s?.queued ?? 0), 0)
    : 0;
  const remotePushed = remote
    ? Object.values(remote.streams).reduce((a, s) => a + (s?.pushed ?? 0), 0)
    : 0;
  const remoteFailed = remote
    ? Object.values(remote.streams).reduce((a, s) => a + (s?.failed ?? 0), 0)
    : 0;
  const remoteBytes = remote
    ? Object.values(remote.streams).reduce(
        (a, s) => a + (s?.bytes_sent ?? 0),
        0,
      )
    : 0;
  const remoteLastError = remote
    ? Object.values(remote.streams)
        .map((s) => s?.last_error)
        .filter((e): e is string => !!e)
        .slice(-1)[0] ?? null
    : null;

  // Header status priority: LIVE (remote healthy) > ONLINE (local healthy) > STANDBY.
  let headerLabel = "STANDBY";
  let headerClass =
    "bg-amber-500/15 text-amber-300 border border-amber-500/30";
  if (remoteEnabled && remoteOnline) {
    headerLabel = "LIVE";
    headerClass =
      "bg-cyan-500/15 text-cyan-300 border border-cyan-500/30";
  } else if (localHealthy) {
    headerLabel = "ONLINE";
    headerClass =
      "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30";
  }

  const handleReplay = async () => {
    setReplaying(true);
    setReplayResult(null);
    try {
      const r = await api.foundryRemoteReplay();
      const total = Object.values(r.drained).reduce((a, b) => a + b, 0);
      setReplayResult(`drained ${total}`);
    } catch (e) {
      setReplayResult(`error: ${(e as Error).message}`);
    } finally {
      setReplaying(false);
    }
  };

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">
          Foundry sync
        </h2>
        <span
          className={clsx(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
            headerClass,
          )}
        >
          {headerLabel}
        </span>
      </div>

      {/* ─── Real Palantir Foundry tenant (Phase F) ──────────────── */}
      <div className="border-b border-panel-700 p-3 text-[12px]">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider text-slate-500">
            Palantir Foundry tenant
          </span>
          <span
            className={clsx(
              "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
              remoteEnabled
                ? remoteOnline
                  ? "bg-cyan-500/15 text-cyan-300 border border-cyan-500/30"
                  : "bg-rose-500/15 text-rose-300 border border-rose-500/30"
                : "bg-slate-500/15 text-slate-400 border border-slate-500/30",
            )}
          >
            {remoteEnabled
              ? remoteOnline
                ? "LIVE"
                : "OFFLINE"
              : "NOT CONFIGURED"}
          </span>
        </div>

        {remoteEnabled ? (
          <>
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Stack</span>
              <span
                className="font-mono text-[11px] text-slate-200 truncate max-w-[60%]"
                title={remote?.stack_url ?? ""}
              >
                {remote?.stack_url ?? "—"}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Streams configured</span>
              <span className="font-mono text-slate-200">
                {remoteStreamCount} / 7
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Pushed</span>
              <span className="font-mono text-cyan-300">{remotePushed}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Queued (DDIL)</span>
              <span
                className={clsx(
                  "font-mono",
                  remoteQueued > 0 ? "text-amber-300" : "text-slate-500",
                )}
              >
                {remoteQueued}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Failed (hard)</span>
              <span
                className={clsx(
                  "font-mono",
                  remoteFailed > 0 ? "text-rose-300" : "text-slate-500",
                )}
              >
                {remoteFailed}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Bytes sent</span>
              <span className="font-mono text-slate-300">
                {formatBytes(remoteBytes)}
              </span>
            </div>
            {remoteLastError && (
              <div className="mt-1 rounded bg-rose-500/10 px-2 py-1 font-mono text-[10px] text-rose-300">
                {remoteLastError}
              </div>
            )}
            {remoteQueued > 0 && (
              <div className="mt-2 flex items-center justify-between">
                <button
                  onClick={handleReplay}
                  disabled={replaying}
                  className="rounded border border-cyan-500/30 bg-cyan-500/15 px-2 py-0.5 text-[11px] font-semibold text-cyan-300 hover:bg-cyan-500/25 disabled:opacity-50"
                >
                  {replaying ? "Replaying…" : "Replay DDIL buffer"}
                </button>
                {replayResult && (
                  <span className="font-mono text-[10px] text-slate-400">
                    {replayResult}
                  </span>
                )}
              </div>
            )}
          </>
        ) : (
          <div className="rounded bg-slate-500/10 px-2 py-1.5 text-[11px] leading-snug text-slate-400">
            Add <span className="font-mono">FOUNDRY_API</span> and{" "}
            <span className="font-mono">FOUNDRY_STREAM_RIDS</span> to{" "}
            <span className="font-mono">.env</span> to push into the real
            Palantir Foundry tenant. The local sink keeps running either way.
          </div>
        )}
      </div>

      {/* ─── Local Foundry-shaped sink ──────────────────────────── */}
      <div className="space-y-2 p-3 text-[12px]">
        <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
          Local mirror sink
        </div>
        <div className="flex items-center justify-between">
          <span className="text-slate-400">Transport</span>
          <span className="font-mono text-slate-200">{transport}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-slate-400">Pushed (this run)</span>
          <span className="font-mono text-slate-100">{totalPushed}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-slate-400">Errors</span>
          <span
            className={clsx(
              "font-mono",
              totalErrors > 0 ? "text-red-300" : "text-slate-500",
            )}
          >
            {totalErrors}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-slate-400">Last success</span>
          <span className="font-mono text-slate-300">
            {lastSuccessSecs === null
              ? "—"
              : lastSuccessSecs < 60
                ? `${Math.round(lastSuccessSecs)}s ago`
                : `${Math.round(lastSuccessSecs / 60)}m ago`}
          </span>
        </div>
      </div>

      <div className="border-t border-panel-700 px-3 py-2">
        <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
          Per-type · pushed → received
          {remoteEnabled && (
            <span className="ml-1 text-cyan-400">· remote pushed</span>
          )}
        </div>
        <ul className="space-y-1">
          {TYPES.map(([t, label]) => {
            const pushed = foundrySync?.push_metrics.success_count[t] ?? 0;
            const received = sinkStats?.counts[t] ?? 0;
            const bytes = sinkStats?.bytes[t] ?? 0;
            const remoteStream = remote?.streams[t];
            const remotePushedHere = remoteStream?.pushed ?? 0;
            const remoteQueuedHere = remoteStream?.queued ?? 0;
            return (
              <li
                key={t}
                className="flex items-center justify-between font-mono text-[11px]"
              >
                <span className="text-slate-400">{label}</span>
                <span className="text-slate-200">
                  {pushed} → <span className="text-slate-300">{received}</span>
                  <span className="ml-2 text-slate-500">
                    {bytes > 0 ? `${Math.round(bytes / 1024)} KB` : ""}
                  </span>
                  {remoteEnabled && (
                    <span className="ml-2 text-cyan-300">
                      · {remotePushedHere}
                      {remoteQueuedHere > 0 && (
                        <span className="text-amber-300">
                          {" "}
                          (+{remoteQueuedHere})
                        </span>
                      )}
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="border-t border-panel-700 px-3 py-2 text-[10px] leading-snug text-slate-500">
        Edge-first by design: ML on-device, only ~600 B intelligence rows
        cross the wire. Local mirror always on; real Palantir Foundry
        tenant push activates when{" "}
        <span className="font-mono">FOUNDRY_API</span> and{" "}
        <span className="font-mono">FOUNDRY_STREAM_RIDS</span> are set in{" "}
        <span className="font-mono">.env</span>.
      </div>
    </div>
  );
}

function lastSuccessAge(map: Record<string, number | null>): number | null {
  const vals = Object.values(map).filter(
    (v): v is number => v !== null && v > 0,
  );
  if (vals.length === 0) return null;
  const latest = Math.max(...vals);
  return Date.now() / 1000 - latest;
}

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(2)} MB`;
}
