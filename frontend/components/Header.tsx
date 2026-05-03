"use client";

import { useStore } from "@/lib/store";
import { Radio, Wifi, WifiOff, AlertCircle } from "lucide-react";
import clsx from "clsx";

export default function Header() {
  const device = useStore((s) => s.device);
  const wsConnected = useStore((s) => s.wsConnected);
  const scenarioActive = useStore((s) => s.scenarioActive);

  const networkStatus = device?.network_status ?? "DISCONNECTED";
  const statusColor =
    networkStatus === "CONNECTED"
      ? "text-accent-green"
      : networkStatus === "DEGRADED"
      ? "text-accent-amber"
      : "text-accent-red";

  return (
    <header className="sticky top-0 z-30 border-b border-panel-700 bg-panel-950/90 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1920px] items-center justify-between px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-gradient-to-br from-accent-cyan/30 to-accent-blue/10 ring-1 ring-accent-cyan/30">
            <Radio className="h-5 w-5 text-accent-cyan" />
          </div>
          <div>
            <h1 className="text-base font-semibold tracking-tight text-slate-100">
              SpectrumCustody <span className="text-slate-500">//</span> Edge Command
            </h1>
            <div className="text-xs text-slate-500">
              {device?.device_id ?? "—"} &middot; {device?.site_id ?? "—"}
              {device?.site_lat != null && (
                <span className="ml-2 text-slate-600">
                  ({device.site_lat.toFixed(4)}, {device.site_lon.toFixed(4)})
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-4 text-xs">
          {scenarioActive && (
            <div className="flex items-center gap-1.5 rounded-md border border-accent-violet/40 bg-accent-violet/10 px-2.5 py-1 text-accent-violet">
              <AlertCircle className="h-3.5 w-3.5" />
              SCENARIO ACTIVE
            </div>
          )}

          {/* Model summary */}
          <div className="hidden items-center gap-3 text-slate-400 md:flex">
            {device?.model_summary?.loaded ? (
              <>
                <div>
                  <span className="text-slate-500">model </span>
                  <span className="tnum text-slate-200">
                    {Math.round((device.model_summary.total_params ?? 0) / 1000)}K params
                  </span>
                </div>
                <div>
                  <span className="text-slate-500">val </span>
                  <span className="tnum text-accent-green">
                    {((device.model_summary.val_acc ?? 0) * 100).toFixed(1)}%
                  </span>
                </div>
              </>
            ) : (
              <span>model: loading</span>
            )}
          </div>

          {/* Network status pill */}
          <div
            className={clsx(
              "flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs font-medium",
              networkStatus === "CONNECTED"
                ? "border-accent-green/40 bg-accent-green/10"
                : "border-accent-red/40 bg-accent-red/10"
            )}
          >
            {networkStatus === "CONNECTED" ? (
              <Wifi className={clsx("h-3.5 w-3.5", statusColor)} />
            ) : (
              <WifiOff className={clsx("h-3.5 w-3.5", statusColor)} />
            )}
            <span className={statusColor}>{networkStatus}</span>
            <span
              className={clsx(
                "h-1.5 w-1.5 rounded-full animate-pulse-dot",
                networkStatus === "CONNECTED" ? "bg-accent-green" : "bg-accent-red"
              )}
            />
          </div>

          {/* WS status */}
          <div className="text-xs text-slate-500">
            ws {wsConnected ? (
              <span className="text-accent-green">●</span>
            ) : (
              <span className="text-accent-red">●</span>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}
