"use client";

import { useState } from "react";
import { useStore } from "@/lib/store";
import { api } from "@/lib/api";
import { Play, Square, Sparkles } from "lucide-react";
import clsx from "clsx";

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export default function ScenarioPanel() {
  const scenarioActive = useStore((s) => s.scenarioActive);
  const steps = useStore((s) => s.scenarioSteps);
  const [busy, setBusy] = useState(false);

  const run = async (
    scenario: "full_demo" | "quick_anomaly" | "drone_swarm" | "cross_cue_demo",
  ) => {
    setBusy(true);
    try {
      await api.scenarioRun(scenario);
    } catch (e) {
      console.warn(e);
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await api.scenarioStop();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100 flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-accent-violet" /> Demo scenario
        </h2>
        {scenarioActive && (
          <span className="animate-pulse-dot rounded-sm bg-accent-violet/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-accent-violet">
            running
          </span>
        )}
      </div>
      <div className="space-y-2 p-3">
        <div className="grid grid-cols-1 gap-1.5">
          <button
            onClick={() => run("full_demo")}
            disabled={busy || scenarioActive}
            className="flex w-full items-center justify-center gap-1.5 rounded border border-accent-violet/30 bg-accent-violet/5 px-3 py-2 text-xs font-medium text-accent-violet transition-colors hover:bg-accent-violet/10 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Play className="h-3.5 w-3.5" />
            Run full 14-step demo
          </button>
          <div className="grid grid-cols-2 gap-1.5">
            <button
              onClick={() => run("quick_anomaly")}
              disabled={busy || scenarioActive}
              className="flex items-center justify-center gap-1 rounded border border-panel-600 bg-panel-900 px-2 py-1.5 text-[11px] text-slate-300 transition-colors hover:text-slate-100 disabled:opacity-40"
            >
              Quick anomaly
            </button>
            <button
              onClick={() => run("drone_swarm")}
              disabled={busy || scenarioActive}
              className="flex items-center justify-center gap-1 rounded border border-panel-600 bg-panel-900 px-2 py-1.5 text-[11px] text-slate-300 transition-colors hover:text-slate-100 disabled:opacity-40"
            >
              Drone swarm
            </button>
          </div>
          {/* Phase A — cross-sensor cueing demo (RF → EO multi-modal beats). */}
          <button
            onClick={() => run("cross_cue_demo")}
            disabled={busy || scenarioActive}
            className="flex w-full items-center justify-center gap-1.5 rounded border border-accent-blue/30 bg-accent-blue/5 px-3 py-2 text-[11px] font-medium text-accent-blue transition-colors hover:bg-accent-blue/10 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Cross-sensor cueing (RF → EO)
          </button>
          {scenarioActive && (
            <button
              onClick={stop}
              disabled={busy}
              className="flex w-full items-center justify-center gap-1.5 rounded border border-accent-red/30 bg-accent-red/5 px-3 py-2 text-xs font-medium text-accent-red hover:bg-accent-red/10"
            >
              <Square className="h-3.5 w-3.5" />
              Stop scenario
            </button>
          )}
        </div>

        {steps.length > 0 && (
          <div className="rounded border border-panel-700 bg-panel-950/50 p-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
              Announcements
            </div>
            <ul className="max-h-40 space-y-1 overflow-y-auto text-[11px]">
              {steps.map((s, i) => (
                <li
                  key={i}
                  className={clsx(
                    "flex items-start gap-2",
                    s.phase === "announce" && "text-accent-violet",
                    s.phase === "network" && (s.online ? "text-accent-green" : "text-accent-red"),
                    s.phase === "sensitivity" && "text-accent-amber",
                    s.phase === "eo_fail" && "text-accent-violet",
                    s.phase === "done" && "text-slate-500"
                  )}
                >
                  <span className="mt-0.5 h-1 w-1 shrink-0 rounded-full bg-current opacity-70" />
                  <span className="min-w-0 flex-1">
                    {s.phase === "announce" && s.text}
                    {s.phase === "network" &&
                      `Link ${s.online ? "RESTORED" : "DROPPED"}${s.note ? ` — ${s.note}` : ""}`}
                    {s.phase === "sensitivity" &&
                      `Sensitivity set to ${s.mode?.toUpperCase()}${s.note ? ` — ${s.note}` : ""}`}
                    {s.phase === "eo_fail" &&
                      (s.text ?? `EO subsystem masked${s.note ? ` — ${s.note}` : ""}`)}
                    {s.phase === "start" && `Scenario "${s.name}" started`}
                    {s.phase === "done" && `Scenario "${s.name}" complete`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
