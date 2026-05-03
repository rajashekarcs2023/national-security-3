"use client";

import { useMemo } from "react";

import { useStore } from "@/lib/store";
import type {
  AttributionResult,
  AttributionVerdict,
  BlueForceUnit,
  PersistentEmitter,
  SensorNode,
  TdoaSolution,
} from "@/lib/types";

// Phase E — Geo map.
// Renders the actual lat/lon-projected battlespace:
//   * 4 RF sensor nodes (Alpha/Bravo/Charlie/Delta)
//   * 3 blue-force units with callsigns
//   * up to 12 most-recent TDOA fixes, drawn as filled dots with a CEP
//     ring around each one (sized to the fix's CEP in metres)
//   * persistent unknown emitter clusters as larger pulsating circles
// Lightweight SVG only — same approach as MapPanel, no Leaflet dep.

const SVG_W = 460;
const SVG_H = 360;

// Approximate "metres per degree" near 34°N. Used to scale CEP circles.
const M_PER_DEG_LAT = 111_320;
const M_PER_DEG_LON_AT_34N = 92_350;

function project(
  lat: number,
  lon: number,
  bbox: { minLat: number; maxLat: number; minLon: number; maxLon: number },
): { x: number; y: number } {
  // Equirectangular projection — good enough for ~10 km AO.
  const x = ((lon - bbox.minLon) / (bbox.maxLon - bbox.minLon)) * SVG_W;
  const y = SVG_H - ((lat - bbox.minLat) / (bbox.maxLat - bbox.minLat)) * SVG_H;
  return { x, y };
}

function metersToPxRadius(
  meters: number,
  bbox: { minLat: number; maxLat: number; minLon: number; maxLon: number },
): number {
  // Use lon span for x-scale (longer baseline at this latitude).
  const lonRangeM = (bbox.maxLon - bbox.minLon) * M_PER_DEG_LON_AT_34N;
  return (meters / lonRangeM) * SVG_W;
}

function verdictColor(v?: AttributionVerdict | null): string {
  switch (v) {
    case "BLUE_ATTRIBUTED":
      return "#3fb950";
    case "RED_KNOWN":
      return "#f85149";
    case "AMBIGUOUS":
      return "#e3b341";
    case "UNEXPLAINED":
      return "#a371f7";
    default:
      return "#58a6ff";
  }
}

export default function GeoMapPanel() {
  const sensors = useStore((s) => s.sensorArray);
  const blueForce = useStore((s) => s.blueForce);
  const tdoaRecent = useStore((s) => s.tdoaRecent);
  const persistent = useStore((s) => s.persistentEmitters);
  const attributionByEvent = useStore((s) => s.attributionByEvent);

  // Compute bbox from all known points so the projection always frames everything.
  const bbox = useMemo(() => {
    const lats: number[] = [];
    const lons: number[] = [];
    sensors.forEach((s) => {
      lats.push(s.lat);
      lons.push(s.lon);
    });
    blueForce.forEach((u) => {
      lats.push(u.lat);
      lons.push(u.lon);
    });
    Object.values(persistent).forEach((p) => {
      lats.push(p.lat);
      lons.push(p.lon);
    });
    tdoaRecent.slice(0, 30).forEach((t) => {
      lats.push(t.lat);
      lons.push(t.lon);
    });
    if (lats.length === 0) {
      // Fallback to LA-area centroid.
      return {
        minLat: 34.02,
        maxLat: 34.10,
        minLon: -118.27,
        maxLon: -118.16,
      };
    }
    const minLat = Math.min(...lats);
    const maxLat = Math.max(...lats);
    const minLon = Math.min(...lons);
    const maxLon = Math.max(...lons);
    // 5% padding.
    const padLat = Math.max(0.005, (maxLat - minLat) * 0.1);
    const padLon = Math.max(0.005, (maxLon - minLon) * 0.1);
    return {
      minLat: minLat - padLat,
      maxLat: maxLat + padLat,
      minLon: minLon - padLon,
      maxLon: maxLon + padLon,
    };
  }, [sensors, blueForce, persistent, tdoaRecent]);

  const recentFixes = tdoaRecent.slice(0, 12);

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100">
          Geo battlespace
        </h2>
        <span className="font-mono text-[11px] text-slate-500">
          {sensors.length} sensors · {blueForce.length} blue · {recentFixes.length} fixes
        </span>
      </div>

      <div className="p-3">
        <svg
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          className="w-full"
          style={{ aspectRatio: `${SVG_W} / ${SVG_H}` }}
        >
          <rect x={0} y={0} width={SVG_W} height={SVG_H} fill="#0a1018" />
          {/* Sub-grid */}
          {Array.from({ length: 8 }).map((_, i) => (
            <line
              key={`gx-${i}`}
              x1={(i * SVG_W) / 8}
              x2={(i * SVG_W) / 8}
              y1={0}
              y2={SVG_H}
              stroke="#15202b"
              strokeDasharray="2 4"
            />
          ))}
          {Array.from({ length: 6 }).map((_, i) => (
            <line
              key={`gy-${i}`}
              x1={0}
              x2={SVG_W}
              y1={(i * SVG_H) / 6}
              y2={(i * SVG_H) / 6}
              stroke="#15202b"
              strokeDasharray="2 4"
            />
          ))}

          {/* Persistent unknown emitter clusters — pulse + label */}
          {Object.values(persistent).map((p) => (
            <PersistentMarker key={p.id} cluster={p} bbox={bbox} />
          ))}

          {/* Recent TDOA fixes with CEP rings */}
          {recentFixes.map((t, i) => (
            <FixMarker
              key={`${t.event_id ?? i}-${i}`}
              fix={t}
              bbox={bbox}
              attribution={t.event_id ? attributionByEvent[t.event_id] : undefined}
              age={i / Math.max(1, recentFixes.length - 1)}
            />
          ))}

          {/* Sensors — diamond shape */}
          {sensors.map((s) => (
            <SensorMarker key={s.id} sensor={s} bbox={bbox} />
          ))}

          {/* Blue force — square + callsign */}
          {blueForce.map((u) => (
            <BlueMarker key={u.unit_id} unit={u} bbox={bbox} />
          ))}
        </svg>

        <Legend />
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
function SensorMarker({
  sensor,
  bbox,
}: {
  sensor: SensorNode;
  bbox: { minLat: number; maxLat: number; minLon: number; maxLon: number };
}) {
  const { x, y } = project(sensor.lat, sensor.lon, bbox);
  const color = sensor.status === "online" ? "#39d0d8" : "#6e7681";
  return (
    <g>
      {/* Diamond */}
      <polygon
        points={`${x},${y - 7} ${x + 6},${y} ${x},${y + 7} ${x - 6},${y}`}
        fill={color}
        fillOpacity={0.25}
        stroke={color}
        strokeWidth={1.5}
      />
      <text
        x={x + 9}
        y={y - 4}
        fill={color}
        fontSize={10}
        fontFamily="ui-monospace, monospace"
      >
        {sensor.id}
      </text>
    </g>
  );
}

// ----------------------------------------------------------------------
function BlueMarker({
  unit,
  bbox,
}: {
  unit: BlueForceUnit;
  bbox: { minLat: number; maxLat: number; minLon: number; maxLon: number };
}) {
  const { x, y } = project(unit.lat, unit.lon, bbox);
  return (
    <g>
      <rect
        x={x - 6}
        y={y - 6}
        width={12}
        height={12}
        fill="#3fb950"
        fillOpacity={0.2}
        stroke="#3fb950"
        strokeWidth={1.5}
      />
      <circle cx={x} cy={y} r={2} fill="#3fb950" />
      <text
        x={x + 9}
        y={y + 3}
        fill="#3fb950"
        fontSize={10}
        fontFamily="ui-monospace, monospace"
      >
        {unit.callsign}
      </text>
    </g>
  );
}

// ----------------------------------------------------------------------
function FixMarker({
  fix,
  bbox,
  attribution,
  age,
}: {
  fix: TdoaSolution;
  bbox: { minLat: number; maxLat: number; minLon: number; maxLon: number };
  attribution?: AttributionResult;
  age: number;
}) {
  const { x, y } = project(fix.lat, fix.lon, bbox);
  const cepPx = Math.max(2, metersToPxRadius(fix.cep_m, bbox));
  const color = verdictColor(attribution?.verdict);
  // Older fixes fade. Index 0 (newest) → opacity 0.95; oldest → 0.25.
  const opacity = 0.95 - 0.7 * age;
  return (
    <g opacity={opacity}>
      <circle
        cx={x}
        cy={y}
        r={cepPx}
        fill={color}
        fillOpacity={0.08}
        stroke={color}
        strokeOpacity={0.55}
        strokeWidth={1}
      />
      <circle cx={x} cy={y} r={3} fill={color} />
    </g>
  );
}

// ----------------------------------------------------------------------
function PersistentMarker({
  cluster,
  bbox,
}: {
  cluster: PersistentEmitter;
  bbox: { minLat: number; maxLat: number; minLon: number; maxLon: number };
}) {
  const { x, y } = project(cluster.lat, cluster.lon, bbox);
  const r = Math.max(8, metersToPxRadius(cluster.radius_m, bbox));
  const color = cluster.priority === "high" ? "#f85149" : "#e3b341";
  return (
    <g>
      <circle
        cx={x}
        cy={y}
        r={r}
        fill={color}
        fillOpacity={0.12}
        stroke={color}
        strokeWidth={1.5}
        strokeDasharray="3 3"
      >
        <animate
          attributeName="r"
          values={`${r};${r * 1.2};${r}`}
          dur="2s"
          repeatCount="indefinite"
        />
        <animate
          attributeName="stroke-opacity"
          values="0.4;1.0;0.4"
          dur="2s"
          repeatCount="indefinite"
        />
      </circle>
      <text
        x={x + r + 4}
        y={y + 3}
        fill={color}
        fontSize={9}
        fontFamily="ui-monospace, monospace"
      >
        {cluster.n_detections}× unknown
      </text>
    </g>
  );
}

// ----------------------------------------------------------------------
function Legend() {
  return (
    <div className="mt-3 grid grid-cols-2 gap-1 text-[10px]">
      <LegendChip color="#39d0d8" symbol="◆" label="RF sensor" />
      <LegendChip color="#3fb950" symbol="■" label="Blue unit" />
      <LegendChip color="#3fb950" symbol="●" label="BLUE_ATTRIBUTED fix" />
      <LegendChip color="#f85149" symbol="●" label="RED_KNOWN fix" />
      <LegendChip color="#e3b341" symbol="●" label="AMBIGUOUS fix" />
      <LegendChip color="#a371f7" symbol="●" label="UNEXPLAINED fix" />
    </div>
  );
}

function LegendChip({
  color,
  symbol,
  label,
}: {
  color: string;
  symbol: string;
  label: string;
}) {
  return (
    <div className="flex items-center gap-1.5 rounded-sm bg-panel-800/60 px-1.5 py-0.5 text-slate-300">
      <span style={{ color }}>{symbol}</span>
      <span>{label}</span>
    </div>
  );
}
