"""Edge LLM wrapper (Qwen 2.5 1.5B via Ollama).

Talks to a local Ollama server running an instruction-tuned model. Used for:
  - polishing the action cue on intelligence events (LLM brief)
  - generating after-action mission briefs over a window of events
  - parsing natural-language operator queries into structured filters

All calls have graceful fallback: if Ollama is unreachable, return a clean
template-derived string so the demo keeps working with no external deps.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import httpx

OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# Default: llama3.2:1b (1.3 GB) — widely available via Ollama and a good fit
# for an edge device. Users can override via OLLAMA_MODEL. The health() check
# below will auto-fall back to any installed small instruct model if the
# requested one isn't present, so the demo works with whatever is already pulled.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:1b")
# Preferred fallbacks in order of preference (small, instruction-tuned).
OLLAMA_FALLBACK_MODELS = [
    "llama3.2:1b",
    "qwen2.5:1.5b",
    "gemma3:1b",
    "phi3.5:3.8b",
    "llama3.2:3b",
    "llama3.2:latest",
    "gemma3:4b",
]
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT", "20"))


SYSTEM_PROMPT_BRIEF = (
    "You are a tactical RF intelligence assistant for an edge SIGINT system. "
    "You write terse, professional action briefs for an operator. Plain text. "
    "No bullets or markdown. 2-3 short sentences max. No speculation beyond the "
    "evidence provided."
)


SYSTEM_PROMPT_AFTER_ACTION = (
    "You are a tactical RF intelligence analyst writing an after-action summary "
    "for command. Given a list of intelligence events from an edge node during "
    "a defined time window, produce a single concise paragraph (3-5 sentences) "
    "covering: what was observed, what was filtered, what was anomalous, and "
    "the recommended next action. Plain text. No markdown."
)


SYSTEM_PROMPT_NL_QUERY = (
    "You translate operator natural-language queries about a tactical RF "
    "system into a strict JSON filter object with these optional keys: "
    "min_priority (low|medium|high|critical), classification (string), "
    "sector (NE|NW|SW|SE), since_minutes (int), only_anomalies (bool), "
    "synced_only (bool). Return ONLY JSON, no other text. If the query "
    "cannot be parsed, return {}."
)


class EdgeLLM:
    """Async client for the local Ollama server."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: str = OLLAMA_MODEL):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._available: Optional[bool] = None  # cached availability check

    async def health(self) -> bool:
        """Check Ollama availability and auto-select a working model.

        If the requested ``self.model`` isn't installed but another model from
        OLLAMA_FALLBACK_MODELS is, we silently swap to that so the demo works
        with whatever the user already has pulled.
        """
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                if r.status_code != 200:
                    self._available = False
                    return False
                tags = r.json()
                names = [m.get("name", "") for m in tags.get("models", [])]
                # Exact requested model available?
                if any(n == self.model for n in names):
                    self._available = True
                    return True
                # Any installed model whose family matches?
                family = self.model.split(":")[0]
                family_match = next((n for n in names if n.split(":")[0] == family), None)
                if family_match:
                    self.model = family_match
                    self._available = True
                    return True
                # Otherwise walk the fallback list and pick the first installed one.
                for candidate in OLLAMA_FALLBACK_MODELS:
                    if candidate in names:
                        self.model = candidate
                        self._available = True
                        return True
                    # Also match by family in case the user has a tagged variant.
                    cand_family = candidate.split(":")[0]
                    hit = next((n for n in names if n.split(":")[0] == cand_family), None)
                    if hit:
                        self.model = hit
                        self._available = True
                        return True
                self._available = False
                return False
        except Exception:
            self._available = False
            return False

    @property
    def available(self) -> bool:
        # We default to *not available* until a successful health check.
        return bool(self._available)

    async def _generate(self, system: str, user: str, max_tokens: int = 220) -> Optional[str]:
        body = {
            "model": self.model,
            "prompt": user,
            "system": system,
            "stream": False,
            "options": {
                "temperature": 0.4,
                "num_predict": max_tokens,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
                r = await client.post(f"{self.base_url}/api/generate", json=body)
                if r.status_code != 200:
                    return None
                data = r.json()
                return (data.get("response") or "").strip()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    async def brief_for_event(self, event: dict[str, Any]) -> Optional[str]:
        if self._available is False:
            return None
        prompt = (
            "Event JSON:\n"
            f"{json.dumps(event, default=str, indent=2)}\n\n"
            "Write the operator brief now. Reference frequency, classification, OOD score, "
            "any local baseline deviation, and the recommended action. Keep it crisp."
        )
        return await self._generate(SYSTEM_PROMPT_BRIEF, prompt)

    async def after_action_brief(
        self,
        events: list[dict[str, Any]],
        outage_seconds: Optional[float] = None,
    ) -> Optional[str]:
        if self._available is False:
            return None
        header = "Events during the recent operating window (most recent first):"
        outage_line = ""
        if outage_seconds is not None:
            outage_line = f"\nNote: link was disconnected for {outage_seconds:.0f}s during this window."
        prompt = (
            f"{header}{outage_line}\n"
            f"{json.dumps(events[:24], default=str, indent=2)}\n\n"
            "Write the after-action paragraph now."
        )
        return await self._generate(SYSTEM_PROMPT_AFTER_ACTION, prompt, max_tokens=320)

    async def parse_query(self, query: str) -> dict[str, Any]:
        if self._available is False:
            return {}
        out = await self._generate(SYSTEM_PROMPT_NL_QUERY, query, max_tokens=120)
        if not out:
            return {}
        # Try to extract a JSON object even if the model added text.
        try:
            start = out.index("{")
            end = out.rindex("}") + 1
            return json.loads(out[start:end])
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Template fallbacks (used when LLM is unavailable)
# ---------------------------------------------------------------------------

def template_brief(event: dict[str, Any]) -> str:
    cls = event.get("classification", "unknown")
    title = event.get("title", "RF event")
    freq = event.get("evidence", [])
    ood = event.get("ood_score", 0.0)
    rec = event.get("recommended_action", "Continue monitoring.")
    sync = event.get("sync_status", "queued")
    net = event.get("network_state_at_detection", "online")
    return (
        f"{title}. Classification {cls} with OOD {ood:.2f}. "
        f"Network state at detection: {net}; sync status: {sync}. "
        f"Recommended: {rec}"
    )


def template_after_action(events: list[dict[str, Any]], outage_seconds: Optional[float] = None) -> str:
    if not events:
        return "No intelligence events recorded in the recent window."
    n = len(events)
    by_class: dict[str, int] = {}
    n_sync = 0
    for e in events:
        c = e.get("classification", "unknown")
        by_class[c] = by_class.get(c, 0) + 1
        if e.get("sync_status") == "synced":
            n_sync += 1
    leading_class = max(by_class.items(), key=lambda kv: kv[1])[0]
    outage_str = (
        f" Link was offline for {outage_seconds:.0f}s; events were queued locally and synced on reconnect."
        if outage_seconds
        else ""
    )
    return (
        f"During the recent window, the edge node processed {n} intelligence events "
        f"(synced: {n_sync}). Predominant signal family: {leading_class}.{outage_str} "
        "Recommended next action: maintain current sensitivity profile and request visual confirmation on outstanding anomalies."
    )
