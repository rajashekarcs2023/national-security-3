"use client";

import { useStore } from "@/lib/store";
import clsx from "clsx";

// Simple SVG "tactical" radar-style panel showing:
//  - the edge node at center
//  - sector fans
//  - active tracks placed by sector + n_detections radius
//  - friendly emitter profiles badged around the rim
//
// Deliberately lightweight — no Leaflet dep. Still proves the "overlay on
// map" story the mentor asked for (friendly attribution vs unexplained EMS).

const SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] as const;
type Sector = (typeof SECTORS)[number];

function sectorAngle(s: Sector): number {
  // Angles in degrees, 0 = North, clockwise.
  const idx = SECTORS.indexOf(s);
  return idx * 45;
}

function threatColor(level: string): string {
  if (level === "HIGH") return "#f85149";
  if (level === "MEDIUM") return "#e3b341";
  return "#58a6ff";
}

function stateAlpha(state: string): number {
  switch (state) {
    case "TRACKING":
    case "REACQUIRED":
      return 0.95;
    case "VISUAL_LOST_RF_PRESENT":
      return 0.75;
    case "DETECTED":
      return 0.85;
    case "TRACK_LOST":
      return 0.4;
    case "CLEARED":
    case "DISMISSED":
      return 0.25;
    default:
      return 0.6;
  }
}

export default function MapPanel() {
  const device = useStore((s) => s.device);
  const tracks = useStore((s) => s.tracks);

  const R = 200;
  const C = 220;
  const maxDet = Math.max(1, ...tracks.map((t) => t.n_detections));

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">Tactical view</h2>
        <span className="font-mono text-[11px] text-slate-500">
          {device?.site_lat.toFixed(4)}, {device?.site_lon.toFixed(4)}
        </span>
      </div>
      <div className="flex flex-col items-center gap-3 p-4">
        <svg
          viewBox={`0 0 ${C * 2} ${C * 2}`}
          className="w-full max-w-[440px]"
          style={{ aspectRatio: "1 / 1" }}
        >
          <defs>
            <radialGradient id="bgFade" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="#111823" stopOpacity="1" />
              <stop offset="100%" stopColor="#070b10" stopOpacity="1" />
            </radialGradient>
          </defs>
          {/* Background */}
          <rect x={0} y={0} width={C * 2} height={C * 2} fill="url(#bgFade)" />

          {/* Range rings */}
          {[0.25, 0.5, 0.75, 1.0].map((f) => (
            <circle
              key={f}
              cx={C}
              cy={C}
              r={R * f}
              fill="none"
              stroke="#1a2433"
              strokeDasharray="3 4"
            />
          ))}

          {/* Cardinal sector lines */}
          {SECTORS.map((s) => {
            const a = (sectorAngle(s) - 90) * (Math.PI / 180);
            const x2 = C + R * Math.cos(a);
            const y2 = C + R * Math.sin(a);
            return (
              <line
                key={s}
                x1={C}
                y1={C}
                x2={x2}
                y2={y2}
                stroke="#1a2433"
                strokeWidth={s === "N" || s === "S" || s === "E" || s === "W" ? 1 : 0.5}
              />
            );
          })}

          {/* Cardinal labels */}
          {SECTORS.map((s) => {
            const a = (sectorAngle(s) - 90) * (Math.PI / 180);
            const rr = R + 14;
            const x = C + rr * Math.cos(a);
            const y = C + rr * Math.sin(a);
            return (
              <text
                key={`lbl-${s}`}
                x={x}
                y={y}
                fill="#4b5563"
                fontSize={10}
                fontFamily="ui-monospace, monospace"
                textAnchor="middle"
                dominantBaseline="middle"
              >
                {s}
              </text>
            );
          })}

          {/* Tracks */}
          {tracks.map((t, i) => {
            const a = (sectorAngle((t.sector as Sector) ?? "N") - 90) * (Math.PI / 180);
            const dist = 0.35 + 0.55 * (t.n_detections / maxDet);
            const x = C + R * dist * Math.cos(a);
            const y = C + R * dist * Math.sin(a);
            const color = threatColor(t.threat_level);
            const opa = stateAlpha(t.custody_state);
            return (
              <g key={t.track_id} opacity={opa}>
                <circle cx={x} cy={y} r={9} fill={color} fillOpacity={0.15} />
                <circle cx={x} cy={y} r={5} fill={color} />
                <text
                  x={x + 8}
                  y={y - 6}
                  fill="#c9d1d9"
                  fontSize={9}
                  fontFamily="ui-monospace, monospace"
                >
                  {t.track_id.split("-").slice(0, 3).join("-")}
                </text>
                <text
                  x={x + 8}
                  y={y + 5}
                  fill="#8b949e"
                  fontSize={8}
                  fontFamily="ui-monospace, monospace"
                >
                  {t.custody_state.replace(/_/g, " ")} · {t.n_detections}x
                </text>
              </g>
            );
          })}

          {/* Edge node marker */}
          <circle cx={C} cy={C} r={9} fill="#39d0d8" fillOpacity={0.2} />
          <circle cx={C} cy={C} r={4} fill="#39d0d8" />
          <text
            x={C}
            y={C + 18}
            fill="#39d0d8"
            fontSize={10}
            fontFamily="ui-monospace, monospace"
            textAnchor="middle"
          >
            {device?.device_id ?? "EDGE"}
          </text>

          {/* Sweeping range pulse */}
          <circle cx={C} cy={C} r={R * 0.98} fill="none" stroke="#39d0d8" strokeOpacity={0.15} />
        </svg>

        {/* Friendly attribution legend */}
        <div className="w-full space-y-1 rounded border border-panel-700 bg-panel-950/50 p-2 text-[11px]">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">
            Friendly attribution
          </div>
          <div className="flex flex-wrap gap-2">
            <LegendChip color="#3fb950" label="Blue-2 VHF (144-148)" />
            <LegendChip color="#3fb950" label="Blue-1 UHF (225-400)" />
            <LegendChip color="#58a6ff" label="Commercial WiFi (2.4)" />
            <LegendChip color="#f85149" label="DJI 2.4 (control)" />
            <LegendChip color="#f85149" label="DJI 5.8 (swarm)" />
          </div>
        </div>
      </div>
    </div>
  );
}

function LegendChip({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5 rounded-sm bg-panel-800 px-1.5 py-0.5 text-slate-300">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </div>
  );
}
