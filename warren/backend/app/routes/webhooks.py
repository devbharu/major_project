"""GitHub webhooks. Verify, acknowledge immediately, do the heavy work in the job queue."""
import hashlib
import hmac
import logging
import threading
import time
from collections import OrderedDict

from flask import Blueprint, current_app, jsonify, request

from ..agents.chat import ChatAgent
from ..agents.review import ReviewAgent
from ..config import settings
from ..container import repo_settings
from ..services.sync import upsert_pr

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")
log = logging.getLogger("warren.webhooks")
REVIEW_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


_seen_deliveries: OrderedDict = OrderedDict()
_delivery_lock = threading.Lock()


def _already_processed(delivery_id: str) -> bool:
    """Replay protection (GitHub redeliveries and replayed captures reuse X-GitHub-Delivery)."""
    if not delivery_id:
        return False
    with _delivery_lock:
        if delivery_id in _seen_deliveries:
            return True
        _seen_deliveries[delivery_id] = time.time()
        while len(_seen_deliveries) > 5000:
            _seen_deliveries.popitem(last=False)
    return False


def _valid_signature() -> bool:
    if not settings.github_webhook_secret:
        return False       # never accept unsigned deliveries
    digest = "sha256=" + hmac.new(settings.github_webhook_secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, request.headers.get("X-Hub-Signature-256", ""))


def _is_ours(body: str, sender: dict) -> bool:
    return "<!-- warren:" in (body or "") or sender.get("type") == "Bot"


@bp.post("/github")
def github():
    if not _valid_signature():
        return jsonify(error="bad signature"), 401
    event, payload = request.headers.get("X-GitHub-Event", ""), request.get_json(silent=True, force=True) or {}
    if event == "ping":
        return jsonify(ok=True)
    if _already_processed(request.headers.get("X-GitHub-Delivery", "")):
        return jsonify(ignored="duplicate delivery")
    c = current_app.container
    full_name = (payload.get("repository") or {}).get("full_name")
    repo = c.db.one("SELECT * FROM repos WHERE full_name=? AND enabled=1", (full_name,))
    if not repo:
        return jsonify(ignored="repository not enabled")

    if event == "pull_request":
        gh_pr, action = payload["pull_request"], payload.get("action")
        pr_id = upsert_pr(c.db, repo, gh_pr)
        pr = c.db.get("pull_requests", pr_id)
        if action in REVIEW_ACTIONS and repo_settings(repo)["auto_review"] and not gh_pr.get("draft"):
            if payload.get("after") and payload["after"] == pr["reviewed_sha"]:   # our own autofix commit
                return jsonify(ignored="already reviewed")
            c.jobs.submit("review", lambda p: ReviewAgent(c).run(pr_id, p), pr_id=pr_id)
        return jsonify(ok=True)

    if event == "workflow_run":
        run = payload["workflow_run"]
        if payload.get("action") == "completed" and run.get("conclusion") == "success":
            return jsonify(ok=True, resolved=c.ci.confirm_green(repo, run))      # closes the loop on a pushed fix
        if payload.get("action") != "completed" or run.get("conclusion") not in ("failure", "timed_out"):
            return jsonify(ignored="run did not fail")
        iid, start, why = c.ci.ingest(repo, run)
        if start:
            c.ci.start(iid)
        return jsonify(ok=True, incident=iid, skipped=why or None)

    if event in ("issue_comment", "pull_request_review_comment") and payload.get("action") == "created":
        comment, sender = payload["comment"], payload.get("sender") or {}
        if _is_ours(comment.get("body"), sender):
            return jsonify(ignored="bot comment")
        number = (payload.get("issue") or payload.get("pull_request") or {}).get("number")
        if event == "issue_comment" and "pull_request" not in payload.get("issue", {}):
            return jsonify(ignored="not a pull request")
        thread_root = comment.get("in_reply_to_id")
        ours = thread_root and c.db.one("SELECT id FROM findings WHERE comment_id=?", (thread_root,))
        if settings.bot_mention.lower() not in comment["body"].lower() and not ours:
            return jsonify(ignored="not addressed to the bot")
        pr = c.db.one("SELECT * FROM pull_requests WHERE repo_id=? AND number=?", (repo["id"], number))
        if not pr:
            pr_id = upsert_pr(c.db, repo, c.user_gh(repo["user_id"]).pr(full_name, number))
        else:
            pr_id = pr["id"]
        login = sender.get("login", "someone")
        trusted = c.can_write(repo["user_id"], full_name, login)   # only collaborators may run commands or teach the bot
        c.jobs.submit("chat", lambda p: ChatAgent(c).respond(
            pr_id, comment["body"], login, comment_id=thread_root or comment["id"] if event == "pull_request_review_comment" else None,
            in_review_thread=event == "pull_request_review_comment", trusted=trusted), pr_id=pr_id)
        return jsonify(ok=True)
    return jsonify(ignored=event)
