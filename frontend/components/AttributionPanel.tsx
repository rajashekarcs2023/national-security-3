"use client";

import clsx from "clsx";

import { useStore } from "@/lib/store";
import type { AttributionResult, AttributionVerdict } from "@/lib/types";

// Phase E — Attribution Panel.
// Shows the live "tonight's tally" of attribution verdicts at the top
// (BLUE / RED_KNOWN / AMBIGUOUS / UNEXPLAINED) and a scroll of the most
// recent attributions below, each with the verdict, best library match,
// confidence, and the operator-readable reason. The attribution engine
// already emits these payloads through the WebSocket; this panel is just
// the renderer.

export default function AttributionPanel() {
  const counters = useStore((s) => s.attributionCounters);
  const recent = useStore((s) => s.attributionRecent);

  const total = Math.max(
    1,
    counters.blue_attributed +
      counters.red_known +
      counters.ambiguous +
      counters.unexplained,
  );

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">
          Attribution
        </h2>
        <span className="font-mono text-[11px] text-slate-500">
          {total} call{total === 1 ? "" : "s"} this run
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4">
        <CounterTile
          label="BLUE"
          count={counters.blue_attributed}
          color="#3fb950"
          tooltip="Friendly emission, blue unit confirmed in range"
        />
        <CounterTile
          label="RED_KNOWN"
          count={counters.red_known}
          color="#f85149"
          tooltip="High-confidence library match for adversary-class emitter"
        />
        <CounterTile
          label="AMBIGUOUS"
          count={counters.ambiguous}
          color="#e3b341"
          tooltip="Mid-confidence match, operator review recommended"
        />
        <CounterTile
          label="UNEXPLAINED"
          count={counters.unexplained}
          color="#a371f7"
          tooltip="No library match — geolocate + investigate"
        />
      </div>

      <div className="max-h-[340px] overflow-y-auto border-t border-panel-700">
        {recent.length === 0 ? (
          <div className="px-4 py-8 text-center text-xs text-slate-500">
            No attributions yet. Wait for the first intelligence event...
          </div>
        ) : (
          <ul className="divide-y divide-panel-800">
            {recent.slice(0, 30).map((a, i) => (
              <li key={`${a.event_id ?? i}-${i}`} className="px-3 py-2">
                <AttributionRow result={a} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
function CounterTile({
  label,
  count,
  color,
  tooltip,
}: {
  label: string;
  count: number;
  color: string;
  tooltip: string;
}) {
  return (
    <div
      className="rounded border border-panel-700 bg-panel-950/40 px-2 py-1.5"
      title={tooltip}
    >
      <div
        className="text-[10px] font-semibold uppercase tracking-wider"
        style={{ color }}
      >
        {label}
      </div>
      <div className="font-mono text-base font-bold text-slate-100">{count}</div>
    </div>
  );
}

// ----------------------------------------------------------------------
function AttributionRow({ result }: { result: AttributionResult }) {
  const verdict = result.verdict;
  const palette = verdictPalette(verdict);
  const score = (result.best_score ?? 0).toFixed(2);
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={clsx(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
            palette.badge,
          )}
        >
          {verdict.replace("_", " ")}
        </span>
        <span className="truncate text-xs text-slate-100" title={result.best_emitter_name ?? ""}>
          {result.best_emitter_name ?? "—"}
        </span>
        <span className="ml-auto font-mono text-[11px] text-slate-500">
          {score}
        </span>
      </div>
      {result.attributed_unit_callsign && (
        <div className="text-[11px] text-slate-300">
          ↪ <span className="text-emerald-300">{result.attributed_unit_callsign}</span>
          <span className="text-slate-500"> ({result.attributed_unit_id})</span>
          <span className="text-slate-500">
            {" "}@ {Math.round(result.distance_to_attributed_unit_m ?? 0)} m
          </span>
        </div>
      )}
      <div className="text-[11px] leading-snug text-slate-400">{result.reason}</div>
      {/* Per-feature mini bar */}
      {result.feature_scores && (
        <FeatureBar feature_scores={result.feature_scores} />
      )}
    </div>
  );
}

// ----------------------------------------------------------------------
function FeatureBar({
  feature_scores,
}: {
  feature_scores: AttributionResult["feature_scores"];
}) {
  const features: Array<["freq" | "bw" | "pattern" | "class", string]> = [
    ["freq", "F"],
    ["bw", "B"],
    ["pattern", "P"],
    ["class", "C"],
  ];
  return (
    <div className="flex items-center gap-1 text-[9px] font-mono text-slate-500">
      {features.map(([k, label]) => {
        const v = feature_scores?.[k] ?? 0;
        const color =
          v >= 0.85 ? "#3fb950" : v >= 0.5 ? "#e3b341" : "#f85149";
        return (
          <div key={k} className="flex items-center gap-1" title={`${k}: ${v.toFixed(2)}`}>
            <span>{label}</span>
            <div className="h-1 w-6 rounded bg-panel-800">
              <div
                className="h-1 rounded"
                style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%`, background: color }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------------------------
function verdictPalette(v: AttributionVerdict): { badge: string } {
  switch (v) {
    case "BLUE_ATTRIBUTED":
      return { badge: "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30" };
    case "RED_KNOWN":
      return { badge: "bg-red-500/15 text-red-300 border border-red-500/30" };
    case "AMBIGUOUS":
      return { badge: "bg-amber-500/15 text-amber-300 border border-amber-500/30" };
    case "UNEXPLAINED":
      return { badge: "bg-purple-500/15 text-purple-300 border border-purple-500/30" };
    default:
      return { badge: "bg-panel-800 text-slate-300" };
  }
}
