"""Server-Sent Events: pushes tiny 'something changed' hints so the dashboard updates in real time (no polling)."""
import json
import queue
import re

import threading
from collections import Counter

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from ..auth import issue_ticket, login_required, ticket_required
from ..ratelimit import limited
from .prs import owned_job

bp = Blueprint("live", __name__, url_prefix="/api")
TOPIC = re.compile(r"^(board|incidents|pr:\d+|incident:\d+)$")


_open: Counter = Counter()
_open_lock = threading.Lock()
MAX_STREAMS_PER_USER = 6


@bp.post("/stream/ticket")
@login_required
@limited("ticket", 30)
def ticket():
    return jsonify(ticket=issue_ticket(g.user["id"]))


@bp.get("/stream")
@ticket_required
def stream():
    user_id = g.user["id"]
    with _open_lock:
        if _open[user_id] >= MAX_STREAMS_PER_USER:
            return jsonify(error="too many open live connections"), 429
        _open[user_id] += 1
    topics = [t for t in request.args.get("topics", "board").split(",") if TOPIC.match(t)]
    bus = current_app.container.bus
    q = bus.subscribe(topics)

    def events():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    topic, data = q.get(timeout=20)
                    yield f"event: update\ndata: {json.dumps({'topic': topic, **data})}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"   # keep proxies from closing the idle connection
        finally:
            bus.unsubscribe(q, topics)
            with _open_lock:
                _open[user_id] -= 1

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.post("/jobs/<int:job_id>/cancel")
@login_required
def cancel_job(job_id):
    return jsonify(cancelled=bool(owned_job(job_id) and current_app.container.jobs.cancel(job_id)))
