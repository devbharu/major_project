"""Security overview, scans, and 'fix in a PR'."""
from flask import Blueprint, current_app, g, jsonify, request

from ..agents.security_scan import SecurityScanner
from ..auth import login_required
from ..ratelimit import limited
from ..services.github import GitHubError

bp = Blueprint("security", __name__, url_prefix="/api/security")


def _repos(repo_id):
    rows = current_app.db.query("SELECT * FROM repos WHERE user_id=? AND enabled=1", (g.user["id"],))
    return [r for r in rows if not repo_id or r["id"] == repo_id]


@bp.get("/overview")
@login_required
def overview():
    db = current_app.db
    repos = _repos(request.args.get("repo_id", type=int))
    ids = [r["id"] for r in repos] or [0]
    marks = ",".join("?" * len(ids))
    scans = db.query(f"SELECT * FROM scans WHERE repo_id IN ({marks}) ORDER BY id", ids)  # nosec B608 - marks are ? placeholders
    latest = {}
    for s in scans:
        latest[s["repo_id"]] = s
    totals = dict.fromkeys(("critical", "high", "medium", "low", "healthy", "unscanned"), 0)
    for s in latest.values():
        for k in totals:
            totals[k] += s["counts"].get(k, 0)
    findings = db.query(f"SELECT * FROM findings WHERE repo_id IN ({marks}) AND source IN ('security','dependency') "  # nosec B608 - marks are ? placeholders
                        f"AND status IN ('open','fixed') ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id", ids)
    health = ("Not scanned" if not latest else "At risk" if totals["critical"] else "Needs attention" if totals["high"]
              else "Mostly healthy" if totals["medium"] else "Healthy")
    trend = [{"date": s["created_at"][:10], **{k: s["counts"].get(k, 0) for k in ("critical", "high", "medium", "low")}} for s in scans][-30:]
    names = {r["id"]: r["full_name"] for r in repos}
    for f in findings:
        f["repo"] = names.get(f["repo_id"])
    return jsonify(health=health, totals=totals, findings=findings, trend=trend, last_scan=max((s["created_at"] for s in latest.values()), default=None),
                   scanned_repos=len(latest), repos=[{"id": r["id"], "full_name": r["full_name"]} for r in repos])


@bp.post("/scan")
@login_required
@limited("scan", 6)
def scan():
    if g.user["login"] == "demo":
        return jsonify(error="Demo mode is read-only."), 403
    repo_id = (request.get_json(silent=True) or {}).get("repo_id")
    repo = current_app.db.get("repos", repo_id) if repo_id else None
    if not repo or repo["user_id"] != g.user["id"]:
        return jsonify(error="pick a connected repository"), 400
    c = current_app.container
    return jsonify(job_id=c.jobs.submit("security_scan", lambda p: SecurityScanner(c).run(repo_id, p), repo_id=repo_id))


@bp.post("/findings/<int:fid>/fix-pr")
@login_required
def fix_pr(fid):
    if g.user["login"] == "demo":
        return jsonify(error="Demo mode is read-only."), 403
    finding = current_app.db.get("findings", fid)
    repo = finding and current_app.db.get("repos", finding["repo_id"])
    if not finding or repo["user_id"] != g.user["id"]:
        return jsonify(error="not found"), 404
    try:
        return jsonify(url=SecurityScanner(current_app.container).open_fix_pr(fid))
    except (ValueError, GitHubError) as exc:
        return jsonify(error=str(exc)), 400
