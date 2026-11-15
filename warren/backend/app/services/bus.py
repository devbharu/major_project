"""In-process pub/sub that feeds the Server-Sent Events stream (real-time dashboard updates)."""
import queue
import threading
from collections import defaultdict


class EventBus:
    """Topics used: 'board', 'incidents', 'pr:<id>', 'incident:<id>'. Payloads are tiny hints; clients refetch."""

    def __init__(self):
        self._subs: dict[str, set[queue.Queue]] = defaultdict(set)
        self._lock = threading.Lock()

    def subscribe(self, topics: list[str]) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            for t in topics:
                self._subs[t].add(q)
        return q

    def unsubscribe(self, q: queue.Queue, topics: list[str]):
        with self._lock:
            for t in topics:
                self._subs[t].discard(q)

    def publish(self, topic: str, data: dict | None = None):
        with self._lock:
            targets = list(self._subs.get(topic, ()))
        for q in targets:
            try:
                q.put_nowait((topic, data or {}))
            except queue.Full:   # slow client: drop, it will resync on the next hint
                pass
