"use client";

import { useState } from "react";
import { useStore } from "@/lib/store";
import { api } from "@/lib/api";
import CotPreviewModal from "./CotPreviewModal";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Radio as RadioIcon,
  Send,
  Upload,
  WifiOff,
  XCircle,
} from "lucide-react";
import clsx from "clsx";
import type { AttributionResult, AttributionVerdict, IntelligenceEvent, TdoaSolution } from "@/lib/types";

function priorityStyle(priority: string) {
  switch (priority) {
    case "critical":
      return {
        border: "border-accent-red/50",
        bg: "bg-accent-red/5",
        text: "text-accent-red",
        icon: <AlertTriangle className="h-4 w-4" />,
      };
    case "high":
      return {
        border: "border-accent-amber/50",
        bg: "bg-accent-amber/5",
        text: "text-accent-amber",
        icon: <AlertTriangle className="h-4 w-4" />,
      };
    case "medium":
      return {
        border: "border-accent-blue/40",
        bg: "bg-accent-blue/5",
        text: "text-accent-blue",
        icon: <RadioIcon className="h-4 w-4" />,
      };
    default:
      return {
        border: "border-panel-600",
        bg: "bg-panel-900",
        text: "text-slate-400",
        icon: <RadioIcon className="h-4 w-4" />,
      };
  }
}

function syncBadge(sync: string) {
  if (sync === "synced")
    return (
      <span className="flex items-center gap-1 rounded-sm bg-accent-green/10 px-1.5 py-0.5 text-[10px] text-accent-green">
        <Upload className="h-3 w-3" /> synced
      </span>
    );
  if (sync === "queued")
    return (
      <span className="flex items-center gap-1 rounded-sm bg-accent-amber/10 px-1.5 py-0.5 text-[10px] text-accent-amber">
        <Clock className="h-3 w-3" /> queued
      </span>
    );
  if (sync === "local_only")
    return (
      <span className="flex items-center gap-1 rounded-sm bg-panel-800 px-1.5 py-0.5 text-[10px] text-slate-500">
        <WifiOff className="h-3 w-3" /> local
      </span>
    );
  return null;
}

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 1000) return "now";
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  return `${Math.floor(ms / 3_600_000)}h ago`;
}

function verdictBadge(verdict: AttributionVerdict): { className: string; short: string } {
  switch (verdict) {
    case "BLUE_ATTRIBUTED":
      return {
        className: "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30",
        short: "BLUE",
      };
    case "RED_KNOWN":
      return {
        className: "bg-red-500/15 text-red-300 border border-red-500/30",
        short: "RED",
      };
    case "AMBIGUOUS":
      return {
        className: "bg-amber-500/15 text-amber-300 border border-amber-500/30",
        short: "AMBIG",
      };
    case "UNEXPLAINED":
      return {
        className: "bg-purple-500/15 text-purple-300 border border-purple-500/30",
        short: "UNK",
      };
  }
}

function EventCard({
  e,
  attribution,
  fix,
}: {
  e: IntelligenceEvent;
  attribution?: AttributionResult;
  fix?: TdoaSolution;
}) {
  const st = priorityStyle(e.priority);
  const [showCot, setShowCot] = useState(false);
  const vb = attribution ? verdictBadge(attribution.verdict) : null;
  return (
    <div
      className={clsx(
        "rounded-md border p-3 transition-colors",
        st.border,
        st.bg
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-start gap-2">
          <div className={clsx("mt-0.5", st.text)}>{st.icon}</div>
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-slate-100">
              {e.title}
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-slate-500">
              <span className="font-mono">{e.classification}</span>
              <span>·</span>
              <span>conf {(e.confidence * 100).toFixed(0)}%</span>
              <span>·</span>
              <span>OOD {(e.ood_score * 100).toFixed(0)}%</span>
              <span>·</span>
              <span>{e.track_id || "—"}</span>
              <span>·</span>
              <span>{timeAgo(e.timestamp)}</span>
            </div>
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span
            className={clsx(
              "rounded-sm px-1.5 py-0.5 text-[10px] font-semibold uppercase",
              st.text,
              "bg-white/5"
            )}
          >
            {e.priority}
          </span>
          {vb && (
            <span
              className={clsx(
                "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
                vb.className,
              )}
              title={attribution?.reason ?? ""}
            >
              {vb.short}
            </span>
          )}
          {syncBadge(e.sync_status)}
        </div>
      </div>

      <div className="mt-2 text-xs text-slate-300">{e.summary}</div>

      {/* Phase E — attribution + TDOA inline strip */}
      {attribution && (
        <div className="mt-2 rounded border border-panel-700 bg-panel-950/40 px-2 py-1.5 text-[11px]">
          <div className="text-slate-300">
            <span className="text-slate-500">match:</span>{" "}
            <span className="text-slate-100">
              {attribution.best_emitter_name ?? "—"}
            </span>{" "}
            <span className="text-slate-500">·</span>{" "}
            <span className="font-mono text-slate-300">
              {attribution.best_score.toFixed(2)}
            </span>
          </div>
          {attribution.attributed_unit_callsign && (
            <div className="mt-0.5 text-slate-300">
              <span className="text-slate-500">unit:</span>{" "}
              <span className="text-emerald-300">
                {attribution.attributed_unit_callsign}
              </span>{" "}
              <span className="text-slate-500">
                @ {Math.round(attribution.distance_to_attributed_unit_m ?? 0)} m
              </span>
            </div>
          )}
          {fix && (
            <div className="mt-0.5 font-mono text-slate-400">
              <span className="text-slate-500">fix:</span>{" "}
              {fix.lat.toFixed(5)}, {fix.lon.toFixed(5)}
              <span className="text-slate-500"> · CEP </span>
              <span className="text-slate-200">{Math.round(fix.cep_m)} m</span>
              <span className="text-slate-500"> · GDOP </span>
              <span className="text-slate-200">{fix.gdop.toFixed(2)}</span>
            </div>
          )}
        </div>
      )}

      {e.llm_brief && (
        <div className="mt-2 rounded border border-panel-600 bg-panel-950/60 p-2 text-[11px] leading-relaxed text-slate-300">
          <div className="mb-1 text-[9px] uppercase tracking-wider text-accent-cyan">
            Edge LLM brief
          </div>
          {e.llm_brief}
        </div>
      )}

      <details className="mt-2 text-[11px] text-slate-500">
        <summary className="cursor-pointer select-none hover:text-slate-300">
          Evidence &middot; recommended action
        </summary>
        <div className="mt-1 rounded border border-panel-700 bg-panel-950/50 p-2 font-mono">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
            recommended
          </div>
          <div className="mb-2 text-slate-300">{e.recommended_action}</div>
          <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
            evidence
          </div>
          <ul className="space-y-0.5 text-slate-400">
            {e.evidence.map((ev, i) => (
              <li key={i}>{ev}</li>
            ))}
          </ul>
        </div>
      </details>

      <div className="mt-2 flex items-center gap-1.5">
        {e.track_id && (
          <>
            <button
              className="rounded border border-panel-600 bg-panel-900 px-2 py-1 text-[11px] text-slate-300 hover:border-accent-green/50 hover:text-accent-green"
              onClick={() =>
                api.operatorAction({
                  event_id: e.id,
                  track_id: e.track_id,
                  action_type: "CONFIRM",
                  details: "Operator confirmed",
                })
              }
            >
              <CheckCircle2 className="mr-1 inline h-3 w-3" />
              Confirm
            </button>
            <button
              className="rounded border border-panel-600 bg-panel-900 px-2 py-1 text-[11px] text-slate-300 hover:border-accent-red/50 hover:text-accent-red"
              onClick={() =>
                api.operatorAction({
                  event_id: e.id,
                  track_id: e.track_id,
                  action_type: "DISMISS",
                  details: "Operator dismissed",
                })
              }
            >
              <XCircle className="mr-1 inline h-3 w-3" />
              Dismiss
            </button>
            <button
              className="rounded border border-panel-600 bg-panel-900 px-2 py-1 text-[11px] text-slate-300 hover:border-accent-amber/50 hover:text-accent-amber"
              onClick={() =>
                api.operatorAction({
                  event_id: e.id,
                  track_id: e.track_id,
                  action_type: "ESCALATE",
                  details: "Operator escalated",
                })
              }
            >
              <AlertTriangle className="mr-1 inline h-3 w-3" />
              Escalate
            </button>
          </>
        )}
        <button
          // CoT publish is allowed for *every* event — even ones without a
          // live track (the CoT will be built off event-only data and use
          // the site's GPS for position). The icon mapping handles that.
          className="ml-auto rounded border border-panel-600 bg-panel-900 px-2 py-1 text-[11px] text-slate-300 hover:border-accent-cyan/50 hover:text-accent-cyan"
          onClick={() => setShowCot(true)}
          title="Build a CoT 2.0 message and broadcast to TAK clients"
        >
          <Send className="mr-1 inline h-3 w-3" />
          Publish to ATAK
        </button>
      </div>
      {showCot && <CotPreviewModal event={e} onClose={() => setShowCot(false)} />}
    </div>
  );
}

export default function IntelligenceEvents() {
  const events = useStore((s) => s.events);
  const attributionByEvent = useStore((s) => s.attributionByEvent);
  const tdoaByEvent = useStore((s) => s.tdoaByEvent);

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">
          Intelligence events
        </h2>
        <span className="text-xs text-slate-500 tnum">{events.length} total</span>
      </div>
      <div className="max-h-[640px] space-y-2 overflow-y-auto p-3">
        {events.length === 0 ? (
          <div className="rounded border border-dashed border-panel-700 p-6 text-center text-xs text-slate-500">
            Awaiting classifications &middot; low-priority readings are filtered at the edge
          </div>
        ) : (
          events.map((e) => (
            <EventCard
              key={e.id}
              e={e}
              attribution={attributionByEvent[e.id]}
              fix={tdoaByEvent[e.id]}
            />
          ))
        )}
      </div>
    </div>
  );
}
