"use client";

import { useState } from "react";
import { useStore } from "@/lib/store";
import { api } from "@/lib/api";
import {
  Wifi,
  WifiOff,
  ArrowUp,
  ArrowDown,
  Minus,
  Download,
  Radio,
  Database,
} from "lucide-react";
import clsx from "clsx";

function Btn({
  onClick,
  disabled,
  children,
  tone = "default",
  className,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
  tone?: "default" | "green" | "red" | "amber" | "cyan";
  className?: string;
}) {
  const toneClass = {
    default: "border-panel-600 hover:border-panel-600 text-slate-300 hover:text-slate-100",
    green: "border-accent-green/30 bg-accent-green/5 text-accent-green hover:bg-accent-green/10",
    red: "border-accent-red/30 bg-accent-red/5 text-accent-red hover:bg-accent-red/10",
    amber: "border-accent-amber/30 bg-accent-amber/5 text-accent-amber hover:bg-accent-amber/10",
    cyan: "border-accent-cyan/30 bg-accent-cyan/5 text-accent-cyan hover:bg-accent-cyan/10",
  }[tone];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        "flex w-full items-center justify-center gap-1.5 rounded border bg-panel-900 px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        toneClass,
        className
      )}
    >
      {children}
    </button>
  );
}

export default function CommandControls() {
  const device = useStore((s) => s.device);
  const [busy, setBusy] = useState(false);

  const isOnline = device?.network_status === "CONNECTED";
  const sens = device?.sensitivity_mode ?? "normal";

  const call = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      console.warn("command failed", e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100 flex items-center gap-2">
          <Radio className="h-4 w-4 text-accent-cyan" /> Command &amp; control
        </h2>
      </div>
      <div className="p-3 space-y-3">
        {/* Network toggle */}
        <div>
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            Link to command
          </div>
          <Btn
            onClick={() => call(() => api.toggleNetwork(!isOnline))}
            disabled={busy}
            tone={isOnline ? "red" : "green"}
          >
            {isOnline ? <WifiOff className="h-3.5 w-3.5" /> : <Wifi className="h-3.5 w-3.5" />}
            {isOnline ? "Disconnect (simulate DDIL)" : "Reconnect & drain queue"}
          </Btn>
        </div>

        {/* Sensitivity */}
        <div>
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            Sensitivity (command-down)
          </div>
          <div className="grid grid-cols-3 gap-1.5">
            <Btn
              onClick={() => call(() => api.setSensitivity("low"))}
              disabled={busy}
              tone={sens === "low" ? "cyan" : "default"}
            >
              <ArrowDown className="h-3 w-3" />
              Low
            </Btn>
            <Btn
              onClick={() => call(() => api.setSensitivity("normal"))}
              disabled={busy}
              tone={sens === "normal" ? "cyan" : "default"}
            >
              <Minus className="h-3 w-3" />
              Normal
            </Btn>
            <Btn
              onClick={() => call(() => api.setSensitivity("high"))}
              disabled={busy}
              tone={sens === "high" ? "amber" : "default"}
            >
              <ArrowUp className="h-3 w-3" />
              High
            </Btn>
          </div>
        </div>

        {/* Watch band quick buttons */}
        <div>
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            Watch band (quick)
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            <Btn onClick={() => call(() => api.watchBand(2400, 2484))} disabled={busy}>
              2.4 GHz (DJI / WiFi)
            </Btn>
            <Btn onClick={() => call(() => api.watchBand(5725, 5850))} disabled={busy}>
              5.8 GHz (swarm)
            </Btn>
          </div>
        </div>

        {/* Real I/Q injection (RadioML 2016.10A) */}
        <div>
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            <Database className="h-3 w-3" />
            Real I/Q mix
            {device?.real_data_available === false && (
              <span
                title="Drop the RML2016.10a_dict_optimized.pkl file into datasets/radioml2016/ to enable real-data injection."
                className="ml-auto rounded-sm border border-panel-700 bg-panel-800 px-1 py-0.5 normal-case tracking-normal text-slate-500"
              >
                dataset missing
              </span>
            )}
            {device?.real_data_available && (
              <span className="ml-auto tnum normal-case tracking-normal text-accent-cyan">
                {Math.round((device?.real_data_mix ?? 0) * 100)}%
              </span>
            )}
          </div>
          <div className="grid grid-cols-4 gap-1.5">
            {[0, 0.25, 0.5, 1].map((m) => {
              const active = Math.abs((device?.real_data_mix ?? 0) - m) < 0.01;
              const label = m === 0 ? "Off" : `${Math.round(m * 100)}%`;
              return (
                <Btn
                  key={m}
                  onClick={() => call(() => api.setRealDataMix(m))}
                  disabled={busy || device?.real_data_available === false}
                  tone={active ? "cyan" : "default"}
                >
                  {label}
                </Btn>
              );
            })}
          </div>
        </div>

        {/* Foundry export */}
        <div>
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            Palantir Foundry export
          </div>
          <a
            href={api.foundryExportUrl}
            className="flex w-full items-center justify-center gap-1.5 rounded border border-accent-blue/30 bg-accent-blue/5 px-3 py-2 text-xs font-medium text-accent-blue transition-colors hover:bg-accent-blue/10"
            target="_blank"
            rel="noreferrer"
          >
            <Download className="h-3.5 w-3.5" />
            Download JSONL bundle
          </a>
        </div>
      </div>
    </div>
  );
}
