"""Mirror GitHub PR state into SQLite (used by manual sync, the poller and webhooks)."""
import logging

from ..db import now

log = logging.getLogger("warren.sync")
AGENT_PREFIXES = ("codex/", "copilot/", "claude/", "devin/", "cursor/", "agent/", "dependabot/", "renovate/")


def is_agent_pr(gh_pr: dict) -> bool:
    user = gh_pr.get("user") or {}
    ref = (gh_pr.get("head") or {}).get("ref", "")
    labels = [l["name"].lower() for l in gh_pr.get("labels", [])]
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]") or ref.startswith(AGENT_PREFIXES) or "agent" in labels


def upsert_pr(db, repo: dict, gh_pr: dict) -> int:
    head = gh_pr.get("head") or {}
    values = {
        "title": gh_pr["title"], "body": gh_pr.get("body") or "", "author": (gh_pr.get("user") or {}).get("login", "ghost"),
        "author_avatar": (gh_pr.get("user") or {}).get("avatar_url", ""), "is_agent": int(is_agent_pr(gh_pr)),
        "state": "merged" if gh_pr.get("merged_at") else gh_pr["state"], "draft": int(bool(gh_pr.get("draft"))),
        "head_ref": head.get("ref", ""), "head_sha": head.get("sha", ""), "base_ref": (gh_pr.get("base") or {}).get("ref", ""),
        "head_repo": ((head.get("repo") or {}).get("full_name")) or repo["full_name"],
        "labels": [l["name"] for l in gh_pr.get("labels", [])],
        "requested_reviewers": [r["login"] for r in gh_pr.get("requested_reviewers", [])],
        "can_modify": int(bool(gh_pr.get("maintainer_can_modify"))), "url": gh_pr.get("html_url", ""), "created_gh": gh_pr.get("created_at"), "updated_gh": gh_pr.get("updated_at"),
        "updated_at": now(),
    }
    for key in ("additions", "deletions", "changed_files"):
        if key in gh_pr:
            values[key] = gh_pr[key]
    if "mergeable_state" in gh_pr:
        values["mergeable"] = gh_pr["mergeable_state"]
    return db.upsert("pull_requests", {"repo_id": repo["id"], "number": gh_pr["number"]}, values)


def sync_repo(c, repo: dict) -> int:
    """Pull open PRs (fetched in parallel), score them with the heuristic triage scorer, close PRs that are no longer open."""
    from concurrent.futures import ThreadPoolExecutor

    from ..agents.triage import TriageScorer

    gh = c.user_gh(repo["user_id"])
    scorer, seen = TriageScorer(c), []

    def fetch(summary):
        try:    # the list endpoint lacks additions/mergeable, so each PR needs its own call; files feed the triage score
            return gh.pr(repo["full_name"], summary["number"]), gh.pr_files(repo["full_name"], summary["number"])
        except Exception:
            log.warning("could not fetch PR #%s", summary.get("number"), exc_info=True)
            return None

    with ThreadPoolExecutor(6) as pool:
        fetched = [x for x in pool.map(fetch, gh.open_prs(repo["full_name"], limit=30)) if x]
    for full, files in fetched:
        pr_id = upsert_pr(c.db, repo, full)
        pr = c.db.get("pull_requests", pr_id)
        c.db.update("pull_requests", pr_id, {"triage": scorer.score(pr, files, findings=c.db.query("SELECT * FROM findings WHERE pr_id=?", (pr_id,)))})
        seen.append(pr_id)
    for row in c.db.query("SELECT id FROM pull_requests WHERE repo_id=? AND state='open'", (repo["id"],)):
        if row["id"] not in seen:
            c.db.update("pull_requests", row["id"], {"state": "closed"})
    c.db.update("repos", repo["id"], {"last_synced": now()})
    c.touch_board()
    return len(seen)
