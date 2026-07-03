"""Tiny in-process pub/sub used to stream live progress to the GUI."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque


class EventBus:
    def __init__(self, replay: int = 200):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._recent: deque[dict] = deque(maxlen=replay)

    def publish(self, experiment_id: str, kind: str, payload: dict | None = None) -> None:
        event = {
            "experiment_id": experiment_id,
            "kind": kind,
            "payload": payload or {},
            "ts": time.time(),
        }
        with self._lock:
            self._recent.append(event)
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    @staticmethod
    def sse_format(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"
