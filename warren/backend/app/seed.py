"""Demo data (DEMO_SEED=true) so the dashboard is explorable before GitHub is connected. Read-only user 'demo'."""
from datetime import datetime, timedelta, timezone

from .agents.triage import TriageScorer
from .container import AGENT_NAME, BOT_NAME
from .db import now

AV = "https://api.dicebear.com/9.x/notionists-neutral/svg?seed="
PERMISSIONS_PATCH = """@@ -21,6 +21,12 @@
   return member.role === "ADMIN"
 }

-export function canRemoveMember(member: Member) {
-  return member.role === "ADMIN"
-}
+export function canRemoveMember(member: Member, target: Member) {
+  if (target.role === "OWNER") return false
+  return member.role === "ADMIN" || member.role === "OWNER"
+}
+
+export function canInvite(member: Member, role: Role) {
+  return role !== "OWNER" && member.role !== "VIEWER"
+}"""

PRIORITY = {25493: "P0", 25095: "P0", 2704: "P0", 63: "P1", 126: "P1", 23204: "P1", 23447: "P2", 23373: "P2", 482: "P1"}
COLUMN = {"P0": "Urgent", "P1": "High priority", "P2": "Medium priority", "P3": "Low priority"}

PRS = [  # repo, number, title, author, agent, labels, files(path, changes), body, hours_old
    ("identity-platform", 25493, "fix(auth): rotate exposed production signing key", "sentinel-agent[bot]", True, ["security"], [("src/auth/signing.py", 140), ("deploy/secrets.yaml", 12)], "Agent detected a compromised key path. Human judgment required.", 5),
    ("platform", 25095, "feat(mcp): add integrations-authoritative reverse tunnel routing", "caseyreed", False, [], [("src/mcp/tunnel.ts", 420), ("src/mcp/router.ts", 380), ("src/config/routes.ts", 40)], "New, active work needs your attention.", 20),
    ("webapp", 2704, "docs(blog): explain graph engineering and LLM orchestration", "aaravmehta", False, [], [("docs/blog/graph-engineering.md", 60)], "This may need maintainer follow-up.", 40),
    ("events", 63, "feat(managed-events): own historical event programs", "aaravmehta", False, [], [("src/events/programs.ts", 240), ("src/events/store.ts", 150), ("tests/programs.test.ts", 90)], "Assign a reviewer to keep this moving.", 30),
    ("carrot-ui", 126, "feat(theme): tasteful depth, applied across the system", "nadiapetrova", False, [], [("src/theme/depth.css", 60), ("src/components/Card.tsx", 30)], "Review has been waiting and may need coordination.", 200),
    ("platform", 23204, "Autofix path instructions, try 2", "mateosilva", False, [], [("src/agents/autofix.py", 55), ("tests/test_autofix.py", 20)], "Review has been waiting and may need coordination.", 90),
    ("platform", 23447, "fix(roles): scope Assigned role checks to the organization", "priyaraman", False, [], [("src/server/auth/permissions.ts", 70)], "Reviewer assigned; waiting for review.", 12),
    ("platform", 23373, "feat(reviews): add in-app review nudges", "luciatorres", False, [], [("src/reviews/nudge.ts", 330), ("src/reviews/ui.tsx", 280)], "Another role owns the next action.", 60),
    ("web-app", 482, "Add teammate invitations with roles", "priyaraman", False, [], [("src/server/auth/permissions.ts", 70), ("src/server/db/invitations.ts", 90), ("src/server/api/invite.ts", 120), ("src/server/email/invite.tsx", 60), ("src/components/InviteDialog.tsx", 110), ("tests/invite.test.ts", 80)], "Teammates can be invited with a role; admins can remove members.", 3),
]


def seed_demo(c):
    db = c.db
    if db.one("SELECT id FROM users WHERE login='demo'"):
        return
    uid = db.insert("users", {"github_id": 0, "login": "demo", "name": "Demo Reviewer", "avatar_url": AV + "demo", "token": "", "created_at": now()})
    repos = {}
    for name in ("platform", "identity-platform", "webapp", "events", "carrot-ui", "web-app"):
        repos[name] = db.insert("repos", {"user_id": uid, "full_name": f"acme/{name}", "owner": "acme", "name": name, "default_branch": "main",
                                          "private": 1, "enabled": 1, "created_at": now(), "last_synced": now()})
    scorer, ids = TriageScorer(c), {}
    for repo, number, title, author, agent, labels, files, body, hours in PRS:
        created = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        lines = sum(ch for _, ch in files)
        pr = {"title": title, "additions": int(lines * .8), "deletions": int(lines * .2), "labels": labels, "is_agent": int(agent), "draft": 0,
              "head_sha": "a1b2c3d", "created_gh": created, "review_status": "none"}
        reviewers = ["nadiapetrova", "mateosilva"] if number in (63, 126) else []
        triage = scorer.score(pr, [{"filename": p, "changes": ch} for p, ch in files], reviewers=reviewers)
        triage["reason"] = body
        want = PRIORITY.get(number)
        if want:
            triage["priority"], triage["column"] = want, COLUMN[want]
        ids[number] = db.insert("pull_requests", {
            "repo_id": repos[repo], "number": number, "title": title, "body": body, "author": author, "author_avatar": AV + author,
            "is_agent": int(agent), "state": "open", "draft": 0, "head_ref": f"feature/{number}", "head_sha": "a1b2c3d", "base_ref": "main",
            "head_repo": f"acme/{repo}", "additions": pr["additions"], "deletions": pr["deletions"], "changed_files": len(files), "labels": labels,
            "requested_reviewers": ["demo"] if number in (25095, 23447) else [], "url": f"https://github.com/acme/{repo}/pull/{number}",
            "review_status": "none", "triage": triage, "created_gh": created, "updated_gh": created, "updated_at": now()})
    _seed_review(c, ids[482], repos["web-app"])
    _seed_security(c, repos["platform"])
    _seed_incidents(c, repos, ids)
    db.insert("learnings", {"repo_id": repos["web-app"], "text": "HTTP handlers own request validation and status-code mapping; services must stay transport-agnostic.",
                            "path_hint": "src/server/api", "source_pr": 482, "created_by": "priyaraman", "created_at": now()})


def _seed_review(c, pr_id, repo_id):
    db = c.db
    findings = [
        ("major", "Potential issue", "Owners can be removed by admins", "`canRemoveMember` lets an ADMIN remove an OWNER. Guard the target role before authorising.", "src/server/auth/permissions.ts", 24,
         '  return member.role === "ADMIN"', '  if (target.role === "OWNER") return false\n  return member.role === "ADMIN" || member.role === "OWNER"', "verified"),
        ("minor", "Security", "Invitation tokens never expire", "Tokens are generated without an expiry, so a leaked email grants access forever. Store `expiresAt` and reject expired tokens.", "src/server/db/invitations.ts", 31, None, None, "open"),
    ]
    stat = []
    for sev, cat, title, body, path, line, old, new, status in findings:
        fid = db.insert("findings", {"pr_id": pr_id, "repo_id": repo_id, "source": "review", "severity": sev, "category": cat, "title": title, "body": body,
                                     "path": path, "line": line, "suggestion": new, "status": status, "meta": {"old_code": old}, "created_at": now()})
        stat.append((fid, sev, cat, title, body, path, line, old, new, status))
    summary = "Adds teammate invitations with roles: a data model, API endpoints, invite emails and an admin-only permission check. Authorization changes deserve a careful human read."
    c.emit(pr_id, BOT_NAME, "bot", "summary", f"## Review summary\n\n**Walkthrough**\n\n{summary}\n\n**Assessment: Changes requested.** 1 major finding to resolve before merge.", {"posted": False})
    for fid, sev, cat, title, body, path, line, old, new, status in stat:
        c.emit(pr_id, BOT_NAME, "bot", "finding", f"**{title}**\n\n{body}", {"finding_id": fid, "severity": sev, "category": cat, "path": path, "line": line, "old_code": old, "new_code": new})
        if status == "verified":
            c.emit(pr_id, AGENT_NAME, "agent", "fix", "Fixed in `9f2c4a1` — owners can no longer be removed by admins, with a regression test for the owner path.",
                   {"finding_id": fid, "commit": "9f2c4a1", "old_code": old, "new_code": new})
            c.emit(pr_id, BOT_NAME, "bot", "verify", "Verified. The target role is checked before authorising and the new test covers the failure mode.", {"finding_id": fid})
    stack_files = [
        ("src/server/db/invitations.ts", "added", 90, 0, "@@ -0,0 +1,6 @@\n+export type Invitation = {\n+  id: string\n+  email: string\n+  role: Role\n+  token: string\n+}"),
        ("src/server/auth/permissions.ts", "modified", 12, 3, PERMISSIONS_PATCH),
        ("src/server/api/invite.ts", "added", 120, 0, "@@ -0,0 +1,4 @@\n+export async function POST(req: Request) {\n+  const { email, role } = await req.json()\n+  return createInvitation(email, role)\n+}"),
        ("src/server/email/invite.tsx", "added", 60, 0, "@@ -0,0 +1,2 @@\n+export const InviteEmail = ({ url }) => <a href={url}>Join the team</a>\n+"),
    ]
    mk = lambda i, t, d, idx: {"id": i, "title": t, "description": d, "files": [{"path": p, "status": s, "additions": a, "deletions": dl, "patch": pa} for p, s, a, dl, pa in [stack_files[j] for j in idx]]}
    stack = {
        "overview": {"summary": summary, "intent": "Let admins invite teammates with a specific role.", "risk": "medium", "files": 9, "additions": 460, "deletions": 3},
        "layers": [mk(1, "Add the invitation data model", "Defines the invitation record and its token.", [0]),
                   mk(2, "Add the invitation API and emails", "Endpoints for token generation; the invitation email; and the admin-only permission check.", [2, 3, 1])],
        "blast_radius": [{"area": "Authorization", "impact": "Changes who may remove members and who may invite.", "severity": "high", "files": ["src/server/auth/permissions.ts"]},
                         {"area": "Email delivery", "impact": "New outbound email containing a secret link.", "severity": "medium", "files": ["src/server/email/invite.tsx"]}],
        "architecture": [{"component": "Auth permissions", "change": "Signature gains a target member", "note": "All callers of canRemoveMember must pass the target."},
                         {"component": "Database", "change": "New invitations table", "note": "Needs a migration and an expiry column."}],
        "dependents": {"src/server/auth/permissions.ts": ["src/server/api/members.ts", "src/components/MemberList.tsx"]},
        "verify": [{"name": "Clone & checkout", "status": "passed", "detail": "PR #482 at a1b2c3d"}, {"name": "Python syntax", "status": "passed", "detail": "all files compile"},
                   {"name": "Description Check", "status": "passed", "detail": "The PR description gives reviewers context."}],
    }
    pr = db.get("pull_requests", pr_id)
    db.update("pull_requests", pr_id, {"review_status": "done", "reviewed_sha": pr["head_sha"], "summary": summary, "change_stack": stack,
              "premerge": [{"name": "Description Check", "status": "passed", "explanation": "The PR description gives reviewers context."},
                           {"name": "Test Changes", "status": "passed", "explanation": "Tests were updated alongside the code."}]})


def _seed_security(c, repo_id):
    db = c.db
    rows = [("dependency", "high", "Dependency vulnerability", "axios · GHSA-4w2v-q235-vp99", "Axios requests can leak credentials to third-party hosts.", "package.json",
             {"package": "axios", "version": "0.21.0", "ecosystem": "npm", "fixed_in": "0.21.2", "vuln_id": "GHSA-4w2v-q235-vp99"}),
            ("security", "medium", "Security", "Session cookie missing SameSite", "The session cookie is set without SameSite, enabling CSRF on state-changing routes.", "src/server/auth/session.ts", {"verified": True}),
            ("security", "low", "Security", "Verbose error leaks stack trace", "Errors return stack traces in production responses.", "src/server/api/errors.ts", {"verified": True})]
    for src, sev, cat, title, body, path, meta in rows:
        db.insert("findings", {"repo_id": repo_id, "source": src, "severity": sev, "category": cat, "title": title, "body": body, "path": path,
                               "status": "open", "meta": meta, "created_at": now()})
    for i, (cr, hi, me, lo) in enumerate([(1, 1, 2, 3), (1, 2, 2, 3), (0, 1, 2, 2), (0, 1, 1, 2)]):
        stamp = (datetime.now(timezone.utc) - timedelta(days=(3 - i) * 4)).isoformat(timespec="seconds")
        db.insert("scans", {"repo_id": repo_id, "kind": "full", "counts": {"critical": cr, "high": hi, "medium": me, "low": lo, "healthy": 12, "unscanned": 40}, "created_at": stamp})


def _seed_incidents(c, repos, ids):
    db = c.db
    base = {"kind": "ci", "created_at": now(), "updated_at": now()}

    def incident(repo, pr, run_id, workflow, job, step, branch, sha, status, category, cause, solution, conf, **extra):
        meta = extra.pop("meta", {})
        iid = db.insert("incidents", {**base, "repo_id": repos[repo], "pr_id": ids.get(pr), "run_id": run_id, "workflow": workflow, "job_name": job,
                                      "step_name": step, "branch": branch, "head_sha": sha, "status": status, "category": category, "root_cause": cause,
                                      "solution": solution, "confidence": conf, "run_url": f"https://github.com/acme/{repo}/actions/runs/{run_id}",
                                      "attempts": 1, "meta": meta, **extra})
        return iid

    def say(iid, pr, text, kind="ci_analysis", who=AGENT_NAME, actor_kind="agent"):
        c.emit(ids.get(pr), who, actor_kind, kind, text, {"incident_id": iid}, incident_id=iid)

    log = "E   AssertionError: assert 11 == 10\nFAILED tests/test_invite.py::test_invite_total\n##[error]Process completed with exit code 1."
    a = incident("web-app", 482, 9001, "CI", "Tests", "Run pytest -q", "feature/482", "a1b2c3d", "fixed", "test_failure",
                 "`invite_total()` adds a stray `+ 1` seat, so the invoice total is off by one.", "Remove the stray `+ 1` in `app/billing.py`.", 0.93,
                 fix_commit="9f2c4a1d7e", log_excerpt=log, meta={"validation": "`pytest -q` passes in the E2B sandbox", "diffs": {"app/billing.py": "--- a/app/billing.py\n+++ b/app/billing.py\n@@ -8,3 +8,3 @@\n-    return base + seats * price + 1\n+    return base + seats * price"}})
    say(a, 482, "CI run failed: **CI** on `feature/482` (a1b2c3d). Investigating.", "ci_detected")
    say(a, 482, "Failing step: **Tests › Run pytest -q**\n\n`pytest -q`")
    say(a, 482, "Classified as **test failure** → plan: **fix**.")
    say(a, 482, "Reproduced the failure in the sandbox.")
    say(a, 482, "Attempt 1: proposed a fix touching `app/billing.py` (confidence 93%).")
    say(a, 482, "Validated: `pytest -q` passes in the E2B sandbox.")
    say(a, 482, "Fixed in `9f2c4a1` — committed to `feature/482`.", "fix")

    b = incident("platform", 23204, 9002, "CI", "Lint & Build", "Run npm run build", "autofix-path-2", "b7c8d9e", "awaiting_approval", "build",
                 "The build imports `lodash` but the lockfile pins a version without the `get` export used by the new code.", "Bump `lodash` in `package.json` and refresh `package-lock.json`.", 0.71,
                 log_excerpt="##[error]Module not found: Can't resolve 'lodash/get'\nnpm ERR! code 1", meta={
                     "approval_reasons": ["it touches protected files: package-lock.json"], "validation": "`npm run build` passes in the E2B sandbox",
                     "diffs": {"package.json": "--- a/package.json\n+++ b/package.json\n@@ -12,1 +12,1 @@\n-    \"lodash\": \"^4.17.0\",\n+    \"lodash\": \"^4.17.21\","}})
    say(b, 23204, "CI run failed: **CI** on `autofix-path-2` (b7c8d9e). Investigating.", "ci_detected")
    say(b, 23204, "Classified as **build** → plan: **fix**.")
    say(b, 23204, "Validated: `npm run build` passes in the E2B sandbox.")
    say(b, 23204, "Waiting for your approval before touching the repository: it touches protected files: package-lock.json.", "ci_approval")

    d = incident("identity-platform", None, 9003, "Deploy to production", "deploy", "Run ./deploy.sh", "main", "e5f6a7b", "escalated", "permissions_config",
                 "The deploy job cannot assume the AWS role: the `AWS_ROLE_ARN` secret is missing in the `production` environment.",
                 "Add the secret in Settings → Environments → production, then re-run the failed job.", 0.88, kind="cd",
                 log_excerpt="##[error]Input required and not supplied: role-to-assume", meta={"issue_url": "https://github.com/acme/identity-platform/issues/12"})
    say(d, None, "CI run failed: **Deploy to production** on `main` (e5f6a7b). Investigating.", "ci_detected")
    say(d, None, "Classified as **permissions config** → plan: **escalate**.")
    say(d, None, "### CI failure analysis\n\n**Why it broke.** The deploy job cannot assume the AWS role: the `AWS_ROLE_ARN` secret is missing in the `production` environment.\n\n"
        "**Recommended fix.** Add the secret, then re-run the failed job.\n\n1. Open Settings → Environments → production\n2. Add `AWS_ROLE_ARN`\n3. Re-run failed jobs", "report")

    f = incident("events", 63, 9004, "CI", "Integration", "Run npm test", "feature/63", "c1d2e3f", "retrying", "flaky_infra",
                 None, None, 0.5, log_excerpt="Error: read ECONNRESET", meta={"retried": True})
    say(f, 63, "Classified as **flaky infra** → plan: **retry**.")
    say(f, 63, "This looks transient (network/infra signature or not reproducible). Re-running the failed jobs once; I will dig in if it fails again.", "ci_retry")

    m = incident("platform", None, 9007, "CI", "Tests", "Run pytest -q", "main", "e1f2a3b", "escalated", "test_failure",
                 "`test_rate_limit` asserts the old limit of 100 requests; the limit was changed to 60 on main.", "Update the expected limit in `tests/test_rate_limit.py`, or revert the limit change.",
                 0.82, log_excerpt=log, meta={"blocked_prs": [23373], "also": ["Backend tests"]})
    say(m, None, "CI run failed: **CI** on `main` (e1f2a3b). Investigating.", "ci_detected")
    say(m, None, "The same failure also appeared in **Backend tests**; covered by this investigation.", "ci_analysis")
    say(m, None, "### CI failure analysis\n\n**Why it broke.** `test_rate_limit` asserts the old limit of 100 requests.\n\n**Recommended fix.** Update the expected limit in the test, or revert the limit change.", "report")
