"""In-process event bus for run status/log events, consumed by the WebSocket endpoint.

WebSocket event shapes (frozen contract):
  {"type": "log",    "run_id": int, "stream": "stdout"|"stderr", "line": str}
  {"type": "status", "run_id": int, "status": str}
  {"type": "end",    "run_id": int, "status": str}
Log lines are also tailed from the run's artifact files, so runs started from a separate
CLI process still stream to the GUI.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, run_id: int) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: int, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs[run_id].discard(q)
        if not self._subs[run_id]:
            self._subs.pop(run_id, None)

    def publish(self, run_id: int, event: dict[str, Any]) -> None:
        for q in list(self._subs.get(run_id, ())):
            try:
                q.put_nowait({"run_id": run_id, **event})
            except asyncio.QueueFull:
                pass


bus = EventBus()
