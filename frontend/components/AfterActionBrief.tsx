"use client";

import { useState } from "react";
import { useStore } from "@/lib/store";
import { api } from "@/lib/api";
import { FileText, Search, Sparkles } from "lucide-react";

export default function AfterActionBrief() {
  const lastBrief = useStore((s) => s.lastBrief);
  const [generating, setGenerating] = useState(false);
  const [source, setSource] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [queryResult, setQueryResult] = useState<string | null>(null);

  const gen = async () => {
    setGenerating(true);
    try {
      const r = await api.llmBrief();
      setSource(r.source);
    } catch (e) {
      console.warn(e);
    } finally {
      setGenerating(false);
    }
  };

  const runQuery = async () => {
    if (!query.trim()) return;
    setQueryResult("...");
    try {
      const r = await api.llmQuery(query);
      setQueryResult(
        `[${r.source}] ${Object.keys(r.filter).length > 0 ? JSON.stringify(r.filter, null, 2) : "(no filter extracted)"}`
      );
    } catch (e) {
      setQueryResult(String(e));
    }
  };

  return (
    <div className="rounded-md border border-panel-700 bg-panel-900">
      <div className="border-b border-panel-700 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-tight text-slate-100 flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-accent-cyan" /> Edge LLM
        </h2>
      </div>
      <div className="space-y-3 p-3">
        {/* After-action brief */}
        <div>
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            After-action brief
          </div>
          <button
            onClick={gen}
            disabled={generating}
            className="flex w-full items-center justify-center gap-1.5 rounded border border-accent-cyan/30 bg-accent-cyan/5 px-3 py-2 text-xs font-medium text-accent-cyan hover:bg-accent-cyan/10 disabled:opacity-40"
          >
            <FileText className="h-3.5 w-3.5" />
            {generating ? "Generating..." : "Generate brief over recent events"}
          </button>
          {lastBrief && (
            <div className="mt-2 rounded border border-panel-700 bg-panel-950/60 p-2 text-[11px] leading-relaxed text-slate-200">
              {source && (
                <div className="mb-1 text-[9px] uppercase tracking-wider text-accent-cyan">
                  source: {source}
                </div>
              )}
              {lastBrief}
            </div>
          )}
        </div>

        {/* NL query */}
        <div>
          <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
            Natural-language query
          </div>
          <div className="flex gap-1.5">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runQuery()}
              placeholder={`e.g. "only critical drone events in last 10 min"`}
              className="min-w-0 flex-1 rounded border border-panel-700 bg-panel-950 px-2 py-1.5 text-[11px] text-slate-200 placeholder-slate-600 outline-none focus:border-accent-cyan/40"
            />
            <button
              onClick={runQuery}
              className="rounded border border-panel-600 bg-panel-900 px-2 text-slate-300 hover:text-slate-100"
              aria-label="Run query"
            >
              <Search className="h-3.5 w-3.5" />
            </button>
          </div>
          {queryResult && (
            <pre className="mt-2 max-h-32 overflow-auto rounded border border-panel-700 bg-panel-950/60 p-2 font-mono text-[10px] text-slate-300">
              {queryResult}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
