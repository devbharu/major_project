"""Triage: score, rank and route PRs (P0-P3). Deterministic scoring first; the LLM only writes the one-line reason."""
import logging
import re
from datetime import datetime, timezone

from ..services import llm
from . import prompts
from .schemas import TriageNote

log = logging.getLogger("warren.triage")
SENSITIVE = re.compile(r"(auth|login|session|token|secret|crypt|passw|permission|rbac|oauth|payment|billing|\.env|dockerfile|"
                       r"\.github/workflows|migration|terraform|k8s|helm|iam)", re.I)
DOCS = re.compile(r"(\.md$|\.rst$|\.txt$|^docs?/|license)", re.I)
URGENT_LABELS = {"hotfix", "urgent", "security", "p0", "critical", "incident", "sev1"}
TEMPLATES = {
    "P0": "Review this first: it carries high risk and should not wait behind routine work.",
    "P1": "Review soon with a human in the loop; the change is consequential or has been waiting.",
    "P2": "Keep this visible for async review; it is useful work without an immediate interruption signal.",
    "P3": "Hold this behind other work; it is low risk, low urgency, or not ready for reviewer attention.",
}
LEVELS = ["Low", "Medium", "High"]
COLUMNS = {"P0": "Urgent", "P1": "High priority", "P2": "Medium priority", "P3": "Low priority"}


class TriageScorer:
    def __init__(self, container):
        self.c = container

    def score(self, pr: dict, files: list[dict], findings=(), dependents: int = 0, use_llm: bool = False,
              reviewers: list[str] | None = None) -> dict:
        paths = [f["filename"] for f in files]
        lines = pr.get("additions", 0) + pr.get("deletions", 0) or sum(f.get("changes", 0) for f in files)
        labels = {l.lower() for l in pr.get("labels", [])}
        open_findings = [f for f in findings if f.get("status") in ("open", None)]
        crit = sum(f["severity"] == "critical" for f in open_findings)
        major = sum(f["severity"] == "major" for f in open_findings)
        sensitive = [p for p in paths if SENSITIVE.search(p)]
        docs_only = bool(paths) and all(DOCS.search(p) for p in paths)

        risk_pts = (len(sensitive) * 2 + crit * 4 + major * 2 + (2 if lines > 800 else 0) + (1 if dependents > 5 else 0)
                    + (3 if labels & {"security", "hotfix", "incident"} else 0) + (1 if sensitive and pr.get("is_agent") else 0))
        risk = 2 if risk_pts >= 6 else 1 if risk_pts >= 3 else 0
        cx_pts = len(paths) + lines / 100
        complexity = 2 if cx_pts > 12 else 1 if cx_pts > 5 else 0
        effort = 2 if lines > 600 else 1 if lines > 150 else 0

        age_days = 0
        if pr.get("created_gh"):
            created = datetime.fromisoformat(pr["created_gh"].replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days
        title = pr.get("title", "").lower()
        urgency = (2 if labels & URGENT_LABELS else 0) + (1 if title.startswith(("fix", "hotfix")) else 0) + \
                  (1 if age_days > 7 else 0) + (1 if sensitive else 0)

        if risk == 2 and (crit or labels & URGENT_LABELS or sensitive) or (labels & {"p0", "incident", "sev1"}):
            level = 0
        elif risk == 2 or urgency >= 3:
            level = 1
        elif docs_only or (risk == 0 and lines < 40 and urgency == 0):
            level = 3
        else:
            level = 2
        if pr.get("draft"):
            level = min(level + 1, 3)
        priority = f"P{level}"

        reviewed_sha = pr.get("reviewed_sha")
        flags = {
            "agent_created": bool(pr.get("is_agent")),
            "draft": bool(pr.get("draft")),
            "new_changes": bool(reviewed_sha and reviewed_sha != pr.get("head_sha")),
            "needs_author_action": pr.get("mergeable") == "dirty" or crit > 0,
            "high_blast_radius": dependents > 5 or risk == 2,
            "needs_human_review": risk >= 1 or crit > 0 or bool(pr.get("is_agent") and sensitive),
        }
        flags["safe_to_merge"] = (risk == 0 and not crit and not major and pr.get("review_status") == "done"
                                  and not flags["draft"] and not flags["needs_author_action"])
        review_type = "Review later" if (pr.get("draft") or docs_only) else \
                      "Deep review" if risk == 2 or complexity == 2 else \
                      "Quick review" if lines < 60 and risk == 0 else "Standard review"

        tags = []
        if flags["agent_created"]: tags.append("Agent-created")
        if flags["needs_human_review"]: tags.append("Needs human review")
        if flags["new_changes"]: tags.append("New changes since last review")
        if flags["needs_author_action"]: tags.append("Needs author action")
        if flags["draft"]: tags.append("Draft")
        if flags["high_blast_radius"]: tags.append("High blast radius")
        tags += [f"{LEVELS[risk]} risk", f"{LEVELS[complexity]} complexity", f"{LEVELS[effort]} effort", review_type]

        result = {"priority": priority, "column": COLUMNS[priority], "risk": LEVELS[risk].lower(), "complexity": LEVELS[complexity].lower(),
                  "effort": LEVELS[effort].lower(), "review_type": review_type, "flags": flags, "tags": tags,
                  "reason": TEMPLATES[priority], "reviewers": reviewers or (pr.get("triage") or {}).get("reviewers", []),
                  "signals": {"lines": lines, "files": len(paths), "sensitive_paths": sensitive[:5], "age_days": age_days}}
        if use_llm:
            try:
                note = llm.ask_json(prompts.TRIAGE_SYSTEM, f"PR: {pr['title']}\nPriority: {priority}\nRisk: {result['risk']}\n"
                                    f"Signals: {result['signals']}\nTags: {tags}", TriageNote, temperature=0.3)
                result["reason"] = note.reason
            except Exception:
                log.info("LLM triage note unavailable, using template")
        return result

    def suggest_reviewers(self, gh, repo: dict, pr: dict, files: list[dict]) -> list[str]:
        """Rank likely reviewers by who recently touched the biggest changed files."""
        counts: dict[str, int] = {}
        for f in sorted(files, key=lambda f: f.get("changes", 0), reverse=True)[:3]:
            try:
                for login in gh.path_authors(repo["full_name"], f["filename"], pr["base_ref"] or repo["default_branch"]):
                    if login != pr["author"] and not login.endswith("[bot]"):
                        counts[login] = counts.get(login, 0) + 1
            except Exception:
                log.info("could not read history for %s", f["filename"])
                continue
        return [login for login, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]]
