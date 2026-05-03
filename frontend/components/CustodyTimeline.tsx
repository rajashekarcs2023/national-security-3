"use client";

import { useStore } from "@/lib/store";
import clsx from "clsx";

function stateStyle(state: string) {
  switch (state) {
    case "DETECTED":
      return { dot: "bg-accent-blue", text: "text-accent-blue" };
    case "TRACKING":
      return { dot: "bg-accent-amber", text: "text-accent-amber" };
    case "VISUAL_LOST_RF_PRESENT":
      return { dot: "bg-accent-violet", text: "text-accent-violet" };
    case "REACQUIRED":
      return { dot: "bg-accent-green", text: "text-accent-green" };
    case "TRACK_LOST":
      return { dot: "bg-accent-red", text: "text-accent-red" };
    case "CLEARED":
      return { dot: "bg-slate-500", text: "text-slate-400" };
    case "DISMISSED":
      return { dot: "bg-slate-600", text: "text-slate-500" };
    default:
      return { dot: "bg-slate-600", text: "text-slate-500" };
  }
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

export default function CustodyTimeline() {
  const tracks = useStore((s) => s.tracks);
  const logs = useStore((s) => s.custodyLogs);

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">Custody timeline</h2>
        <span className="text-[11px] text-slate-500 tnum">{tracks.length} tracks</span>
      </div>

      <div className="max-h-[440px] overflow-y-auto">
        {tracks.length === 0 && logs.length === 0 ? (
          <div className="p-6 text-center text-xs text-slate-500">
            No tracks yet. Run the demo scenario to open custody tracks.
          </div>
        ) : (
          <>
            {/* Active tracks */}
            {tracks.length > 0 && (
              <div className="space-y-1.5 border-b border-panel-800 p-3">
                {tracks.map((t) => {
                  const st = stateStyle(t.custody_state);
                  return (
                    <div
                      key={t.track_id}
                      className="flex items-center justify-between rounded border border-panel-700 bg-panel-950/50 px-2.5 py-1.5 text-[11px]"
                    >
                      <div className="flex items-center gap-2">
                        <span className={clsx("h-2 w-2 rounded-full", st.dot)} />
                        <span className="font-mono text-slate-300">{t.track_id}</span>
                        <span className="text-slate-600">·</span>
                        <span className="text-slate-500">{t.sector}</span>
                        <span className="text-slate-600">·</span>
                        <span className="text-slate-500">{t.n_detections}x</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className={clsx("font-semibold uppercase", st.text)}>
                          {t.custody_state.replace(/_/g, " ")}
                        </span>
                        <span className="rounded-sm bg-white/5 px-1.5 py-0.5 text-[10px] text-slate-400">
                          {t.threat_level}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* State transition log */}
            <ul className="divide-y divide-panel-800">
              {logs.slice(0, 40).map((log) => {
                const st = stateStyle(log.new_state);
                return (
                  <li key={log.id} className="px-3 py-2 text-[11px]">
                    <div className="flex items-center gap-2">
                      <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", st.dot)} />
                      <span className="font-mono text-slate-400">{fmtTime(log.timestamp)}</span>
                      <span className="font-mono text-slate-300">{log.track_id}</span>
                      <span className="text-slate-600">·</span>
                      <span className="text-slate-500">
                        {log.previous_state ?? "NONE"} →{" "}
                        <span className={clsx("font-semibold", st.text)}>
                          {log.new_state}
                        </span>
                      </span>
                    </div>
                    {log.action_cue && (
                      <div className="mt-1 pl-4 text-slate-400">{log.action_cue}</div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
