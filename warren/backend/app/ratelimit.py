"""Small in-memory sliding-window rate limiter (single-process deployment, like the job queue)."""
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import g, jsonify, request

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def allow(key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def limited(name: str, limit: int, window: int = 60):
    """@limited('review', 10) -> at most 10 calls/minute per signed-in user (or per IP before sign-in)."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            who = g.user["id"] if getattr(g, "user", None) else request.remote_addr
            if not allow(f"{name}:{who}", limit, window):
                return jsonify(error="Too many requests. Please slow down."), 429
            return fn(*args, **kwargs)
        return wrapper
    return decorator
