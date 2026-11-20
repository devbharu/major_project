"""Flask application factory."""
import logging
import os
import threading
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

from .config import settings
from .container import Container, repo_settings
from .db import Database, now
from .jobs import JobRunner
from .services.bus import EventBus
from .services.vectors import VectorStore

log = logging.getLogger("warren")


def validate_runtime():
    """Refuse to start a production deployment that is configured unsafely."""
    if settings.env != "production":
        return
    problems = []
    if settings.sqlite_path == ":memory:":
        problems.append("SQLITE_PATH must be a file path (an in-memory database loses everything on restart)")
    if settings.demo_seed:
        problems.append("DEMO_SEED must be off")
    if settings.public_url and not settings.github_webhook_secret:
        problems.append("GITHUB_WEBHOOK_SECRET is required when PUBLIC_URL is set")
    for u in settings.cors_origins:
        if not u.startswith("https://"):
            problems.append(f"FRONTEND_URL ({u}) must be https:// in production")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
    if not settings.token_encryption_key:
        logging.getLogger("warren").warning("TOKEN_ENCRYPTION_KEY is not set: token encryption is derived from SECRET_KEY (rotating it signs everyone out).")


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_runtime()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["MAX_CONTENT_LENGTH"] = 26 * 1024 * 1024     # GitHub caps webhook payloads at 25 MB
    if settings.trust_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    allowed_origins = list(settings.cors_origins)
    if settings.env != "production":
        for local_origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            if local_origin not in allowed_origins:
                allowed_origins.append(local_origin)
    # Support Vercel deployment and preview subdomains if a Vercel domain is configured
    if any("vercel.app" in u for u in allowed_origins):
        import re
        allowed_origins.append(re.compile(r"^https://.*\.vercel\.app$"))

    CORS(
        app,
        origins=allowed_origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )

    os.makedirs(settings.chroma_path, exist_ok=True)
    app.db = Database(settings.sqlite_path)
    bus = EventBus()
    app.container = Container(db=app.db, vectors=VectorStore(settings.chroma_path), jobs=JobRunner(app.db, settings.worker_threads, bus),
                              bus=bus, checkpointer=_checkpointer())

    from .agents.ci_doctor import CiDoctor
    from .routes import auth, incidents, live, prs, repos, security, webhooks
    app.container.ci = CiDoctor(app.container)
    for module in (auth, repos, prs, security, webhooks, incidents, live):
        app.register_blueprint(module.bp)

    @app.get("/healthz")
    def healthz():          # liveness probe for containers / load balancers: reveals nothing about configuration
        return jsonify(ok=True)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if resp.mimetype != "text/event-stream":
            resp.headers.setdefault("Cache-Control", "no-store")
        if request.is_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    @app.errorhandler(Exception)
    def unhandled(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        log.exception("unhandled error")
        return jsonify(error="internal error"), 500

    if settings.demo_seed:
        from .seed import seed_demo
        seed_demo(app.container)
    if settings.poll_interval > 0:
        threading.Thread(target=_poller, args=(app.container,), daemon=True, name="warren-poller").start()
    return app


def _watch_ci(c: Container, repo: dict):
    """Proactive CI watcher (poller fallback when there is no webhook): close incidents that went green, then look at the
    latest failed run of each workflow/branch. `ingest` ignores anything that is not currently broken."""
    if not repo.get("watch_since"):          # repositories enabled before this existed: start watching from now, never retroactively
        c.db.update("repos", repo["id"], {"watch_since": now()})
        return
    c.ci.confirm_pending(repo)
    newest: dict[tuple, dict] = {}
    for run in c.user_gh(repo["user_id"]).failed_runs(repo["full_name"], 20):
        newest.setdefault((run.get("workflow_id"), run.get("head_branch")), run)      # newest first
    started = 0
    for run in newest.values():
        if started >= 3:
            break
        iid, start, _ = c.ci.ingest(repo, run)
        if start:
            c.ci.start(iid)
            started += 1


def _checkpointer():
    """LangGraph checkpoint store: lets a CI Doctor run pause for approval and resume (even after a restart)."""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    os.makedirs(os.path.dirname(settings.checkpoint_path) or ".", exist_ok=True)
    return SqliteSaver(sqlite3.connect(settings.checkpoint_path, check_same_thread=False))


def _poller(c: Container):
    """Fallback for setups without a public webhook URL: sync enabled repos and auto-review fresh commits."""
    from datetime import datetime, timedelta, timezone

    from .agents.review import ReviewAgent
    from .services.sync import sync_repo

    time.sleep(10)
    while True:
        try:
            for repo in c.db.query("SELECT * FROM repos WHERE enabled=1"):
                if repo["webhook_id"]:
                    continue    # webhooks handle this repo in real time
                user = c.db.get("users", repo["user_id"])
                if not user or user["login"] == "demo":
                    continue
                sync_repo(c, repo)
                if not repo_settings(repo)["auto_review"]:
                    continue
                cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
                fresh = c.db.query("SELECT * FROM pull_requests WHERE repo_id=? AND state='open' AND draft=0 AND review_status IN ('none','done','failed') "
                                   "AND COALESCE(reviewed_sha,'')!=head_sha AND updated_gh>?", (repo["id"], cutoff))
                for pr in fresh[:3]:
                    c.jobs.submit("review", lambda p, i=pr["id"]: ReviewAgent(c).run(i, p), pr_id=pr["id"])
                _watch_ci(c, repo)
        except Exception:
            log.exception("poller cycle failed")
        time.sleep(settings.poll_interval)
