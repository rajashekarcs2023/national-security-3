"use client";

// CrossSensorPanel — Phase A
// ------------------------------------------------------------------
// Visual surface for the cross-sensor cueing layer. Each card is an
// EO/IR tipping-camera frame triggered by an RF custody open, with:
//   - a synthetic camera frame (canvas) so the demo runs offline
//   - bbox overlay showing where the visual classifier "saw" the target
//   - frame_kind + confidence + slew-time chips so an operator can read
//     fusion state at a glance (CONFIRMED / NO VISUAL / CONTRADICTION)
//   - the parent track's custody state, so VISUAL_LOST_RF_PRESENT is
//     instantly visible the moment the gimbal misses a confirmed track
//
// Honest framing: the imagery is *synthetic*. We render shapes that
// match the declared frame_kind so the operator sees something that
// *looks* like a quadcopter / fixed-wing / bird, but no real EO data
// is being ingested. The README and SOURCE badge call this out
// explicitly so judges aren't misled.

import { useStore } from "@/lib/store";
import type { EOFrameKind, EOObservation, UASTrack } from "@/lib/types";
import clsx from "clsx";
import { useEffect, useRef } from "react";

// ------- visual styling per frame kind ------------------------------
function kindStyle(kind: EOFrameKind): { label: string; dot: string; text: string } {
  switch (kind) {
    case "quadcopter":
      return { label: "QUADCOPTER", dot: "bg-accent-red", text: "text-accent-red" };
    case "fixed_wing":
      return { label: "FIXED-WING", dot: "bg-accent-amber", text: "text-accent-amber" };
    case "person":
      return { label: "PERSON", dot: "bg-accent-amber", text: "text-accent-amber" };
    case "bird":
      return { label: "BIRD", dot: "bg-slate-500", text: "text-slate-300" };
    case "no_visual":
      return { label: "NO VISUAL", dot: "bg-accent-violet", text: "text-accent-violet" };
    case "contradiction":
      return { label: "CONTRADICTION", dot: "bg-accent-red", text: "text-accent-red" };
    default:
      return { label: kind, dot: "bg-slate-600", text: "text-slate-400" };
  }
}

// Custody-state colour matched to CustodyTimeline so the two panels
// agree visually.
function custodyStyle(state: string): { dot: string; text: string } {
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
    default:
      return { dot: "bg-slate-600", text: "text-slate-400" };
  }
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

// Hash a string into [0, 1). Used to seed deterministic frame jitter
// so a given observation always renders the same scene — important
// for screenshots and dry-runs.
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10000) / 10000;
}

// ------------------------------------------------------------------
// Synthetic frame renderer — paints a believable thumbnail for each
// frame_kind so the panel is informative without real imagery.
// ------------------------------------------------------------------
function paintFrame(
  ctx: CanvasRenderingContext2D,
  obs: EOObservation,
  W: number,
  H: number,
) {
  const seed = hash01(obs.id || obs.track_id);
  // Sky / ground gradient — daytime EO. Different palette for "no_visual"
  // (smoky / occluded) so the operator immediately reads "we don't see it".
  const isOccluded = obs.frame_kind === "no_visual";
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  if (isOccluded) {
    grad.addColorStop(0, "#1c1f24");
    grad.addColorStop(1, "#0f1115");
  } else {
    grad.addColorStop(0, "#243447");
    grad.addColorStop(0.65, "#3a4a5e");
    grad.addColorStop(1, "#2b3a2e");
  }
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);

  // Faint horizon line so the frame reads as outdoors.
  if (!isOccluded) {
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, H * 0.65);
    ctx.lineTo(W, H * 0.65);
    ctx.stroke();
  }

  // Bbox in normalised coords — backend gives (x, y, w, h) on [0, 1].
  const [bx, by, bw, bh] = obs.bbox;
  const px = bx * W;
  const py = by * H;
  const pw = bw * W;
  const ph = bh * H;
  const cx = px + pw / 2;
  const cy = py + ph / 2;

  // Draw the target shape inside the bbox, by frame_kind.
  ctx.save();
  switch (obs.frame_kind) {
    case "quadcopter": {
      // Cross of four rotors + central body.
      const arm = Math.min(pw, ph) * 0.55;
      ctx.strokeStyle = "#0f1115";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(cx - arm, cy - arm);
      ctx.lineTo(cx + arm, cy + arm);
      ctx.moveTo(cx + arm, cy - arm);
      ctx.lineTo(cx - arm, cy + arm);
      ctx.stroke();
      // Rotor disks
      ctx.fillStyle = "rgba(20,22,26,0.85)";
      const r = Math.max(3, Math.min(pw, ph) * 0.18);
      [
        [cx - arm, cy - arm],
        [cx + arm, cy - arm],
        [cx - arm, cy + arm],
        [cx + arm, cy + arm],
      ].forEach(([x, y]) => {
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      });
      // Body
      ctx.fillStyle = "#0a0c10";
      ctx.beginPath();
      ctx.arc(cx, cy, Math.max(3, Math.min(pw, ph) * 0.16), 0, Math.PI * 2);
      ctx.fill();
      break;
    }
    case "fixed_wing": {
      // Long fuselage + swept wings.
      ctx.fillStyle = "#0a0c10";
      ctx.beginPath();
      ctx.ellipse(cx, cy, pw * 0.42, ph * 0.18, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(cx - pw * 0.15, cy);
      ctx.lineTo(cx + pw * 0.05, cy - ph * 0.45);
      ctx.lineTo(cx + pw * 0.18, cy - ph * 0.4);
      ctx.lineTo(cx + pw * 0.05, cy);
      ctx.closePath();
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(cx - pw * 0.15, cy);
      ctx.lineTo(cx + pw * 0.05, cy + ph * 0.45);
      ctx.lineTo(cx + pw * 0.18, cy + ph * 0.4);
      ctx.lineTo(cx + pw * 0.05, cy);
      ctx.closePath();
      ctx.fill();
      break;
    }
    case "bird": {
      // Stylised wing-flap glyph.
      ctx.strokeStyle = "#0a0c10";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(cx - pw * 0.4, cy);
      ctx.quadraticCurveTo(cx - pw * 0.15, cy - ph * 0.45, cx, cy);
      ctx.quadraticCurveTo(cx + pw * 0.15, cy - ph * 0.45, cx + pw * 0.4, cy);
      ctx.stroke();
      break;
    }
    case "person": {
      // Head + torso silhouette.
      ctx.fillStyle = "#0a0c10";
      ctx.beginPath();
      ctx.arc(cx, cy - ph * 0.32, Math.min(pw, ph) * 0.18, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillRect(cx - pw * 0.18, cy - ph * 0.05, pw * 0.36, ph * 0.55);
      break;
    }
    case "contradiction": {
      // Adversary deception artefact — mismatched RF/EO. Render a
      // "ghost" ellipse with slashes through the bbox so the reader
      // sees something is wrong even before reading the chip.
      ctx.fillStyle = "rgba(220,80,80,0.25)";
      ctx.beginPath();
      ctx.ellipse(cx, cy, pw * 0.45, ph * 0.45, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(220,80,80,0.7)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px + pw, py + ph);
      ctx.moveTo(px + pw, py);
      ctx.lineTo(px, py + ph);
      ctx.stroke();
      break;
    }
    case "no_visual": {
      // Heavy noise / fog — no target rendered. Draw scattered grey
      // dots to imply degraded imagery.
      ctx.fillStyle = "rgba(220,220,220,0.08)";
      for (let i = 0; i < 220; i++) {
        const x = (seed * 7 + i * 13.37) % W;
        const y = (seed * 11 + i * 29.71) % H;
        ctx.fillRect(x, y, 1, 1);
      }
      break;
    }
  }
  ctx.restore();

  // Bbox overlay (skip when no_visual — there's nothing to box).
  if (obs.frame_kind !== "no_visual") {
    const ok = obs.confirms_rf;
    ctx.strokeStyle = ok ? "rgba(74, 222, 128, 0.95)" : "rgba(248, 113, 113, 0.95)";
    ctx.lineWidth = 2;
    ctx.strokeRect(px, py, pw, ph);
    // Confidence label above the bbox.
    ctx.fillStyle = ok ? "rgba(74, 222, 128, 0.95)" : "rgba(248, 113, 113, 0.95)";
    ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
    const label = `${obs.classification} ${Math.round(obs.confidence * 100)}%`;
    ctx.fillText(label, px, Math.max(10, py - 4));
  }

  // Crosshair reticle at frame centre — sells the "gimbal pointed at
  // bearing X" affordance.
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(W / 2 - 8, H / 2);
  ctx.lineTo(W / 2 + 8, H / 2);
  ctx.moveTo(W / 2, H / 2 - 8);
  ctx.lineTo(W / 2, H / 2 + 8);
  ctx.stroke();

  // Bearing readout in the corner.
  ctx.fillStyle = "rgba(255,255,255,0.55)";
  ctx.font = "9px ui-monospace, SFMono-Regular, monospace";
  ctx.fillText(`BRG ${obs.bearing_deg.toFixed(0)}\u00b0`, 6, H - 6);
}

// ------------------------------------------------------------------
// One card per (track, latest observation).
// ------------------------------------------------------------------
function EoCard({ obs, track }: { obs: EOObservation; track: UASTrack | null }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    paintFrame(ctx, obs, c.width, c.height);
  }, [obs]);

  const ks = kindStyle(obs.frame_kind);
  const cs = custodyStyle(track?.custody_state ?? "DETECTED");

  return (
    <div className="rounded border border-panel-700 bg-panel-950/60 p-2">
      <div className="mb-1.5 flex items-center justify-between text-[11px]">
        <div className="flex items-center gap-2">
          <span className="font-mono text-slate-300">{obs.track_id}</span>
          <span className="text-slate-600">·</span>
          <span className="text-slate-500">{obs.sector}</span>
        </div>
        <span className="font-mono text-slate-500">{fmtTime(obs.timestamp)}</span>
      </div>

      <div className="relative overflow-hidden rounded border border-panel-800 bg-black">
        <canvas
          ref={canvasRef}
          width={320}
          height={180}
          className="block h-auto w-full"
        />
        <div className="absolute left-1 top-1 rounded-sm bg-black/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-slate-300">
          EO · sim
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
        <span className={clsx("flex items-center gap-1 rounded-sm bg-white/5 px-1.5 py-0.5", ks.text)}>
          <span className={clsx("h-1.5 w-1.5 rounded-full", ks.dot)} />
          {ks.label}
        </span>
        <span
          className={clsx(
            "rounded-sm px-1.5 py-0.5",
            obs.confirms_rf
              ? "bg-accent-green/15 text-accent-green"
              : obs.frame_kind === "no_visual"
              ? "bg-accent-violet/15 text-accent-violet"
              : "bg-accent-red/15 text-accent-red",
          )}
        >
          {obs.confirms_rf
            ? "CONFIRMS RF"
            : obs.frame_kind === "no_visual"
            ? "NO VISUAL"
            : "DOES NOT CONFIRM"}
        </span>
        <span className="rounded-sm bg-white/5 px-1.5 py-0.5 text-slate-400">
          conf {Math.round(obs.confidence * 100)}%
        </span>
        <span className="rounded-sm bg-white/5 px-1.5 py-0.5 text-slate-400">
          slew {obs.slew_time_ms}ms
        </span>
        {track && (
          <span
            className={clsx("ml-auto flex items-center gap-1 rounded-sm bg-white/5 px-1.5 py-0.5", cs.text)}
          >
            <span className={clsx("h-1.5 w-1.5 rounded-full", cs.dot)} />
            {track.custody_state.replace(/_/g, " ")}
          </span>
        )}
      </div>

      {obs.notes && (
        <div className="mt-1.5 text-[10px] text-slate-500">{obs.notes}</div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------
// Panel: latest observation per track, freshest first.
// ------------------------------------------------------------------
export default function CrossSensorPanel() {
  const eoObservations = useStore((s) => s.eoObservations);
  const tracks = useStore((s) => s.tracks);

  // Sort by recency. Tracks without an observation are omitted entirely
  // (the panel is for cued visuals; if no EO has fired there's nothing
  // to render). The CustodyTimeline already covers RF-only state.
  const cards = Object.values(eoObservations)
    .sort((a, b) => +new Date(b.timestamp) - +new Date(a.timestamp))
    .slice(0, 6);

  const trackById: Record<string, UASTrack> = {};
  tracks.forEach((t) => {
    trackById[t.track_id] = t;
  });

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold tracking-tight text-slate-100">
            Cross-sensor cueing
          </h2>
          <span className="rounded-sm bg-white/5 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-slate-400">
            EO · IR
          </span>
        </div>
        <span className="text-[11px] text-slate-500 tnum">
          {cards.length} cued
        </span>
      </div>

      <div className="max-h-[440px] overflow-y-auto p-3">
        {cards.length === 0 ? (
          <div className="p-4 text-center text-xs text-slate-500">
            <div className="mb-1 text-slate-400">No EO frames yet.</div>
            <div>
              When an RF custody track opens at MEDIUM/HIGH threat, the
              tipping camera slews to the sector and a frame appears here.
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            {cards.map((obs) => (
              <EoCard
                key={obs.id}
                obs={obs}
                track={trackById[obs.track_id] ?? null}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
