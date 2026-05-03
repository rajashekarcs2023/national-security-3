"use client";

import clsx from "clsx";
import { CheckCircle2, Send } from "lucide-react";
import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import type { CoaActionId, PersistentEmitter } from "@/lib/types";

// Phase E — Persistent Unknown Emitter panel.
// Shows clusters that the persistence detector has promoted (3+ unexplained
// detections in the same place over the past hour). Operator should
// hand-off / investigate these — they are the "I can't explain this one"
// signal the mentor called out.
//
// Each row has a single-click button that executes the cluster's
// recommended COA (``INVESTIGATE_AND_GEOLOCATE`` for MEDIUM, or
// ``HAND_OFF_INTERCEPTOR`` for HIGH). The action is audited via
// ``/api/coa/execute``, broadcast over WS, and reflected in the COA
// decision log. We key the button state on the *latest* detection
// event_id in the cluster so subsequent updates to the same cluster do
// not show "handed off" stale-ly — a fresh detection opens a fresh
// call-to-action.

export default function PersistencePanel() {
  const persistent = useStore((s) => s.persistentEmitters);
  const coaDecisions = useStore((s) => s.coaDecisions);
  const clusters = Object.values(persistent).sort(
    (a, b) =>
      new Date(b.last_seen).getTime() - new Date(a.last_seen).getTime(),
  );

  // Build a fast set of "event_id -> executed action_id" so each cluster
  // row can flash a confirmation without re-fetching. Only decisions
  // executed since the cluster's *last_seen* count, so newer detections
  // re-arm the button.
  const executedByEvent = useMemo(() => {
    const out: Record<string, { action_id: CoaActionId; ts: string }> = {};
    for (const d of coaDecisions) {
      if (!d.event_id) continue;
      // Keep the latest decision per event id.
      const prev = out[d.event_id];
      if (!prev || new Date(d.timestamp).getTime() > new Date(prev.ts).getTime()) {
        out[d.event_id] = { action_id: d.action_id, ts: d.timestamp };
      }
    }
    return out;
  }, [coaDecisions]);

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">
          Persistent unknown emitters
        </h2>
        <span className="font-mono text-[11px] text-slate-500">
          {clusters.length} cluster{clusters.length === 1 ? "" : "s"}
        </span>
      </div>

      {clusters.length === 0 ? (
        <div className="px-4 py-8 text-center text-xs text-slate-500">
          No persistent unknown emitters yet. Streaming DBSCAN waits for 3+
          unexplained detections of the same signal class within 200&nbsp;m
          and 1&nbsp;hour.
        </div>
      ) : (
        <ul className="max-h-[420px] divide-y divide-panel-800 overflow-y-auto">
          {clusters.map((c) => {
            // Latest detection event for this cluster — that's the event id
            // we stamp on the COA decision so the kill-chain trail (event
            // → persistence → COA) is consistent across logs.
            const latestEventId =
              c.detection_event_ids[c.detection_event_ids.length - 1];
            const executed = latestEventId
              ? executedByEvent[latestEventId]
              : undefined;
            return (
              <li key={c.id} className="px-3 py-3">
                <PersistenceRow
                  cluster={c}
                  latestEventId={latestEventId}
                  executedActionId={executed?.action_id}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function PersistenceRow({
  cluster,
  latestEventId,
  executedActionId,
}: {
  cluster: PersistentEmitter;
  latestEventId?: string;
  executedActionId?: CoaActionId;
}) {
  const ageMin = Math.round(
    (Date.now() - new Date(cluster.first_seen).getTime()) / 1000 / 60,
  );
  const palette = priorityPalette(cluster.priority);
  const recommendedActionId = cluster.recommended_action as CoaActionId;

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Disable if there is no event to attach to OR if the latest detection
  // already has a matching decision.
  const alreadyExecuted =
    executedActionId !== undefined && executedActionId === recommendedActionId;
  const disabled = !latestEventId || submitting || alreadyExecuted;

  const execute = async () => {
    if (!latestEventId) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.coaExecute({
        event_id: latestEventId,
        action_id: recommendedActionId,
        notes: `Auto-sourced from persistence cluster ${cluster.id} ` +
          `(${cluster.signal_class}, n=${cluster.n_detections}, ` +
          `priority=${cluster.priority}, radius=${Math.round(cluster.radius_m)} m)`,
      });
      // Decision will flow back through WS → coaDecisions → executedByEvent,
      // so the UI naturally flips to the "already executed" state on the
      // next render. No manual store poke needed.
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={clsx(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
            palette,
          )}
        >
          {cluster.priority}
        </span>
        <span className="font-mono text-xs text-slate-100">
          {cluster.signal_class}
        </span>
        <span className="ml-auto font-mono text-[11px] text-slate-500">
          {cluster.n_detections}× hits
        </span>
      </div>
      <div className="font-mono text-[11px] text-slate-400">
        {cluster.lat.toFixed(5)}, {cluster.lon.toFixed(5)}
        <span className="text-slate-600"> · radius {Math.round(cluster.radius_m)} m</span>
      </div>
      <div className="text-[11px] text-slate-400">
        First seen {ageMin} min ago · last hit {timeAgo(cluster.last_seen)}
      </div>
      <div className="text-[11px] text-amber-300">
        Recommended: <span className="font-mono">{cluster.recommended_action}</span>
      </div>

      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          disabled={disabled}
          onClick={execute}
          className={clsx(
            "inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] font-medium transition-colors",
            alreadyExecuted
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300 cursor-default"
              : cluster.priority === "high"
                ? "border-red-500/50 bg-red-500/10 text-red-200 hover:bg-red-500/20"
                : "border-amber-500/50 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20",
            disabled && !alreadyExecuted ? "opacity-50 cursor-not-allowed" : "",
          )}
          title={
            alreadyExecuted
              ? "Latest detection already logged to COA"
              : `Execute ${recommendedActionId} via /api/coa/execute`
          }
        >
          {alreadyExecuted ? (
            <>
              <CheckCircle2 className="h-3 w-3" />
              {recommendedActionId} logged
            </>
          ) : (
            <>
              <Send className="h-3 w-3" />
              Execute {recommendedActionId}
            </>
          )}
        </button>
        {error && (
          <span className="text-[10px] text-red-300" title={error}>
            COA execute failed — {error.slice(0, 40)}…
          </span>
        )}
      </div>
    </div>
  );
}

function priorityPalette(p: PersistentEmitter["priority"]): string {
  switch (p) {
    case "high":
      return "bg-red-500/15 text-red-300 border border-red-500/30";
    case "medium":
      return "bg-amber-500/15 text-amber-300 border border-amber-500/30";
    default:
      return "bg-panel-800 text-slate-300";
  }
}

function timeAgo(iso: string): string {
  const dt = (Date.now() - new Date(iso).getTime()) / 1000;
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  return `${Math.round(dt / 3600)}h ago`;
}
