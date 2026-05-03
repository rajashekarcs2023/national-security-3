"""DDIL offline queue + sync manager.

Intelligence events accumulate locally when the network is down. When the
network returns, queued events drain in priority order to the (simulated)
command layer.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from app.schemas import IntelligenceEvent


PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class OfflineQueue:
    """A priority-aware FIFO for offline events."""

    def __init__(self, queue: deque[IntelligenceEvent]):
        self._q = queue

    @property
    def size(self) -> int:
        return len(self._q)

    def enqueue(self, event: IntelligenceEvent) -> None:
        event.sync_status = "queued"
        self._q.append(event)

    def drain(self) -> list[IntelligenceEvent]:
        """Return all queued events sorted by priority + age."""
        items = list(self._q)
        self._q.clear()
        items.sort(key=lambda e: (PRIORITY_ORDER.get(e.priority, 9), e.timestamp))
        for e in items:
            e.sync_status = "synced"
        return items

    def peek(self) -> list[IntelligenceEvent]:
        return list(self._q)

    def queue_summary(self) -> dict:
        if not self._q:
            return {"depth": 0, "by_priority": {}}
        by_priority: dict[str, int] = {}
        for e in self._q:
            by_priority[e.priority] = by_priority.get(e.priority, 0) + 1
        return {
            "depth": len(self._q),
            "by_priority": by_priority,
            "oldest_ts": min(e.timestamp for e in self._q).isoformat(),
        }
