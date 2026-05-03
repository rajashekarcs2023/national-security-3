"use client";

// CotPublicationsPanel — Phase B
// Rolling list of CoT 2.0 messages this dashboard has published to ATAK.
// Each row shows the affiliation-coded callsign, type, SIDC, and a copy
// button for the wire bytes. Click a row to expand the formatted XML
// inline. Empty state explains why nothing's there yet.

import { useState } from "react";
import { useStore } from "@/lib/store";
import type { CotPublication } from "@/lib/types";
import { Copy, Send } from "lucide-react";
import clsx from "clsx";

function affiliationStyle(cotType: string) {
  // Second character of the CoT type cosmology = affiliation.
  // a-h-... (hostile), a-f-... (friendly), a-s-... (suspect),
  // a-u-... (unknown), a-n-... (neutral).
  const aff = cotType.split("-")[1];
  switch (aff) {
    case "h":
      return { label: "HOSTILE", text: "text-accent-red", bg: "bg-accent-red/10", border: "border-accent-red/40" };
    case "s":
      return { label: "SUSPECT", text: "text-accent-amber", bg: "bg-accent-amber/10", border: "border-accent-amber/40" };
    case "u":
      return { label: "UNKNOWN", text: "text-slate-300", bg: "bg-white/5", border: "border-panel-600" };
    case "f":
      return { label: "FRIEND", text: "text-accent-green", bg: "bg-accent-green/10", border: "border-accent-green/40" };
    case "n":
      return { label: "NEUTRAL", text: "text-accent-cyan", bg: "bg-accent-cyan/10", border: "border-accent-cyan/40" };
    default:
      return { label: aff?.toUpperCase() || "?", text: "text-slate-400", bg: "bg-white/5", border: "border-panel-600" };
  }
}

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 1000) return "now";
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  return `${Math.floor(ms / 3_600_000)}h ago`;
}

function PubRow({ p }: { p: CotPublication }) {
  const aff = affiliationStyle(p.cot_type);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const onCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(p.xml);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {}
  };

  return (
    <div className={clsx("rounded-md border p-2.5", aff.border, aff.bg)}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start justify-between gap-2 text-left"
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span
              className={clsx(
                "rounded-sm px-1.5 py-0.5 text-[9px] font-semibold tracking-wide",
                aff.text,
                "bg-white/5",
              )}
            >
              {aff.label}
            </span>
            <span className="truncate text-xs font-medium text-slate-100">{p.callsign}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-slate-500">
            <span className="font-mono">{p.cot_type}</span>
            <span>·</span>
            <span className="font-mono">{p.sidc}</span>
            <span>·</span>
            <span>stale {p.stale_seconds}s</span>
            <span>·</span>
            <span>{timeAgo(p.timestamp)}</span>
          </div>
          <div className="mt-1 truncate text-[10px] text-slate-500">{p.icon_name}</div>
        </div>
        <button
          type="button"
          onClick={onCopy}
          title="Copy CoT XML"
          className="rounded border border-panel-600 bg-panel-900 px-1.5 py-1 text-[10px] text-slate-400 hover:border-accent-cyan/50 hover:text-accent-cyan"
        >
          <Copy className="h-3 w-3" />
          {copied && <span className="ml-1">Copied</span>}
        </button>
      </button>
      {open && (
        <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-all rounded border border-panel-700 bg-panel-950 p-2 font-mono text-[10px] leading-relaxed text-slate-300">
          {p.xml}
        </pre>
      )}
    </div>
  );
}

export default function CotPublicationsPanel() {
  const pubs = useStore((s) => s.cotPublications);

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
        <h2 className="flex items-center gap-1.5 text-sm font-semibold tracking-tight text-slate-100">
          <Send className="h-3.5 w-3.5 text-accent-cyan" /> CoT broadcasts
        </h2>
        <span className="text-[10px] text-slate-500 tnum">{pubs.length} this session</span>
      </div>
      <div className="max-h-[400px] space-y-2 overflow-y-auto p-3">
        {pubs.length === 0 ? (
          <div className="rounded border border-dashed border-panel-700 p-4 text-center text-[11px] leading-relaxed text-slate-500">
            <div>No CoT messages published yet.</div>
            <div className="mt-1 text-slate-600">
              Click <span className="font-medium text-slate-400">Publish to ATAK</span> on any
              intelligence event to broadcast a CoT 2.0 message with MIL-STD-2525C symbology.
            </div>
          </div>
        ) : (
          pubs.map((p) => <PubRow key={p.id} p={p} />)
        )}
      </div>
    </div>
  );
}
