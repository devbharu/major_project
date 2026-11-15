"""CI/CD incidents: list, inspect, approve / steer / retry / dismiss the CI Doctor's work."""
from flask import Blueprint, current_app, g, jsonify, request

from ..agents.refine import refine
from ..auth import login_required
from ..ratelimit import limited

bp = Blueprint("incidents", __name__, url_prefix="/api/incidents")
ACTIVE = ("detected", "analyzing", "reproducing", "fixing", "validating", "retrying")


def _mine(incident_id: int):
    db = current_app.db
    inc = db.get("incidents", incident_id)
    repo = inc and db.get("repos", inc["repo_id"])
    return (inc, repo) if inc and repo["user_id"] == g.user["id"] else (None, None)


def _demo():
    if g.user["login"] == "demo":
        return jsonify(error="Demo mode is read-only."), 403


def serialize(inc: dict, repo: dict, pr: dict | None) -> dict:
    return {**{k: v for k, v in inc.items() if k not in ("thread_id", "log_excerpt")}, "repo": repo["full_name"],
            "pr_number": pr["number"] if pr else None, "pr_title": pr["title"] if pr else None}


@bp.get("")
@login_required
def list_incidents():
    db = current_app.db
    repos = {r["id"]: r for r in db.query("SELECT * FROM repos WHERE user_id=?", (g.user["id"],))}
    rows = [i for i in db.query("SELECT * FROM incidents WHERE status NOT IN ('merged','superseded') ORDER BY id DESC LIMIT 200") if i["repo_id"] in repos]
    prs = {p["id"]: p for p in db.query("SELECT id, number, title FROM pull_requests")}
    return jsonify(incidents=[serialize(i, repos[i["repo_id"]], prs.get(i["pr_id"])) for i in rows],
                   active=sum(i["status"] in ACTIVE or i["status"] == "awaiting_approval" for i in rows))


@bp.get("/<int:incident_id>")
@login_required
def detail(incident_id):
    inc, repo = _mine(incident_id)
    if not inc:
        return jsonify(error="not found"), 404
    db = current_app.db
    pr = db.get("pull_requests", inc["pr_id"]) if inc["pr_id"] else None
    return jsonify(incident={**inc, "thread_id": None, "repo": repo["full_name"], "pr_number": pr and pr["number"], "pr_title": pr and pr["title"]},
                   events=db.query("SELECT * FROM events WHERE incident_id=? ORDER BY id", (incident_id,)),
                   job=db.one("SELECT * FROM jobs WHERE incident_id=? ORDER BY id DESC LIMIT 1", (incident_id,)))


@bp.post("/<int:incident_id>/approve")
@login_required
@limited("incident-action", 20)
def approve(incident_id):
    if (blocked := _demo()):
        return blocked
    inc, _ = _mine(incident_id)
    if not inc:
        return jsonify(error="not found"), 404
    if inc["status"] != "awaiting_approval":
        return jsonify(error="this incident is not waiting for approval"), 409
    body = request.get_json(silent=True) or {}
    c = current_app.container
    approved, note = bool(body.get("approved", True)), refine(body.get("note") or "", "guidance")
    job = c.jobs.submit("ci_resume", lambda p: c.ci.resume(incident_id, approved, note, p), repo_id=inc["repo_id"], pr_id=inc["pr_id"], incident_id=incident_id)
    return jsonify(job_id=job)


@bp.post("/<int:incident_id>/retry")
@login_required
@limited("incident-action", 20)
def retry(incident_id):
    if (blocked := _demo()):
        return blocked
    inc, _ = _mine(incident_id)
    if not inc:
        return jsonify(error="not found"), 404
    note = refine((request.get_json(silent=True) or {}).get("note") or "", "guidance")
    return jsonify(job_id=current_app.container.ci.start(incident_id, note))


@bp.post("/<int:incident_id>/dismiss")
@login_required
def dismiss(incident_id):
    inc, _ = _mine(incident_id)
    if not inc:
        return jsonify(error="not found"), 404
    current_app.db.update("incidents", incident_id, {"status": "dismissed"})
    current_app.container.touch_board()
    return jsonify(ok=True)


@bp.post("/<int:incident_id>/cancel")
@login_required
def cancel(incident_id):
    inc, _ = _mine(incident_id)
    job = inc and current_app.db.one("SELECT id FROM jobs WHERE incident_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (incident_id,))
    return jsonify(cancelled=bool(job and current_app.container.jobs.cancel(job["id"])))
