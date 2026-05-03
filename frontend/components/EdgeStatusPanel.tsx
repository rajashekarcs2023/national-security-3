"use client";

import { useStore } from "@/lib/store";
import { Cpu, BatteryMedium, Gauge, Signal } from "lucide-react";
import clsx from "clsx";

function Row({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="flex items-center justify-between gap-2 py-1.5">
      <span className="text-[11px] uppercase tracking-wider text-slate-500">{label}</span>
      <span className={clsx("tnum font-mono text-[12px]", tone ?? "text-slate-200")}>{value}</span>
    </div>
  );
}

export default function EdgeStatusPanel() {
  const device = useStore((s) => s.device);
  const baseline = useStore((s) => s.baselineSummary);
  if (!device) return null;
  const model = device.model_summary;

  const netTone =
    device.network_status === "CONNECTED"
      ? "text-accent-green"
      : device.network_status === "DEGRADED"
      ? "text-accent-amber"
      : "text-accent-red";
  const sensTone =
    device.sensitivity_mode === "high"
      ? "text-accent-amber"
      : device.sensitivity_mode === "low"
      ? "text-slate-400"
      : "text-accent-green";

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100 flex items-center gap-2">
          <Cpu className="h-4 w-4 text-accent-cyan" /> Edge node
        </h2>
        <span className="font-mono text-[11px] text-slate-500">v{device.firmware_version}</span>
      </div>
      <div className="grid grid-cols-1 gap-0 divide-y divide-panel-800 px-4 py-2 md:grid-cols-2 md:divide-x md:divide-y-0">
        <div className="pr-4">
          <Row label="Device" value={device.device_id} />
          <Row label="Deployment" value={device.deployment_type.replace(/_/g, " ").toLowerCase()} />
          <Row label="Sensors" value={device.sensors_equipped.join(", ")} />
          <Row label="Network" value={device.network_status} tone={netTone} />
          <Row label="Sensitivity" value={device.sensitivity_mode} tone={sensTone} />
          <Row
            label="Watch band"
            value={
              device.watch_band_mhz
                ? `${device.watch_band_mhz[0]}–${device.watch_band_mhz[1]} MHz`
                : "—"
            }
          />
          <Row
            label="Battery"
            value={
              <span className="flex items-center gap-1">
                <BatteryMedium className="h-3.5 w-3.5 text-slate-500" />
                {device.battery_pct.toFixed(0)}%
              </span>
            }
          />
        </div>
        <div className="pl-0 pt-2 md:pl-4 md:pt-0">
          <Row
            label="Model"
            value={
              <span className="flex items-center gap-1">
                <Gauge className="h-3.5 w-3.5 text-accent-cyan" />
                {model?.loaded ? `${(model.total_params ?? 0).toLocaleString()} params` : "loading"}
              </span>
            }
          />
          <Row
            label="Val acc"
            value={model?.val_acc != null ? `${(model.val_acc * 100).toFixed(1)}%` : "—"}
            tone="text-accent-green"
          />
          <Row
            label="Model size (int8)"
            value={
              model?.size_int8_bytes != null
                ? `${(model.size_int8_bytes / 1024).toFixed(1)} KB`
                : "—"
            }
          />
          <Row label="Active tracks" value={device.active_tracks.toString()} />
          <Row
            label="Baseline"
            value={
              <span className="flex items-center gap-1">
                <Signal className="h-3.5 w-3.5 text-slate-500" />
                {baseline ? `${baseline.n_observed} obs, ${(baseline.anomaly_rate * 100).toFixed(0)}% anom` : "—"}
              </span>
            }
          />
          <Row
            label="Mean power"
            value={baseline?.mean_power_dbm != null ? `${baseline.mean_power_dbm.toFixed(1)} dBm` : "—"}
          />
          <Row label="Queue depth" value={device.sync_queue_depth.toString()} />
        </div>
      </div>
    </div>
  );
}
