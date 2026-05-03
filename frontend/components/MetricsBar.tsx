"use client";

import { useStore } from "@/lib/store";
import { Activity, Filter, Upload, HardDrive, Radio, Database } from "lucide-react";

function Card({
  icon,
  label,
  value,
  sub,
  tone = "default",
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
  tone?: "default" | "green" | "amber" | "red" | "cyan";
}) {
  const toneClasses = {
    default: "text-slate-100",
    green: "text-accent-green",
    amber: "text-accent-amber",
    red: "text-accent-red",
    cyan: "text-accent-cyan",
  }[tone];

  return (
    <div className="flex items-center gap-3 rounded-md border border-panel-700 bg-panel-900 px-4 py-3">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-panel-800 text-slate-400">
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
        <div className={`tnum truncate text-lg font-semibold leading-tight ${toneClasses}`}>
          {value}
        </div>
        {sub && <div className="truncate text-[11px] text-slate-500">{sub}</div>}
      </div>
    </div>
  );
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export default function MetricsBar() {
  const device = useStore((s) => s.device);
  const queueDepth = useStore((s) => s.queueDepth);

  const total = device?.total_readings_processed ?? 0;
  const filtered = device?.total_filtered_local ?? 0;
  const synced = device?.total_events_synced ?? 0;
  const bytesSaved = device?.bytes_saved_at_edge ?? 0;
  const bytesSynced = device?.bytes_actually_synced ?? 0;
  const filteredPct = total > 0 ? ((filtered / total) * 100).toFixed(0) : "0";
  const totalBytes = bytesSaved + bytesSynced;
  const savedPct = totalBytes > 0 ? ((bytesSaved / totalBytes) * 100).toFixed(1) : "0";

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
      <Card
        icon={<Radio className="h-4 w-4" />}
        label="Readings processed"
        value={total.toLocaleString()}
        sub="live"
        tone="cyan"
      />
      <Card
        icon={<Filter className="h-4 w-4" />}
        label="Filtered locally"
        value={filtered.toLocaleString()}
        sub={`${filteredPct}% of readings`}
      />
      <Card
        icon={<Upload className="h-4 w-4" />}
        label="Events synced"
        value={synced.toLocaleString()}
        sub="to command"
        tone="green"
      />
      <Card
        icon={<Database className="h-4 w-4" />}
        label="Sync queue"
        value={queueDepth.toString()}
        sub={queueDepth > 0 ? "offline pending" : "empty"}
        tone={queueDepth > 0 ? "amber" : "default"}
      />
      <Card
        icon={<HardDrive className="h-4 w-4" />}
        label="Saved at edge"
        value={fmtBytes(bytesSaved)}
        sub={`vs ${fmtBytes(bytesSynced)} synced`}
        tone="green"
      />
      <Card
        icon={<Activity className="h-4 w-4" />}
        label="Data reduction"
        value={totalBytes > 0 ? `${savedPct}%` : "—"}
        sub="raw kept at edge"
        tone="green"
      />
    </div>
  );
}
