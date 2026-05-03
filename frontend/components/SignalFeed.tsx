"use client";

import { useStore } from "@/lib/store";
import clsx from "clsx";

function dotFor(priority: string): string {
  return {
    critical: "bg-accent-red",
    high: "bg-accent-amber",
    medium: "bg-accent-blue",
    low: "bg-slate-600",
  }[priority] ?? "bg-slate-600";
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export default function SignalFeed() {
  const ticks = useStore((s) => s.recentTicks);
  const recent = ticks.slice(0, 80);

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">Signal feed</h2>
        <span className="text-[11px] text-slate-500 tnum">{ticks.length} ticks</span>
      </div>
      <div className="max-h-[420px] overflow-y-auto px-1 py-1 font-mono text-[11px]">
        {recent.length === 0 ? (
          <div className="p-4 text-center text-slate-500">No readings yet.</div>
        ) : (
          <ul className="divide-y divide-panel-800">
            {recent.map((t) => (
              <li
                key={t.classified.id}
                className={clsx(
                  "flex items-center gap-2 px-2 py-1 hover:bg-panel-800/50",
                  t.classified.is_anomaly && "bg-panel-800/30"
                )}
              >
                <span className={clsx("h-2 w-2 shrink-0 rounded-full", dotFor(t.classified.priority))} />
                <span className="shrink-0 text-slate-500 tnum">{fmtTime(t.reading.timestamp)}</span>
                <span className="shrink-0 tnum text-slate-300">
                  {t.reading.center_frequency_mhz.toFixed(0)}
                </span>
                <span className="shrink-0 text-slate-600">MHz</span>
                <span className="truncate text-slate-300">{t.classified.predicted_class}</span>
                <span className="ml-auto shrink-0 tnum text-slate-500">
                  conf {(t.classified.confidence * 100).toFixed(0)}
                </span>
                <span
                  className={clsx(
                    "shrink-0 tnum",
                    t.classified.ood_score > 0.5 ? "text-accent-red" : "text-slate-500"
                  )}
                >
                  ood {(t.classified.ood_score * 100).toFixed(0)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
