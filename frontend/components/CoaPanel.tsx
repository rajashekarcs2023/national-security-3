"use client";

// CoaPanel — Phase C
// Shows the Course-of-Action recommender for the currently-selected track.
// ROE posture buttons gate which actions are even surfaced; within the
// allowed set, options are ranked by appropriateness + prereq-met score.
// Every option carries a rationale and a ROE citation so the operator
// can audit the decision.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useStore } from "@/lib/store";
import type {
  CoaDecision,
  CoaOption,
  CoaRecommendation,
  RoePosture,
  UASTrack,
} from "@/lib/types";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Crosshair,
  Eye,
  Hand,
  Radio,
  Shield,
  Siren,
  Target,
  Zap,
} from "lucide-react";
import clsx from "clsx";

// Static metadata for each posture — short label for the button,
// description shown when hovering / selected.
const POSTURE_META: Record<
  RoePosture,
  { label: string; short: string; tone: string; ring: string }
> = {
  HOLD_FIRE: {
    label: "Hold fire",
    short: "Passive observe / report only",
    tone: "text-slate-300",
    ring: "border-panel-500",
  },
  WARNING_ONLY: {
    label: "Warning",
    short: "Warnings + positive ID, no kinetic",
    tone: "text-accent-blue",
    ring: "border-accent-blue/60",
  },
  DEFENSIVE: {
    label: "Defensive",
    short: "Non-kinetic EW, jam, hand-off authorised",
    tone: "text-accent-amber",
    ring: "border-accent-amber/60",
  },
  WEAPONS_FREE: {
    label: "Weapons free",
    short: "Kinetic engagement on positive hostile ID",
    tone: "text-accent-red",
    ring: "border-accent-red/60",
  },
};
const POSTURE_ORDER: RoePosture[] = [
  "HOLD_FIRE",
  "WARNING_ONLY",
  "DEFENSIVE",
  "WEAPONS_FREE",
];

const ACTION_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  OBSERVE_AND_REPORT: Eye,
  INCREASE_SENSITIVITY: Radio,
  REACQUIRE_VISUAL: Target,
  WARN_AND_QUERY: Siren,
  HAND_OFF_INTERCEPTOR: Hand,
  JAM_RF: Zap,
  MARK_FRIENDLY: Shield,
  ENGAGE_KINETIC: Crosshair,
  DISMISS: CheckCircle2,
};

function riskBadge(risk: string) {
  switch (risk) {
    case "high":
      return { text: "text-accent-red", bg: "bg-accent-red/10" };
    case "medium":
      return { text: "text-accent-amber", bg: "bg-accent-amber/10" };
    default:
      return { text: "text-accent-green", bg: "bg-accent-green/10" };
  }
}

// Score colour band so the eye can spot the top recommendation.
function scoreStyle(score: number, topScore: number) {
  if (topScore <= 0) return "text-slate-400";
  const ratio = score / topScore;
  if (ratio > 0.85) return "text-accent-green";
  if (ratio > 0.5) return "text-accent-cyan";
  if (ratio > 0.25) return "text-slate-300";
  return "text-slate-500";
}

function OptionRow({
  opt,
  topScore,
  onExecute,
  executing,
  justExecuted,
}: {
  opt: CoaOption;
  topScore: number;
  onExecute: (opt: CoaOption) => void;
  executing: boolean;
  justExecuted: string | null;
}) {
  const Icon = ACTION_ICON[opt.action_id] ?? Radio;
  const risk = riskBadge(opt.risk_level);
  const [open, setOpen] = useState(false);
  const is_top = opt.score >= topScore && topScore > 0;
  const was_just_executed = justExecuted === opt.action_id;

  return (
    <div
      className={clsx(
        "rounded-md border p-2.5 transition-colors",
        is_top
          ? "border-accent-green/40 bg-accent-green/5"
          : opt.prerequisites_met
          ? "border-panel-600 bg-panel-900"
          : "border-panel-700 bg-panel-950",
        !opt.prerequisites_met && "opacity-75",
      )}
    >
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 text-left"
      >
        <div className={clsx("mt-0.5 shrink-0", is_top ? "text-accent-green" : "text-slate-400")}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-xs font-medium text-slate-100">{opt.label}</span>
            <span
              className={clsx(
                "font-mono text-[10px] tnum",
                scoreStyle(opt.score, topScore),
              )}
            >
              {opt.score.toFixed(2)}
            </span>
            <span className={clsx("rounded-sm px-1 py-0.5 text-[9px] uppercase", risk.text, risk.bg)}>
              {opt.risk_level}
            </span>
            {opt.reversible ? (
              <span className="rounded-sm bg-white/5 px-1 py-0.5 text-[9px] text-slate-400">reversible</span>
            ) : (
              <span className="rounded-sm bg-accent-red/10 px-1 py-0.5 text-[9px] text-accent-red">non-rev</span>
            )}
            {!opt.prerequisites_met && (
              <span className="flex items-center gap-0.5 rounded-sm bg-accent-amber/10 px-1 py-0.5 text-[9px] text-accent-amber">
                <AlertTriangle className="h-2.5 w-2.5" /> prereqs
              </span>
            )}
          </div>
          <div className="mt-1 truncate text-[10px] text-slate-500">{opt.description}</div>
        </div>
        <ChevronRight
          className={clsx(
            "mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform",
            open && "rotate-90",
          )}
        />
      </button>
      {open && (
        <div className="mt-2 space-y-2 border-t border-panel-700 pt-2">
          <div>
            <div className="text-[9px] uppercase tracking-wider text-slate-500">Rationale</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-slate-300">
              {opt.rationale}
            </div>
          </div>
          <div>
            <div className="text-[9px] uppercase tracking-wider text-slate-500">Expected outcome</div>
            <div className="mt-0.5 text-[11px] text-slate-300">{opt.expected_outcome}</div>
          </div>
          {opt.prerequisites.length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-wider text-slate-500">Prerequisites</div>
              <ul className="mt-0.5 list-disc space-y-0.5 pl-4 text-[11px] text-slate-300">
                {opt.prerequisites.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <div className="text-[9px] uppercase tracking-wider text-slate-500">ROE citation</div>
            <div className="mt-0.5 font-mono text-[10px] text-slate-400">{opt.roe_citation}</div>
          </div>
          <div className="flex items-center justify-between gap-2 pt-1">
            <div className="text-[10px] text-slate-500">
              ETA ~{opt.estimated_time_seconds}s
            </div>
            <button
              onClick={() => onExecute(opt)}
              disabled={executing || was_just_executed}
              className={clsx(
                "rounded border px-2.5 py-1 text-[11px] font-medium",
                was_just_executed
                  ? "border-accent-green/40 bg-accent-green/10 text-accent-green"
                  : "border-accent-cyan/40 bg-accent-cyan/10 text-accent-cyan hover:bg-accent-cyan/20",
                "disabled:cursor-not-allowed disabled:opacity-40",
              )}
            >
              {was_just_executed ? "Executed ✓" : executing ? "Executing..." : "Execute"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 1000) return "now";
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  return `${Math.floor(ms / 3_600_000)}h ago`;
}

export default function CoaPanel() {
  const tracks = useStore((s) => s.tracks);
  const posture = useStore((s) => s.roePosture);
  const decisions = useStore((s) => s.coaDecisions);

  // Default track: freshest TRACKING/REACQUIRED > VISUAL_LOST > DETECTED.
  const defaultTrackId = useMemo(() => {
    if (tracks.length === 0) return null;
    const rank = (t: UASTrack) => {
      switch (t.custody_state) {
        case "TRACKING":
          return 0;
        case "REACQUIRED":
          return 1;
        case "VISUAL_LOST_RF_PRESENT":
          return 2;
        case "DETECTED":
          return 3;
        default:
          return 9;
      }
    };
    return [...tracks].sort((a, b) => rank(a) - rank(b))[0]?.track_id ?? null;
  }, [tracks]);

  const [selectedTrackId, setSelectedTrackId] = useState<string | null>(null);
  const activeTrackId = selectedTrackId ?? defaultTrackId;
  const activeTrack = tracks.find((t) => t.track_id === activeTrackId) ?? null;

  const [rec, setRec] = useState<CoaRecommendation | null>(null);
  const [loading, setLoading] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [justExecuted, setJustExecuted] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [postureBusy, setPostureBusy] = useState(false);

  const fetchRec = useCallback(
    async (tid: string | null, pst: RoePosture) => {
      if (!tid) {
        setRec(null);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const r = await api.coaRecommend({ track_id: tid, posture: pst, top_n: 5 });
        setRec(r);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // Re-fetch whenever track or posture changes.
  useEffect(() => {
    fetchRec(activeTrackId, posture);
  }, [activeTrackId, posture, fetchRec]);

  const onPosture = async (p: RoePosture) => {
    if (p === posture) return;
    setPostureBusy(true);
    try {
      await api.coaSetPosture(p);
      // store updates via WS broadcast; fetchRec runs on posture change.
    } catch (e) {
      setError(String(e));
    } finally {
      setPostureBusy(false);
    }
  };

  const onExecute = async (opt: CoaOption) => {
    if (!activeTrackId) return;
    setExecuting(true);
    setError(null);
    try {
      await api.coaExecute({
        track_id: activeTrackId,
        action_id: opt.action_id,
        notes: `Operator COA commit: ${opt.label}`,
      });
      setJustExecuted(opt.action_id);
      setTimeout(() => setJustExecuted(null), 2500);
      // Refresh recommendation — side effects may have changed posture/track.
      fetchRec(activeTrackId, posture);
    } catch (e) {
      setError(String(e));
    } finally {
      setExecuting(false);
    }
  };

  const topScore = rec?.options.reduce((m, o) => Math.max(m, o.score), 0) ?? 0;

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="flex items-center gap-1.5 text-sm font-semibold tracking-tight text-slate-100">
          <Target className="h-3.5 w-3.5 text-accent-red" /> COA recommender
        </h2>
        <span className="text-[10px] text-slate-500">
          ROE <span className={clsx("font-medium", POSTURE_META[posture].tone)}>{POSTURE_META[posture].label}</span>
        </span>
      </div>

      {/* ROE posture selector */}
      <div className="grid grid-cols-4 gap-1.5 border-b border-panel-700 px-3 py-2">
        {POSTURE_ORDER.map((p) => {
          const meta = POSTURE_META[p];
          const active = p === posture;
          return (
            <button
              key={p}
              onClick={() => onPosture(p)}
              disabled={postureBusy || active}
              title={meta.short}
              className={clsx(
                "rounded border px-1.5 py-1 text-[10px] font-medium transition-colors",
                active
                  ? `${meta.ring} bg-white/5 ${meta.tone}`
                  : "border-panel-600 bg-panel-900 text-slate-400 hover:border-panel-500 hover:text-slate-200",
                postureBusy && !active && "opacity-40",
              )}
            >
              {meta.label}
            </button>
          );
        })}
      </div>

      {/* Track selector */}
      {tracks.length > 1 && (
        <div className="flex items-center gap-2 border-b border-panel-700 px-3 py-2 text-[11px]">
          <span className="text-slate-500">Track</span>
          <select
            className="flex-1 rounded border border-panel-600 bg-panel-900 px-1.5 py-1 font-mono text-[10px] text-slate-200"
            value={activeTrackId ?? ""}
            onChange={(e) => setSelectedTrackId(e.target.value || null)}
          >
            {tracks.map((t) => (
              <option key={t.track_id} value={t.track_id}>
                {t.track_id} — {t.custody_state}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Body */}
      <div className="space-y-2 p-3">
        {error && (
          <div className="rounded border border-accent-red/40 bg-accent-red/5 p-2 text-[11px] text-accent-red">
            {error}
          </div>
        )}
        {!activeTrack ? (
          <div className="rounded border border-dashed border-panel-700 p-4 text-center text-[11px] text-slate-500">
            No active track. Run a scenario to populate custody.
          </div>
        ) : (
          <>
            <div className="rounded border border-panel-600 bg-panel-950/60 p-2 text-[11px]">
              <div className="font-mono text-[10px] text-slate-500">{activeTrack.track_id}</div>
              <div className="mt-0.5 text-slate-300">{rec?.threat_summary ?? "Loading..."}</div>
              {rec?.notes && rec.notes.length > 0 && (
                <ul className="mt-1 space-y-0.5 text-[10px] text-accent-amber">
                  {rec.notes.map((n, i) => (
                    <li key={i}>• {n}</li>
                  ))}
                </ul>
              )}
            </div>
            {loading ? (
              <div className="p-4 text-center text-[11px] text-slate-500">Computing COAs...</div>
            ) : rec && rec.options.length > 0 ? (
              <div className="space-y-1.5">
                {rec.options.map((o) => (
                  <OptionRow
                    key={o.action_id}
                    opt={o}
                    topScore={topScore}
                    onExecute={onExecute}
                    executing={executing}
                    justExecuted={justExecuted}
                  />
                ))}
              </div>
            ) : (
              <div className="text-center text-[11px] text-slate-500">No COAs available.</div>
            )}

            {rec && rec.filtered_out.length > 0 && (
              <details className="mt-2 rounded border border-panel-700 bg-panel-950 p-2 text-[10px] text-slate-500">
                <summary className="cursor-pointer select-none hover:text-slate-300">
                  {rec.filtered_out.length} action(s) filtered out by ROE
                </summary>
                <ul className="mt-1 space-y-0.5 pl-2">
                  {rec.filtered_out.map((f, i) => (
                    <li key={i}>
                      <span className="font-mono text-slate-400">{f.action_id}</span> — {f.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </div>

      {/* Decision audit log */}
      {decisions.length > 0 && (
        <div className="border-t border-panel-700 px-3 py-2">
          <div className="mb-1 text-[9px] uppercase tracking-wider text-slate-500">
            Recent COA decisions
          </div>
          <div className="max-h-32 space-y-1 overflow-y-auto">
            {decisions.slice(0, 6).map((d: CoaDecision) => (
              <div key={d.id} className="text-[10px] text-slate-400">
                <span className="font-mono text-slate-500">{timeAgo(d.timestamp)}</span>{" "}
                <span className="text-slate-300">{d.action_id}</span>{" "}
                <span className="text-slate-500">({d.posture})</span>
                {d.track_id && <span className="text-slate-500"> · {d.track_id}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
