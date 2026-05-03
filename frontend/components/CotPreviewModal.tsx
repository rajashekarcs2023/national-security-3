"use client";

// CotPreviewModal — Phase B
// Shows the wire-format CoT 2.0 XML for an intelligence event before
// publishing to ATAK. Operator can override stale_seconds, copy the
// XML, or hit Publish to broadcast.

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { IntelligenceEvent } from "@/lib/types";
import { Copy, Send, X } from "lucide-react";
import clsx from "clsx";

interface Props {
  event: IntelligenceEvent;
  onClose: () => void;
}

export default function CotPreviewModal({ event, onClose }: Props) {
  const [stale, setStale] = useState<number>(60);
  const [loading, setLoading] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [xml, setXml] = useState<string>("");
  const [cot, setCot] = useState<Record<string, any> | null>(null);
  const [published, setPublished] = useState(false);
  const [copied, setCopied] = useState(false);

  // Fetch preview whenever stale changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .cotPreview(event.id, stale)
      .then((r) => {
        if (cancelled) return;
        setXml(r.xml);
        setCot(r.cot_dict);
      })
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [event.id, stale]);

  const onPublish = async () => {
    setPublishing(true);
    setError(null);
    try {
      await api.cotPublish(event.id, { stale_seconds: stale, transport: "broadcast" });
      setPublished(true);
      setTimeout(onClose, 800);
    } catch (e) {
      setError(String(e));
    } finally {
      setPublishing(false);
    }
  };

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(xml);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore
    }
  };

  const sidc = cot?.detail?._spectrumcustody?.sidc as string | undefined;
  const cotType = cot?.type as string | undefined;
  const callsign = cot?.detail?.contact?.callsign as string | undefined;
  const iconName = cot?.detail?._spectrumcustody?.icon_name as string | undefined;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="w-full max-w-3xl overflow-hidden rounded-md border border-panel-700 bg-panel-900 shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-panel-700 px-4 py-2.5">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">Publish to ATAK — CoT 2.0 preview</h2>
            <div className="mt-0.5 text-[10px] text-slate-500">
              {event.title} · {event.id}
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 hover:bg-white/5 hover:text-slate-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Summary chips */}
        <div className="flex flex-wrap items-center gap-2 border-b border-panel-700 px-4 py-2 text-[11px]">
          {cotType && (
            <span className="rounded-sm bg-accent-red/10 px-1.5 py-0.5 font-mono text-accent-red">
              {cotType}
            </span>
          )}
          {sidc && (
            <span className="rounded-sm bg-white/5 px-1.5 py-0.5 font-mono text-slate-300">
              SIDC {sidc}
            </span>
          )}
          {callsign && (
            <span className="rounded-sm bg-white/5 px-1.5 py-0.5 font-mono text-slate-300">
              {callsign}
            </span>
          )}
          {iconName && <span className="text-slate-500">{iconName}</span>}
          <span className="ml-auto text-slate-500">
            stale in {stale}s
          </span>
        </div>

        {/* Stale slider */}
        <div className="flex items-center gap-3 border-b border-panel-700 px-4 py-2.5">
          <label className="text-[11px] uppercase tracking-wider text-slate-500">stale_seconds</label>
          <input
            type="range"
            min={10}
            max={600}
            step={5}
            value={stale}
            onChange={(e) => setStale(parseInt(e.target.value, 10))}
            className="flex-1 accent-accent-red"
            disabled={publishing}
          />
          <span className="w-12 text-right font-mono text-xs text-slate-300">{stale}s</span>
        </div>

        {/* XML body */}
        <div className="max-h-[420px] overflow-y-auto bg-panel-950 p-3">
          {loading ? (
            <div className="p-6 text-center text-xs text-slate-500">Building CoT preview...</div>
          ) : error ? (
            <div className="rounded border border-accent-red/40 bg-accent-red/5 p-3 text-xs text-accent-red">
              {error}
            </div>
          ) : (
            <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-slate-300">
              {formatXml(xml)}
            </pre>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-panel-700 px-4 py-2.5">
          <button
            onClick={onCopy}
            disabled={!xml || publishing}
            className="flex items-center gap-1.5 rounded border border-panel-600 bg-panel-900 px-2.5 py-1.5 text-[11px] text-slate-300 hover:border-accent-cyan/50 hover:text-accent-cyan disabled:opacity-40"
          >
            <Copy className="h-3.5 w-3.5" />
            {copied ? "Copied!" : "Copy XML"}
          </button>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              disabled={publishing}
              className="rounded border border-panel-600 bg-panel-900 px-2.5 py-1.5 text-[11px] text-slate-400 hover:text-slate-200 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              onClick={onPublish}
              disabled={publishing || loading || !xml}
              className={clsx(
                "flex items-center gap-1.5 rounded border px-3 py-1.5 text-[11px] font-medium",
                published
                  ? "border-accent-green/40 bg-accent-green/10 text-accent-green"
                  : "border-accent-red/40 bg-accent-red/10 text-accent-red hover:bg-accent-red/20",
                "disabled:cursor-not-allowed disabled:opacity-40",
              )}
            >
              <Send className="h-3.5 w-3.5" />
              {published ? "Published" : publishing ? "Publishing..." : "Publish to ATAK"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Cosmetic XML pretty-print — splits on `>` boundaries and indents
// nested elements. Doesn't reparse — purely visual.
function formatXml(xml: string): string {
  if (!xml) return "";
  let depth = 0;
  const out: string[] = [];
  const tokens = xml.replace(/></g, ">\n<").split("\n");
  for (const tok of tokens) {
    const t = tok.trim();
    if (!t) continue;
    const isClose = /^<\//.test(t);
    const isSelfClose = /\/>$/.test(t) || /^<\?/.test(t);
    if (isClose) depth = Math.max(0, depth - 1);
    out.push("  ".repeat(depth) + t);
    if (!isClose && !isSelfClose && /^</.test(t)) depth += 1;
  }
  return out.join("\n");
}
