"use client";

import { useEffect, useRef } from "react";
import { useStore } from "@/lib/store";
import type { RealSampleMeta, SignalSource } from "@/lib/types";
import clsx from "clsx";

const CANVAS_W = 512;
const CANVAS_H = 256;

// Magma-ish color map (dark blue -> purple -> orange -> yellow)
function colorFor(v: number): [number, number, number] {
  // v in [0, 1]
  const stops: [number, [number, number, number]][] = [
    [0.0, [8, 12, 22]],
    [0.15, [24, 16, 68]],
    [0.35, [70, 20, 110]],
    [0.55, [155, 40, 104]],
    [0.75, [226, 92, 60]],
    [0.9, [247, 178, 36]],
    [1.0, [252, 253, 191]],
  ];
  for (let i = 0; i < stops.length - 1; i++) {
    const [a, ca] = stops[i];
    const [b, cb] = stops[i + 1];
    if (v <= b) {
      const t = (v - a) / Math.max(1e-6, b - a);
      return [
        Math.round(ca[0] + (cb[0] - ca[0]) * t),
        Math.round(ca[1] + (cb[1] - ca[1]) * t),
        Math.round(ca[2] + (cb[2] - ca[2]) * t),
      ];
    }
  }
  return stops[stops.length - 1][1];
}

function priorityColor(priority: string) {
  return {
    critical: "text-accent-red",
    high: "text-accent-amber",
    medium: "text-accent-blue",
    low: "text-slate-400",
  }[priority] ?? "text-slate-400";
}

export default function SpectrogramCanvas() {
  const tick = useStore((s) => s.currentTick);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const spec = tick?.reading.spectrogram_u8;
    if (!spec || spec.length === 0) {
      // Clear canvas to solid panel color
      ctx.fillStyle = "#0b1018";
      ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
      return;
    }

    const rows = spec.length;
    const cols = spec[0].length;
    const img = ctx.createImageData(cols, rows);
    for (let y = 0; y < rows; y++) {
      // Flip Y so low frequencies are at the bottom
      const srcRow = spec[rows - 1 - y];
      for (let x = 0; x < cols; x++) {
        const v = srcRow[x] / 255.0;
        const [r, g, b] = colorFor(v);
        const idx = (y * cols + x) * 4;
        img.data[idx] = r;
        img.data[idx + 1] = g;
        img.data[idx + 2] = b;
        img.data[idx + 3] = 255;
      }
    }
    // Draw scaled up
    const tmp = document.createElement("canvas");
    tmp.width = cols;
    tmp.height = rows;
    tmp.getContext("2d")?.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(tmp, 0, 0, CANVAS_W, CANVAS_H);
  }, [tick]);

  const reading = tick?.reading;
  const cls = tick?.classified;

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-slate-500">
          <span>Live spectrogram</span>
          <SourceBadge source={tick?.source} meta={tick?.source_meta} />
        </div>
        {cls && (
          <div className="flex items-center gap-2 text-xs">
            <span
              className={clsx(
                "rounded-sm px-1.5 py-0.5 font-medium uppercase",
                priorityColor(cls.priority),
                "bg-white/5"
              )}
            >
              {cls.priority}
            </span>
            <span className="text-slate-500">{cls.action}</span>
          </div>
        )}
      </div>

      <canvas
        ref={canvasRef}
        width={CANVAS_W}
        height={CANVAS_H}
        className="w-full rounded border border-panel-700 bg-panel-950"
      />

      <div className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500">class</div>
          <div className="truncate font-mono text-slate-100">
            {cls?.predicted_class ?? "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500">frequency</div>
          <div className="tnum font-mono text-slate-100">
            {reading ? `${reading.center_frequency_mhz.toFixed(1)} MHz` : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500">power</div>
          <div className="tnum font-mono text-slate-100">
            {reading ? `${reading.power_dbm.toFixed(1)} dBm` : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500">bandwidth</div>
          <div className="tnum font-mono text-slate-100">
            {reading ? `${reading.bandwidth_khz.toLocaleString()} kHz` : "—"}
          </div>
        </div>
      </div>

      {/* OOD + confidence bars */}
      {cls && (
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Bar label="Confidence" value={cls.confidence} tone="green" />
          <Bar label="OOD score" value={cls.ood_score} tone={cls.ood_score > 0.5 ? "red" : "cyan"} />
          <Bar label="Baseline deviation" value={cls.baseline_deviation} tone="amber" />
        </div>
      )}

      {cls?.explanation && (
        <div className="mt-3 rounded border border-panel-700 bg-panel-800 p-2 font-mono text-[11px] leading-relaxed text-slate-400">
          {cls.explanation}
        </div>
      )}
    </div>
  );
}

/**
 * Provenance badge: did this spectrogram come from our synthetic generator
 * or from the real DeepSig RadioML 2016.10A I/Q dataset? When real, we also
 * surface the original modulation + SNR so a judge can see exactly what
 * piece of real RF is flowing through the live edge pipeline.
 */
function SourceBadge({
  source,
  meta,
}: {
  source?: SignalSource;
  meta?: RealSampleMeta;
}) {
  if (!source) return null;
  if (source === "real") {
    const detail = meta?.modulation
      ? `${meta.modulation}${typeof meta.snr_db === "number" ? ` @ ${meta.snr_db} dB` : ""}`
      : "RadioML 2016.10A";
    return (
      <span
        title={meta?.threat_label ?? "Real I/Q sample (DeepSig RadioML 2016.10A)"}
        className="rounded-sm border border-accent-cyan/40 bg-accent-cyan/10 px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider text-accent-cyan"
      >
        REAL · {detail}
      </span>
    );
  }
  return (
    <span
      title="Synthetic spectrogram from the local generator"
      className="rounded-sm border border-panel-700 bg-panel-800 px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider text-slate-400"
    >
      SYNTH
    </span>
  );
}

function Bar({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "green" | "red" | "cyan" | "amber";
}) {
  const bg = {
    green: "bg-accent-green",
    red: "bg-accent-red",
    cyan: "bg-accent-cyan",
    amber: "bg-accent-amber",
  }[tone];
  const pct = Math.max(0, Math.min(1, value));
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider text-slate-500">
        <span>{label}</span>
        <span className="tnum text-slate-200">{(pct * 100).toFixed(0)}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-panel-700">
        <div
          className={clsx("h-full transition-all", bg)}
          style={{ width: `${pct * 100}%` }}
        />
      </div>
    </div>
  );
}
