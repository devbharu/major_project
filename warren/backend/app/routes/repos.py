"""Repositories: connect from GitHub, enable the bot (webhook), settings, sync, learnings."""
import logging

from flask import Blueprint, current_app, g, jsonify, request

from ..agents.refine import refine
from ..auth import login_required
from ..config import settings
from ..container import repo_settings
from ..db import now
from ..services.github import GitHubError
from ..services.sync import sync_repo

bp = Blueprint("repos", __name__, url_prefix="/api/repos")
log = logging.getLogger("warren.repos")


def public(repo: dict) -> dict:
    c = current_app.container
    open_prs = c.db.one("SELECT COUNT(*) n FROM pull_requests WHERE repo_id=? AND state='open'", (repo["id"],))["n"]
    return {"id": repo["id"], "full_name": repo["full_name"], "private": bool(repo["private"]), "enabled": bool(repo["enabled"]),
            "default_branch": repo["default_branch"], "webhook": bool(repo["webhook_id"]), "last_synced": repo["last_synced"],
            "open_prs": open_prs, "settings": repo_settings(repo)}


def owned(repo_id: int):
    repo = current_app.db.get("repos", repo_id)
    return repo if repo and repo["user_id"] == g.user["id"] else None


@bp.get("")
@login_required
def list_repos():
    rows = current_app.db.query("SELECT * FROM repos WHERE user_id=? ORDER BY enabled DESC, full_name", (g.user["id"],))
    return jsonify(repos=[public(r) for r in rows], public_url=bool(settings.public_url), poller=settings.poll_interval)


@bp.post("/refresh")
@login_required
def refresh():
    """Import the repositories the signed-in GitHub user can access."""
    c = current_app.container
    try:
        remote = c.user_gh(g.user["id"]).list_repos(100)
    except GitHubError as exc:
        return jsonify(error=str(exc)), 502
    for r in remote:
        c.db.upsert("repos", {"user_id": g.user["id"], "full_name": r["full_name"]}, {
            "owner": r["owner"]["login"], "name": r["name"], "default_branch": r["default_branch"],
            "private": int(r["private"]), "created_at": now()})
    return list_repos()


@bp.post("/<int:repo_id>/enable")
@login_required
def enable(repo_id):
    c, repo = current_app.container, owned(repo_id)
    if not repo:
        return jsonify(error="not found"), 404
    on = bool((request.get_json(silent=True) or {}).get("enabled", True))
    warning = None
    if on and settings.public_url and not repo["webhook_id"] and g.user["login"] != "demo" and not settings.github_webhook_secret:
        warning = "PUBLIC_URL is set but GITHUB_WEBHOOK_SECRET is empty, so no webhook was installed. Set a random secret and re-enable."
    elif on and settings.public_url and not repo["webhook_id"] and g.user["login"] != "demo":
        try:
            hook = c.user_gh(g.user["id"]).create_webhook(repo["full_name"], f"{settings.public_url}/api/webhooks/github", settings.github_webhook_secret)
            c.db.update("repos", repo_id, {"webhook_id": hook})
        except GitHubError as exc:
            warning = f"Webhook not created ({exc}). The poller will still pick up new PRs."
    if not on and repo["webhook_id"]:
        try:
            c.user_gh(g.user["id"]).delete_webhook(repo["full_name"], repo["webhook_id"])
        except GitHubError:
            pass
        c.db.update("repos", repo_id, {"webhook_id": None})
    c.db.update("repos", repo_id, {"enabled": int(on), **({"watch_since": now()} if on and not repo["enabled"] else {})})   # CI history before this moment is ignored
    if on and g.user["login"] != "demo":
        c.jobs.submit("sync", lambda p: (p.step("Syncing pull requests"), sync_repo(c, c.db.get("repos", repo_id))), repo_id=repo_id)
    return jsonify(repo=public(c.db.get("repos", repo_id)), warning=warning)


@bp.patch("/<int:repo_id>/settings")
@login_required
def update_settings(repo_id):
    repo = owned(repo_id)
    if not repo:
        return jsonify(error="not found"), 404
    allowed = {"profile", "instructions", "ignore_paths", "auto_review", "auto_fix", "post_comments", "ci_mode", "commit_status"}
    incoming = {k: v for k, v in (request.get_json(silent=True) or {}).items() if k in allowed}
    if incoming.get("instructions") is not None and incoming["instructions"] != repo_settings(repo)["instructions"]:
        incoming["instructions"] = refine(incoming["instructions"], "instructions")      # stored as clean, professional guidance
    merged = {**repo_settings(repo), **incoming}
    if merged["profile"] not in ("quiet", "chill", "assertive"):
        return jsonify(error="profile must be quiet, chill or assertive"), 400
    if merged["ci_mode"] not in ("off", "approval", "auto"):
        return jsonify(error="ci_mode must be off, approval or auto"), 400
    current_app.db.update("repos", repo_id, {"settings": merged})
    return jsonify(repo=public(current_app.db.get("repos", repo_id)))


@bp.post("/<int:repo_id>/sync")
@login_required
def sync(repo_id):
    c, repo = current_app.container, owned(repo_id)
    if not repo:
        return jsonify(error="not found"), 404
    if g.user["login"] == "demo":
        return jsonify(job_id=None)
    job = c.jobs.submit("sync", lambda p: (p.step("Syncing pull requests"), sync_repo(c, c.db.get("repos", repo_id))), repo_id=repo_id)
    return jsonify(job_id=job)


@bp.get("/<int:repo_id>/learnings")
@login_required
def learnings(repo_id):
    if not owned(repo_id):
        return jsonify(error="not found"), 404
    return jsonify(learnings=current_app.db.query("SELECT * FROM learnings WHERE repo_id=? ORDER BY id DESC", (repo_id,)))


@bp.delete("/<int:repo_id>/learnings/<int:learning_id>")
@login_required
def delete_learning(repo_id, learning_id):
    if not owned(repo_id):
        return jsonify(error="not found"), 404
    c = current_app.container
    c.db.execute("DELETE FROM learnings WHERE id=? AND repo_id=?", (learning_id, repo_id))
    c.vectors.drop_learning(repo_id, learning_id)
    return jsonify(ok=True)
