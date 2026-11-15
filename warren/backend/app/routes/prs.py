"""Pull request board, detail, and actions (review / fix / chat / assign / dismiss)."""
from flask import Blueprint, current_app, g, jsonify, request

from ..agents.autofix import AutofixAgent
from ..agents.chat import ChatAgent
from ..agents.review import ReviewAgent
from ..agents.triage import COLUMNS
from ..auth import login_required
from ..ratelimit import limited
from ..services.github import GitHubError

bp = Blueprint("prs", __name__, url_prefix="/api")

VIEWS = {
    "all": lambda pr, me: True,
    "needs_my_review": lambda pr, me: me in pr["requested_reviewers"],
    "authored_by_me": lambda pr, me: pr["author"] == me,
    "safe_to_merge": lambda pr, me: pr["triage"].get("flags", {}).get("safe_to_merge", False),
    "security_watch": lambda pr, me: pr["triage"].get("risk") == "high" or bool(pr["triage"].get("signals", {}).get("sensitive_paths")),
    "release_train": lambda pr, me: any("release" in l.lower() or l.lower() == "hotfix" for l in pr["labels"]) or (pr["base_ref"] or "").startswith("release"),
    "agent_created": lambda pr, me: bool(pr["is_agent"]),
    "ci_failing": lambda pr, me: pr.get("_ci") in ("detected", "analyzing", "reproducing", "fixing", "validating", "awaiting_approval", "retrying", "escalated", "failed", "proposed", "inherited"),
}


def user_repo_ids() -> dict[int, dict]:
    return {r["id"]: r for r in current_app.db.query("SELECT * FROM repos WHERE user_id=? AND enabled=1", (g.user["id"],))}


def card(pr: dict, repo: dict, counts: dict) -> dict:
    t = pr["triage"] or {}
    return {"id": pr["id"], "number": pr["number"], "title": pr["title"], "repo": repo["name"], "repo_full_name": repo["full_name"],
            "author": pr["author"], "author_avatar": pr["author_avatar"], "is_agent": bool(pr["is_agent"]), "draft": bool(pr["draft"]),
            "url": pr["url"], "review_status": pr["review_status"], "priority": t.get("priority", "P2"), "tags": t.get("tags", []),
            "reason": t.get("reason", ""), "reviewers": t.get("reviewers", []), "requested_reviewers": pr["requested_reviewers"],
            "findings": counts.get(pr["id"], {}), "updated": pr["updated_gh"], "ci": pr.get("_ci")}


@bp.get("/prs")
@login_required
def board():
    db = current_app.db
    repos = user_repo_ids()
    repo_filter = request.args.get("repo_id", type=int)
    q = request.args.get("q", "").strip().lower()
    rows = [p for p in db.query("SELECT * FROM pull_requests WHERE state='open' ORDER BY updated_gh DESC") if p["repo_id"] in repos
            and (not repo_filter or p["repo_id"] == repo_filter)]
    if q:
        rows = [p for p in rows if q in p["title"].lower() or q in p["author"].lower() or q == str(p["number"]) or q in repos[p["repo_id"]]["name"].lower()]
    latest_ci = {i["pr_id"]: i["status"] for i in db.query("SELECT pr_id, status FROM incidents WHERE pr_id IS NOT NULL AND status NOT IN ('merged','superseded') ORDER BY id")}
    blocked = {(i["repo_id"], n) for i in db.query("SELECT repo_id, meta FROM incidents WHERE status IN ('detected','analyzing','reproducing','fixing','validating','awaiting_approval','escalated','proposed','fixed')"
                                                  " AND branch IS NOT NULL") for n in i["meta"].get("blocked_prs", [])}
    for p in rows:
        p["_ci"] = latest_ci.get(p["id"]) or ("inherited" if (p["repo_id"], p["number"]) in blocked else None)
    counts = {}
    for r in db.query("SELECT pr_id, severity, COUNT(*) n FROM findings WHERE status='open' AND pr_id IS NOT NULL GROUP BY pr_id, severity"):
        counts.setdefault(r["pr_id"], {})[r["severity"]] = r["n"]
    me = g.user["login"]
    view_counts = {name: sum(1 for p in rows if fn(p, me)) for name, fn in VIEWS.items()}
    view = request.args.get("view", "all")
    chosen = [p for p in rows if VIEWS.get(view, VIEWS["all"])(p, me)]
    columns = {k: {"label": v, "items": []} for k, v in COLUMNS.items()}
    for p in chosen:
        columns[(p["triage"] or {}).get("priority", "P2")]["items"].append(card(p, repos[p["repo_id"]], counts))
    return jsonify(columns=columns, counts=view_counts, total=len(chosen))


@bp.get("/prs/<int:pr_id>")
@login_required
def detail(pr_id):
    db = current_app.db
    pr = db.get("pull_requests", pr_id)
    repo = pr and db.get("repos", pr["repo_id"])
    if not pr or repo["user_id"] != g.user["id"]:
        return jsonify(error="not found"), 404
    job = db.one("SELECT * FROM jobs WHERE pr_id=? ORDER BY id DESC LIMIT 1", (pr_id,))
    pr = {k: v for k, v in pr.items() if k != "change_stack"}
    incidents = db.query("SELECT id, workflow, step_name, status, category, kind FROM incidents WHERE pr_id=? ORDER BY id DESC", (pr_id,))
    return jsonify(pr=pr, repo={"id": repo["id"], "full_name": repo["full_name"]}, job=job, incidents=incidents,
                   events=db.query("SELECT * FROM events WHERE pr_id=? ORDER BY id", (pr_id,)),
                   findings=db.query("SELECT * FROM findings WHERE pr_id=? ORDER BY id", (pr_id,)),
                   stack=(db.get("pull_requests", pr_id) or {}).get("change_stack") or {})


def _owned_pr(pr_id):
    db = current_app.db
    pr = db.get("pull_requests", pr_id)
    return pr if pr and db.get("repos", pr["repo_id"])["user_id"] == g.user["id"] else None


def _guard_demo():
    if g.user["login"] == "demo":
        return jsonify(error="Demo mode is read-only. Sign in with GitHub to run real reviews."), 403


@bp.post("/prs/<int:pr_id>/review")
@login_required
@limited("review", 12)
def review(pr_id):
    if (blocked := _guard_demo()):
        return blocked
    if not _owned_pr(pr_id):
        return jsonify(error="not found"), 404
    c = current_app.container
    return jsonify(job_id=c.jobs.submit("review", lambda p: ReviewAgent(c).run(pr_id, p, manual=True), pr_id=pr_id))


@bp.post("/prs/<int:pr_id>/fix")
@login_required
@limited("fix", 12)
def fix(pr_id):
    if (blocked := _guard_demo()):
        return blocked
    if not _owned_pr(pr_id):
        return jsonify(error="not found"), 404
    ids = (request.get_json(silent=True) or {}).get("finding_ids")
    c = current_app.container
    return jsonify(job_id=c.jobs.submit("autofix", lambda p: AutofixAgent(c).run(pr_id, ids, p), pr_id=pr_id))


@bp.post("/findings/<int:fid>/fix")
@login_required
@limited("fix", 12)
def fix_one(fid):
    if (blocked := _guard_demo()):
        return blocked
    f = current_app.db.get("findings", fid)
    if not f or not f["pr_id"] or not _owned_pr(f["pr_id"]):
        return jsonify(error="not found"), 404
    c = current_app.container
    return jsonify(job_id=c.jobs.submit("autofix", lambda p: AutofixAgent(c).run(f["pr_id"], [fid], p), pr_id=f["pr_id"]))


@bp.post("/findings/<int:fid>/dismiss")
@login_required
def dismiss(fid):
    f = current_app.db.get("findings", fid)
    repo = f and current_app.db.get("repos", f["repo_id"])
    if not f or repo["user_id"] != g.user["id"]:
        return jsonify(error="not found"), 404
    current_app.db.update("findings", fid, {"status": "dismissed"})
    return jsonify(ok=True)


@bp.post("/prs/<int:pr_id>/chat")
@login_required
@limited("chat", 12)
def chat(pr_id):
    if (blocked := _guard_demo()):
        return blocked
    if not _owned_pr(pr_id):
        return jsonify(error="not found"), 404
    text = (request.get_json(silent=True) or {}).get("message", "").strip()
    if not text:
        return jsonify(error="message required"), 400
    c, login = current_app.container, g.user["login"]
    return jsonify(job_id=c.jobs.submit("chat", lambda p: ChatAgent(c).respond(pr_id, text, login, quote_question=True), pr_id=pr_id))


@bp.post("/prs/<int:pr_id>/reviewers")
@login_required
def assign(pr_id):
    if (blocked := _guard_demo()):
        return blocked
    pr = _owned_pr(pr_id)
    if not pr:
        return jsonify(error="not found"), 404
    logins = (request.get_json(silent=True) or {}).get("logins", [])
    c = current_app.container
    repo = c.repo_of(pr)
    try:
        c.user_gh(g.user["id"]).request_reviewers(repo["full_name"], pr["number"], logins)
    except GitHubError as exc:
        return jsonify(error=str(exc)), 502
    c.db.update("pull_requests", pr_id, {"requested_reviewers": sorted(set(pr["requested_reviewers"]) | set(logins))})
    return jsonify(ok=True)


def owned_job(job_id: int):
    """A job is visible only to the owner of the repository it ran for."""
    db = current_app.db
    row = db.get("jobs", job_id)
    repo_id = row and (row["repo_id"] or (db.get("pull_requests", row["pr_id"]) or {}).get("repo_id") if row["pr_id"] else row["repo_id"])
    repo = repo_id and db.get("repos", repo_id)
    return row if repo and repo["user_id"] == g.user["id"] else None


@bp.get("/jobs/<int:job_id>")
@login_required
def job(job_id):
    row = owned_job(job_id)
    return jsonify(job=row) if row else (jsonify(error="not found"), 404)


@bp.get("/health")
@login_required
def health():
    from ..config import settings
    from ..services import llm
    c = current_app.container
    return jsonify(llm=llm.health(), sandbox=settings.sandbox_enabled, embeddings=c.vectors.provider,
                   github_oauth=settings.github_oauth_enabled, webhooks=bool(settings.public_url), poller=settings.poll_interval)
