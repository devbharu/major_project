"""CI Doctor: an autonomous agent that turns a failed CI/CD run into a verified fix (or a precise diagnosis).

LangGraph state machine (checkpointed in SQLite, so it can pause for human approval and resume):

  collect -> classify --fix--> reproduce -> diagnose_fix -> validate -> prepare --(safe)--> apply -> report
                 |  \\--retry--> retry (re-run flaky jobs)      ^  |  \\--(risky/unverified)--> approval (interrupt)
                 |                                              |__|  loop with feedback (max attempts)      |
                 \\--escalate--> explain (root cause + steps; revert PR or issue)                steer / dismiss
"""
import difflib
import logging
import posixpath
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import settings
from ..container import AGENT_NAME, Container, repo_settings
from ..db import now
from ..services import ci_logs, llm
from ..services.github import GitHubError, visible_lines
from ..services.safety import safe_markdown, safe_repo_path, wrap_untrusted
from ..services.sync import upsert_pr
from ..services.sandbox import REPO_DIR, SandboxSession, SandboxUnavailable, quote
from . import prompts
from .schemas import CiFix
from .tools import build_tools, run_agent

log = logging.getLogger("warren.ci")
MARK = "<!-- warren:ci -->"
WORKFLOW_DIR = ".github/workflows/"
ACTIVE = ("detected", "analyzing", "reproducing", "fixing", "validating", "retrying", "awaiting_approval")
OPEN = ACTIVE + ("fixed", "escalated", "proposed")      # an incident still needs attention or confirmation
HIDDEN = ("merged", "superseded")           # internal bookkeeping, never shown or counted
LABELS = {"collect": "Reading the failed run and its logs", "classify": "Classifying the failure", "retry": "Re-running transient failures",
          "reproduce": "Reproducing the failure in the E2B sandbox", "diagnose_fix": "Diagnosing the root cause and writing a fix",
          "validate": "Validating the fix in the sandbox", "prepare": "Deciding whether approval is needed", "approval": "Waiting for approval",
          "steer": "Re-planning with your guidance", "apply": "Applying the fix", "report": "Reporting back", "explain": "Writing the incident analysis",
          "dismiss": "Dismissed", "link_duplicate": "Merging with the existing incident", "inherit": "Linking to the failure on the default branch"}
INSTALL = ("if [ -f requirements.txt ]; then pip install -q -r requirements.txt 2>&1 | tail -2; fi; "
           "if [ -f package.json ]; then (npm ci --silent || npm install --silent) 2>&1 | tail -2; fi")


@dataclass
class Runtime:
    """Per-incident, non-serialisable objects (kept out of the LangGraph checkpoint on purpose)."""
    incident_id: int
    repo: dict
    inc_pr: dict | None
    gh: Any
    run: dict = field(default_factory=dict)
    sandbox: Any = None
    command: dict | None = None
    originals: dict = field(default_factory=dict)


class CiState(TypedDict, total=False):
    incident_id: int
    digest: str
    category: str
    strategy: str
    retry_failed: bool
    reproduced: bool
    repro_output: str
    attempts: int
    feedback: str
    fix: dict
    files: dict
    diffs: dict
    apply_failed: bool
    validation_state: str
    validation: str
    approval_reasons: list
    approved: bool
    note: str
    stalled: bool
    last_files: dict
    ref: int
    reason: str
    stop: bool


def _ts(value: str | None) -> datetime:
    return datetime.fromisoformat((value or "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))


def _in(values: tuple) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"      # constants only, never user input


class CiDoctor:
    def __init__(self, container: Container, sandbox_factory=None):
        self.c = container
        self.sandbox_factory = sandbox_factory or SandboxSession
        self._rt: dict[int, Runtime] = {}
        self._ingest_lock = threading.Lock()
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ public API
    # ------------------------------------------------------------------ deciding whether a failure deserves work
    def ingest(self, repo: dict, run: dict) -> tuple[int | None, bool, str]:
        """Single entry point for webhooks and the poller. Returns (incident_id, should_start, reason).
        A failed run only becomes work if the pipeline is broken RIGHT NOW: the run is for the branch's current head commit,
        it is the latest run of its workflow, and nobody is already handling that commit."""
        with self._ingest_lock:
            why = self.assess(repo, run)
            if why:
                return None, False, why
            self._supersede_older(repo, run)
            iid, start = self.open_incident(repo, run)
            return iid, start, ""

    def assess(self, repo: dict, run: dict) -> str | None:
        """Why this run should NOT be acted on (None = act). Order matters: cheapest checks first."""
        db = self.c.db
        if run.get("conclusion") not in ("failure", "timed_out"):
            return "the run did not fail"          # cancelled runs are normal (concurrency cancel-in-progress)
        since = repo.get("watch_since")
        if since and _ts(run.get("created_at")) < _ts(since):
            return "the run predates Warren watching this repository"
        existing = db.one("SELECT id, status FROM incidents WHERE repo_id=? AND run_id=?", (repo["id"], run["id"]))
        gh = self.c.user_gh(repo["user_id"])
        head_repo = (run.get("head_repository") or {}).get("full_name") or repo["full_name"]
        branch = run.get("head_branch") or ""
        try:
            if gh.branch_head(head_repo, branch) != run.get("head_sha"):
                return "superseded by a newer commit"
        except GitHubError as exc:
            if exc.status == 404:
                return "the branch no longer exists"
        if run.get("workflow_id"):
            try:
                latest = next(iter(gh.workflow_runs(repo["full_name"], run["workflow_id"], branch, 1)), None)
                if latest and latest["id"] != run["id"]:
                    return "a newer run of this workflow exists (it is judged on its own)"
            except GitHubError:
                pass
        if existing and existing["status"] not in ("retrying",):
            return f"already tracked as incident #{existing['id']}"
        twin = db.one(f"SELECT id FROM incidents WHERE repo_id=? AND branch=? AND head_sha=? AND run_id!=? AND status IN {_in(OPEN)}",  # nosec B608 - constants
                      (repo["id"], branch, run.get("head_sha"), run["id"]))
        if twin:
            row = db.get("incidents", twin["id"])
            also = sorted(set(row["meta"].get("also", [])) | {run.get("name", "workflow")})
            self._set(twin["id"], meta={**row["meta"], "also": also})
            return f"same commit already tracked as incident #{twin['id']}"
        return None

    def _supersede_older(self, repo: dict, run: dict):
        """A new failing commit makes older open incidents of the same workflow/branch obsolete."""
        for old in self.c.db.query(f"SELECT * FROM incidents WHERE repo_id=? AND workflow=? AND branch=? AND head_sha!=? AND status IN {_in(OPEN)}",  # nosec B608 - constants
                                   (repo["id"], run.get("name"), run.get("head_branch"), run.get("head_sha"))):
            job = self.c.db.one("SELECT id FROM jobs WHERE incident_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (old["id"],))
            if job:
                self.c.jobs.cancel(job["id"])
            self._set(old["id"], status="superseded")

    def still_current(self, inc: dict, rt: Runtime) -> str | None:
        """Re-checked before work that changes things: has the branch moved on, or turned green by itself?"""
        repo, gh = rt.repo, rt.gh
        head_repo = (rt.run.get("head_repository") or {}).get("full_name") or repo["full_name"]
        try:
            if gh.branch_head(head_repo, inc["branch"]) != inc["head_sha"]:
                return "superseded"
        except GitHubError as exc:
            return "superseded" if exc.status == 404 else None
        wf = (inc["meta"] or {}).get("workflow_id")
        if wf:
            try:
                latest = next(iter(gh.workflow_runs(repo["full_name"], wf, inc["branch"], 1)), None)
                if latest and latest["id"] != inc["run_id"] and latest.get("conclusion") == "success":
                    return "resolved"
            except GitHubError:
                pass
        return None

    def open_incident(self, repo: dict, run: dict) -> tuple[int, bool]:
        """Create (or re-arm) the incident row for a failed run. Prefer `ingest`, which decides whether one should exist at all."""
        db = self.c.db
        attempt = run.get("run_attempt", 1)
        existing = db.one("SELECT * FROM incidents WHERE repo_id=? AND run_id=?", (repo["id"], run["id"]))
        if existing:
            if existing["status"] == "retrying" and attempt > (existing["meta"].get("run_attempt") or 1):
                self._set(existing["id"], status="detected", meta={**existing["meta"], "run_attempt": attempt})
                return existing["id"], True
            return existing["id"], False
        pr = None
        for ref in run.get("pull_requests") or []:
            pr = pr or db.one("SELECT * FROM pull_requests WHERE repo_id=? AND number=?", (repo["id"], ref["number"]))
        pr = pr or db.one("SELECT * FROM pull_requests WHERE repo_id=? AND head_ref=? AND state='open'", (repo["id"], run.get("head_branch")))
        pr = pr or self._pr_from_github(repo, run)
        kind = "cd" if re.search(r"(deploy|release|publish|prod|\bcd\b)", f"{run.get('name', '')} {run.get('path', '')}", re.I) else "ci"
        iid = db.insert("incidents", {
            "repo_id": repo["id"], "pr_id": pr["id"] if pr else None, "run_id": run["id"], "run_url": run.get("html_url", ""),
            "workflow": run.get("name", "workflow"), "branch": run.get("head_branch", ""), "head_sha": run.get("head_sha", ""), "kind": kind,
            "status": "detected", "meta": {"run_attempt": attempt, "retried": False, "workflow_id": run.get("workflow_id"), "also": []},
            "created_at": now(), "updated_at": now()})
        self._say(iid, f"CI run failed: **{run.get('name', 'workflow')}** on `{run.get('head_branch')}` ({run.get('head_sha', '')[:7]}). Investigating.", "detected", pr and pr["id"])
        self.c.touch_board()
        return iid, True

    def _pr_from_github(self, repo: dict, run: dict) -> dict | None:
        """The PR may not be synced yet (webhook raced the poller). Look it up so fixes target the right PR branch."""
        try:
            gh = self.c.user_gh(repo["user_id"])
            numbers = [r["number"] for r in run.get("pull_requests") or []]
            if not numbers and run.get("head_branch"):
                owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login") or repo["owner"]
                numbers = [p["number"] for p in gh.pulls_for_branch(repo["full_name"], owner, run["head_branch"])]
            if numbers:
                return self.c.db.get("pull_requests", upsert_pr(self.c.db, repo, gh.pr(repo["full_name"], numbers[0])))
        except Exception:
            log.info("could not resolve the PR for run %s", run.get("id"), exc_info=True)
        return None

    def confirm_green(self, repo: dict, run: dict) -> int:
        """A run passed. If it is the branch's current head (or our fix commit), every open incident of that workflow/branch is over:
        whether our fix held, a human fixed it, or a re-run passed. Closing them here is what stops any further work."""
        db, done = self.c.db, 0
        rows = db.query(f"SELECT * FROM incidents WHERE repo_id=? AND workflow=? AND branch=? AND status IN {_in(OPEN)}",  # nosec B608 - constants
                        (repo["id"], run.get("name"), run.get("head_branch")))
        if not rows:
            return 0
        head_repo = (run.get("head_repository") or {}).get("full_name") or repo["full_name"]
        try:
            is_head = self.c.user_gh(repo["user_id"]).branch_head(head_repo, run.get("head_branch", "")) == run.get("head_sha")
        except GitHubError:
            is_head = False
        for inc in rows:
            if not (is_head or inc["fix_commit"] == run.get("head_sha")):
                continue
            job = db.one("SELECT id FROM jobs WHERE incident_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (inc["id"],))
            if job:
                self.c.jobs.cancel(job["id"])
            note = ("The fix held." if inc["status"] == "fixed" else
                    "It passed on re-run with no code change, so it was most likely a flaky failure. The first failure's log is kept on this incident." if inc["status"] == "retrying" else
                    "The pipeline is green again, so this incident was closed automatically.")
            self._set(inc["id"], status="resolved", meta={**inc["meta"], "ci_green": True, "flaky": inc["status"] == "retrying"})
            self._say(inc["id"], f"CI is green again on `{run.get('head_branch')}` ({run.get('head_sha', '')[:7]}). {note}", "report")
            done += 1
        return done

    def confirm_pending(self, repo: dict) -> int:
        """Poller fallback (no webhooks): ask GitHub whether each open incident's pipeline has gone green."""
        gh, done = self.c.user_gh(repo["user_id"]), 0
        for inc in self.c.db.query(f"SELECT * FROM incidents WHERE repo_id=? AND status IN {_in(OPEN)}", (repo["id"],)):  # nosec B608 - constants
            wf = (inc["meta"] or {}).get("workflow_id")
            if not wf:
                continue
            try:
                latest = next(iter(gh.workflow_runs(repo["full_name"], wf, inc["branch"], 1)), None)
            except GitHubError:
                continue
            if latest and latest.get("conclusion") == "success":
                done += self.confirm_green(repo, latest)
        return done

    def start(self, incident_id: int, feedback: str = ""):
        inc = self.c.db.get("incidents", incident_id)
        return self.c.jobs.submit("ci_doctor", lambda p: self.run(incident_id, p, feedback), repo_id=inc["repo_id"], pr_id=inc["pr_id"], incident_id=incident_id)

    def run(self, incident_id: int, progress, feedback: str = ""):
        thread = f"inc{incident_id}-{uuid.uuid4().hex[:8]}"
        if self._inc(incident_id)["thread_id"]:     # a repeat analysis: mark where it starts so the log stays readable
            self._say(incident_id, "Re-running the analysis" + (f" with this guidance: {feedback}" if feedback else "") + ".", "steer", actor="you", actor_kind="human")
        self._set(incident_id, thread_id=thread, error=None)
        self._stream({"incident_id": incident_id, "attempts": 0, "feedback": feedback}, thread, incident_id, progress)

    def resume(self, incident_id: int, approved: bool, note: str, progress):
        inc = self.c.db.get("incidents", incident_id)
        if inc["status"] != "awaiting_approval" or not inc["thread_id"]:
            raise ValueError("this incident is not waiting for approval")
        self._stream(Command(resume={"approved": approved, "note": note}), inc["thread_id"], incident_id, progress)

    # ------------------------------------------------------------------ graph
    def _build_graph(self):
        g = StateGraph(CiState)
        for name in LABELS:
            g.add_node(name, getattr(self, f"_{name}"))
        g.add_edge(START, "collect")
        g.add_conditional_edges("collect", lambda s: "stop" if s.get("stop") else "classify", {"stop": END, "classify": "classify"})
        g.add_conditional_edges("classify", lambda s: s["strategy"],
                                {"fix": "reproduce", "retry": "retry", "escalate": "explain", "duplicate": "link_duplicate", "inherited": "inherit"})
        g.add_conditional_edges("retry", lambda s: "explain" if s.get("retry_failed") else "end", {"explain": "explain", "end": END})
        g.add_conditional_edges("reproduce", self._after_reproduce, {"diagnose": "diagnose_fix", "retry": "retry"})
        g.add_conditional_edges("diagnose_fix", self._after_diagnose, {"validate": "validate", "again": "diagnose_fix", "explain": "explain"})
        g.add_conditional_edges("validate", self._after_validate, {"prepare": "prepare", "again": "diagnose_fix", "explain": "explain"})
        g.add_conditional_edges("prepare", lambda s: "approve" if s.get("approval_reasons") else "apply", {"approve": "approval", "apply": "apply"})
        g.add_conditional_edges("approval", self._after_approval, {"apply": "apply", "steer": "steer", "dismiss": "dismiss"})
        for a, b in [("steer", "reproduce"), ("apply", "report"), ("report", END), ("explain", END), ("dismiss", END), ("link_duplicate", END), ("inherit", END)]:
            g.add_edge(a, b)
        return g.compile(checkpointer=self.c.checkpointer)

    def _stream(self, payload, thread: str, incident_id: int, progress):
        cfg = {"configurable": {"thread_id": thread}}
        try:
            for update in self.graph.stream(payload, cfg, stream_mode="updates"):
                for node in update:
                    if node != "__interrupt__":
                        progress.step(LABELS.get(node, node))
            if not self.graph.get_state(cfg).next:
                self._drop(incident_id)
        except Exception as exc:
            self._drop(incident_id)
            if exc.__class__.__name__ == "JobCancelled":
                self._set(incident_id, status="dismissed")
            else:
                self._set(incident_id, status="failed", error=str(exc)[:400])
                self._say(incident_id, f"CI Doctor crashed: {exc}", "error")
            raise

    # ------------------------------------------------------------------ nodes
    def _collect(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        rt.run = {}
        self._set(iid, status="analyzing")
        repo = rt.repo["full_name"]
        try:
            rt.run = rt.gh.workflow_run(repo, inc["run_id"])
            jobs = rt.gh.run_jobs(repo, inc["run_id"])
        except GitHubError as exc:
            self._say(iid, f"Could not read the run from GitHub: {exc}", "error")
            return {"digest": "", "category": "unknown"}
        verdict = self.still_current(inc, rt)
        if verdict:   # the branch moved on or went green while we were queued: do nothing, quietly
            self._set(iid, status=verdict, meta={**inc["meta"], "closed_reason": "checked before analysis"})
            return {"stop": True}
        job = next((j for j in jobs if j.get("conclusion") in ("failure", "timed_out")), None)
        step = next((st for st in (job or {}).get("steps", []) if st.get("conclusion") == "failure"), None)
        text = ""
        if job:
            try:
                text = rt.gh.job_logs(repo, job["id"])
            except GitHubError as exc:
                log.warning("no logs for job %s: %s", job["id"], exc)
        dg = ci_logs.digest(text, settings.ci_log_chars)
        rt.command = None
        if job and step:
            try:
                path = (rt.run.get("path") or "").split("@")[0]
                yml, _ = rt.gh.file(repo, path, inc["head_sha"])
                rt.command = ci_logs.workflow_command(yml, job["name"], step["name"])
            except (GitHubError, KeyError):
                pass
        self._set(iid, job_name=(job or {}).get("name"), step_name=(step or {}).get("name"), log_excerpt=dg,
                  meta={**inc["meta"], "command": rt.command})
        self._say(iid, f"Failing step: **{(job or {}).get('name', '?')} › {(step or {}).get('name', '?')}**" +
                  (f"\n\n`{rt.command['run'].strip().splitlines()[0]}`" if rt.command else ""), "analysis")
        return {"digest": dg}

    def _classify(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        digest = s.get("digest", "")
        cat = ci_logs.classify(digest)
        fp = ci_logs.fingerprint(digest) if digest else None
        self._set(iid, category=cat, fingerprint=fp)
        mode = repo_settings(rt.repo)["ci_mode"]

        verdict = self._dedupe(inc, rt, fp)           # same failure already known? already broken on main? did our own fix fail?
        if verdict:
            strategy, ref, reason = verdict
            self._say(iid, f"Classified as **{cat.replace('_', ' ')}** → plan: **{strategy}**.", "analysis")
            return {"category": cat, "strategy": strategy, "ref": ref or 0, "reason": reason}

        pushed = self.c.db.one("SELECT COUNT(*) n FROM incidents WHERE pr_id=? AND fix_commit IS NOT NULL", (inc["pr_id"] or -1,))["n"]
        reason = ""
        if cat == "permissions_config":
            strategy = "escalate"
        elif cat == "flaky_infra":
            strategy = "escalate" if (inc["meta"].get("retried") or inc["kind"] == "cd") else "retry"   # re-running a deploy could deploy twice
            reason = "cd_no_retry" if inc["kind"] == "cd" else ""
        elif re.match(r"^warren[/-](ci-fix|revert)-", inc["branch"] or ""):
            strategy, reason = "escalate", "own_fix_branch"        # never fix a fix: stops fix-of-fix loops
        elif pushed >= 2:
            strategy, reason = "escalate", "pr_cap"                # loop breaker: Warren already pushed 2 CI fixes to this PR
        elif mode == "off":
            strategy, reason = "escalate", "mode_off"
        else:
            strategy = "fix"
        self._say(iid, f"Classified as **{cat.replace('_', ' ')}** → plan: **{strategy}**.", "analysis")
        return {"category": cat, "strategy": strategy, "reason": reason}

    def _dedupe(self, inc: dict, rt: Runtime, fp: str | None):
        """Decide whether this failure is new work. Returns (strategy, ref_incident_id, reason) or None to carry on."""
        if not fp:
            return None
        db, repo = self.c.db, rt.repo
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
        prior = db.query("SELECT * FROM incidents WHERE repo_id=? AND fingerprint=? AND id<? AND created_at>? AND status!='failed' ORDER BY id DESC",
                         (repo["id"], fp, inc["id"], cutoff))
        for p in (p for p in prior if p["branch"] == inc["branch"] and p["head_sha"] == inc["head_sha"] and p["fix_commit"] != inc["head_sha"]):
            # same commit, same failure, reported by another workflow/run: one investigation covers it
            return "duplicate", p["parent_id"] or p["id"], f"same failing commit {inc['head_sha'][:7]}"
        for p in (p for p in prior if p["branch"] == inc["branch"]):
            if p["status"] == "fixed" and p["fix_commit"] and p["fix_commit"] == inc["head_sha"]:
                return "escalate", p["id"], "fix_failed"           # we pushed a fix and CI still fails the same way
            if p["status"] == "fixed" and p["fix_pr_url"]:
                state = self._fix_pr_state(rt, p["fix_pr_url"])
                if state == "open":
                    return "duplicate", p["id"], "fix PR already open"
                if state == "merged":
                    return "escalate", p["id"], "fix_failed"       # fix merged, same failure persists
                continue                                           # fix PR closed without merging: a fresh attempt is allowed
            if p["status"] in ("detected", "analyzing", "reproducing", "fixing", "validating", "awaiting_approval", "retrying",
                               "escalated", "dismissed", "inherited", "duplicate"):
                return "duplicate", p["parent_id"] or p["id"], "already being handled"
        if inc["branch"] != repo["default_branch"]:
            known = next((p for p in prior if p["branch"] == repo["default_branch"] and p["status"] != "dismissed"), None)
            main_id = known["id"] if known else self._main_incident(inc, rt, fp)
            if main_id:
                return "inherited", main_id, "also failing on the default branch"
        return None

    def _fix_pr_state(self, rt: Runtime, url: str) -> str:
        try:
            pr = rt.gh.pr(rt.repo["full_name"], int(url.rstrip("/").rsplit("/", 1)[-1]))
            return "merged" if pr.get("merged_at") or pr.get("merged") else pr["state"]
        except Exception:
            return "open"

    def _main_incident(self, inc: dict, rt: Runtime, fp: str) -> int | None:
        """Is the SAME failure already on the default branch? Then this PR did not cause it, and fixing it here would be wrong."""
        repo = rt.repo
        try:
            wf = rt.run.get("workflow_id")
            latest = next(iter(rt.gh.workflow_runs(repo["full_name"], wf, repo["default_branch"], 1) or []), None) if wf else None
            if not latest or latest.get("conclusion") != "failure":
                return None
            job = next((j for j in rt.gh.run_jobs(repo["full_name"], latest["id"]) if j.get("conclusion") == "failure"), None)
            if not job or ci_logs.fingerprint(ci_logs.digest(rt.gh.job_logs(repo["full_name"], job["id"]), settings.ci_log_chars)) != fp:
                return None
            main_id, start, _ = self.ingest(repo, latest)
            if main_id is None:     # already tracked (twin) - find it
                twin = self.c.db.one(f"SELECT id FROM incidents WHERE repo_id=? AND branch=? AND status IN {_in(OPEN)} ORDER BY id DESC",  # nosec B608 - constants
                                     (repo["id"], repo["default_branch"]))
                return twin["id"] if twin else None
            if start:
                self.start(main_id)
            return main_id
        except Exception:
            log.info("could not compare with the default branch", exc_info=True)
            return None

    def _link_duplicate(self, s):
        """Same failure already handled elsewhere: fold into it. No new incident is shown, nothing is posted."""
        iid, ref = s["incident_id"], s.get("ref")
        inc, parent = self._inc(iid), self._inc(s.get("ref"))
        self._set(iid, status="merged", parent_id=ref, meta={**inc["meta"], "reason": s.get("reason")})
        if parent:
            also = sorted(set(parent["meta"].get("also", [])) | {inc["workflow"]})
            self._set(ref, meta={**parent["meta"], "also": also})
            self._say(ref, f"The same failure also appeared in **{inc['workflow']}**; covered by this investigation.", "analysis", to_pr=False)
        return {}

    def _inherit(self, s):
        """The default branch fails the same way, so this PR did not cause it. Track it on the main incident and tell the PR once."""
        iid, ref = s["incident_id"], s.get("ref")
        inc, rt, main = self._inc(iid), self._runtime(iid), self._inc(s.get("ref"))
        self._set(iid, status="merged", parent_id=ref, meta={**inc["meta"], "reason": "inherited"})
        number = rt.inc_pr["number"] if rt.inc_pr else None
        blocked = list((main or {}).get("meta", {}).get("blocked_prs", []))
        if number and number not in blocked:
            self._set(ref, meta={**main["meta"], "blocked_prs": blocked + [number]})
            where = f" Fix PR: {main['fix_pr_url']}" if main.get("fix_pr_url") else ""
            try:
                self.c.bot_gh(rt.repo["user_id"]).issue_comment(rt.repo["full_name"], number,
                    f"{MARK}\n### CI is failing, but not because of this pull request\n\n`{inc['step_name']}` fails the same way on `{rt.repo['default_branch']}`. "
                    f"Warren is handling it there (incident #{ref}).{where}\n\nRe-run CI or rebase once that is fixed.")
            except GitHubError:
                log.warning("could not post the inherited-failure note", exc_info=True)
        return {}

    def _retry(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        try:
            rt.gh.rerun_failed(rt.repo["full_name"], inc["run_id"])
        except GitHubError as exc:
            self._say(iid, f"Could not re-run the failed jobs ({exc}).", "error")
            return {"retry_failed": True}
        self._set(iid, status="retrying", meta={**inc["meta"], "retried": True})
        self._say(iid, "This looks transient (network/infra signature or not reproducible). Re-running the failed jobs once; I will dig in if it fails again.", "retry")
        return {"retry_failed": False}

    def _reproduce(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        self._set(iid, status="reproducing")
        if not self._ensure_sandbox(iid, rt, inc):
            return {"reproduced": False, "repro_output": "", "validation_state": "unverified"}
        sb = rt.sandbox
        cwd = self._cwd(rt.command)
        if self._dir_exists(sb, cwd):
            sb.run(INSTALL, cwd=cwd, timeout=420)
        if not rt.command:
            return {"reproduced": True, "repro_output": ""}
        r = sb.run(rt.command["run"], cwd=cwd, timeout=300)
        out = ci_logs.digest(r.output, 3500)
        self._say(iid, ("Reproduced the failure in the sandbox." if not r.ok else "The step passes in the sandbox (could not reproduce)."), "analysis")
        return {"reproduced": not r.ok, "repro_output": out if not r.ok else ""}

    def _after_reproduce(self, s):
        inc = self._inc(s["incident_id"])
        rt = self._runtime(s["incident_id"])
        if rt.sandbox and rt.command and not s.get("reproduced") and not inc["meta"].get("retried") and inc["kind"] != "cd":
            return "retry"
        return "diagnose"

    def _diagnose_fix(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        attempt = s.get("attempts", 0) + 1
        self._set(iid, status="fixing", attempts=attempt)
        if rt.sandbox:
            rt.sandbox.run("git checkout -- . && git clean -fdq", cwd=REPO_DIR)
        tools = build_tools(ToolCtx(self.c, rt, inc))
        user = (f"Category: {s.get('category')}\nWorkflow: {inc['workflow']}  Job: {inc['job_name']}  Step: {inc['step_name']}\n"
                f"Command: {(rt.command or {}).get('run', 'unknown (step uses an action or CI-only expressions)')}\n\n"
                f"CI log digest:\n{wrap_untrusted('ci_log', s.get('digest', ''))}\n\nSandbox reproduction output:\n{wrap_untrusted('sandbox_output', s.get('repro_output') or '(none)')}\n\n"
                f"Feedback from the previous attempt or from a maintainer:\n{wrap_untrusted('feedback', s.get('feedback') or '(none)', 4000)}")
        messages = [SystemMessage(content=prompts.CI_FIX_SYSTEM), HumanMessage(content=user)]
        try:
            model = llm.chat_model(0.1)
            reply = run_agent(model, tools, messages, settings.max_agent_steps)
            try:
                fix = CiFix.model_validate(llm.extract_json(str(reply.content)))
            except Exception:
                messages += [reply, HumanMessage(content="Return ONLY the final JSON object.")]
                fix = CiFix.model_validate(llm.extract_json(str(model.invoke(messages).content)))
        except Exception as exc:
            self._say(iid, f"The model could not produce a fix: {exc}", "error")
            return {"attempts": attempt, "fix": {}, "files": {}, "apply_failed": False}
        if fix.needs_human or not (fix.edits or fix.new_files):
            return {"attempts": attempt, "fix": fix.model_dump(), "files": {}, "apply_failed": False}
        files, diffs, err = self._apply_edits(rt, fix)
        if err:
            self._say(iid, f"Attempt {attempt}: could not apply the proposed edit ({err}).", "analysis")
            return {"attempts": attempt, "fix": fix.model_dump(), "files": {}, "apply_failed": True,
                    "feedback": f"Your previous edit could not be applied: {err}. Quote the existing text exactly."}
        self._say(iid, f"Attempt {attempt}: proposed a fix touching {', '.join(f'`{p}`' for p in files)} (confidence {fix.confidence:.0%}).", "analysis")
        stalled = bool(s.get("last_files")) and files == s["last_files"]
        if stalled:
            self._say(iid, f"Attempt {attempt}: identical to the previous proposal, so another retry would not help.", "analysis")
        return {"attempts": attempt, "fix": fix.model_dump(), "files": files, "diffs": diffs, "apply_failed": False, "feedback": "", "stalled": stalled}

    def _after_diagnose(self, s):
        if s.get("stalled"):
            return "explain"
        if not s.get("files"):
            return "again" if s.get("apply_failed") and s.get("attempts", 0) < settings.max_ci_attempts else "explain"
        return "validate"

    def _validate(self, s):
        """Prove the fix. For workflow-file fixes the failing step is re-read from the PATCHED workflow, so the replay uses the corrected
        working directory/command instead of the broken one (replaying the old config can never pass)."""
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        if not rt.sandbox:
            return {"validation_state": "unverified", "validation": "not verified (no sandbox available)"}
        self._set(iid, status="validating")
        sb, fix, files, command = rt.sandbox, s["fix"], s["files"], rt.command
        workflows = {p: c for p, c in files.items() if p.startswith(WORKFLOW_DIR) and p.endswith((".yml", ".yaml"))}
        if workflows:
            for path, text in workflows.items():
                try:
                    yaml.safe_load(text)
                except yaml.YAMLError as exc:
                    return self._validation_failed(s, f"`{path}` is not valid YAML: {str(exc)[:200]}")
            patched = workflows.get((rt.run.get("path") or "").split("@")[0])
            if patched:
                for directory in ci_logs.working_directories(patched):
                    if not self._dir_exists(sb, posixpath.join(REPO_DIR, directory)):
                        return self._validation_failed(s, f"the patched workflow uses working-directory `{directory}`, which does not exist in the repository")
                command = ci_logs.workflow_command(patched, inc["job_name"] or "", inc["step_name"] or "")
        cmds = [command["run"]] if command else list(fix.get("verify_commands", []))[:3]
        if not cmds:
            if workflows:
                self._say(iid, "The patched workflow is valid YAML and every working directory exists. The step itself could not be replayed.", "analysis")
                return {"validation_state": "static", "validation": "workflow YAML and paths checked statically; the step could not be replayed"}
            return {"validation_state": "unverified", "validation": "not verified (no command to replay)"}
        cwd = self._cwd(command)
        if not self._dir_exists(sb, cwd):
            return self._validation_failed(s, f"the working directory `{cwd.replace(REPO_DIR + '/', '')}` does not exist in the repository")
        sb.run(INSTALL, cwd=cwd, timeout=420)
        for cmd in cmds:
            r = sb.run(cmd, cwd=cwd, timeout=300)
            if not r.ok:
                return self._validation_failed(s, f"`{cmd.strip().splitlines()[0]}` still fails:\n{ci_logs.digest(r.output, 2500)}")
        first = cmds[0].strip().splitlines()[0]
        self._say(iid, f"Validated: `{first}` passes in the E2B sandbox" + (" with the patched workflow." if workflows else "."), "analysis")
        return {"validation_state": "passed", "validation": f"`{first}` passes in the E2B sandbox" + (" using the patched workflow" if workflows else "")}

    def _validation_failed(self, s, message: str):
        self._say(s["incident_id"], f"Attempt {s.get('attempts')}: the fix did not pass validation. {message[:900]}", "analysis")
        return {"validation_state": "failed", "validation": message[:300], "feedback": f"Your fix did not pass validation: {message}", "last_files": s.get("files", {})}

    def _dir_exists(self, sb, path: str) -> bool:
        return sb.run(f"test -d {quote(path)}", timeout=15).ok

    def _after_validate(self, s):
        if s.get("validation_state") == "failed":
            return "again" if s.get("attempts", 0) < settings.max_ci_attempts else "explain"
        return "prepare"

    def _prepare(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        self._close_sandbox(rt)   # the approval may take hours; never keep a sandbox alive across it
        fix, files = s["fix"], s["files"]
        reasons = []
        if s.get("validation_state") != "passed":
            reasons.append("the fix could not be verified in a sandbox")
        if fix.get("confidence", 0) < 0.6:
            reasons.append(f"low model confidence ({fix.get('confidence', 0):.0%})")
        protected = [p for p in files if any(p.startswith(x) or x in p for x in settings.protected_paths)]
        if protected:
            reasons.append("it touches protected files: " + ", ".join(protected[:3]))
        if repo_settings(rt.repo)["ci_mode"] == "approval":
            reasons.append("this repository requires approval for CI fixes")
        if inc["kind"] == "cd":
            reasons.append("this is a deployment pipeline")
        if len(files) > 6:
            reasons.append(f"large change ({len(files)} files)")
        fork = bool(rt.inc_pr and rt.inc_pr["head_repo"] != rt.repo["full_name"])
        if fork and rt.inc_pr.get("can_modify"):
            reasons.append("it would push to a contributor's fork branch")
        elif fork:
            reasons = []        # nothing will be pushed: the change is only suggested in the pull request conversation
        self._set(iid, root_cause=fix.get("root_cause"), solution=fix.get("solution"), confidence=fix.get("confidence", 0),
                  meta={**inc["meta"], "diffs": s.get("diffs", {}), "validation": s.get("validation", ""), "approval_reasons": reasons},
                  status="awaiting_approval" if reasons else "fixing")
        if reasons:
            self._say(iid, "Waiting for your approval before touching the repository: " + "; ".join(reasons) + ".", "approval")
        return {"approval_reasons": reasons}

    def _approval(self, s):
        answer = interrupt({"incident_id": s["incident_id"], "reasons": s.get("approval_reasons", []), "files": list(s.get("files", {}))})
        return {"approved": bool(answer.get("approved")), "note": (answer.get("note") or "").strip()}

    def _after_approval(self, s):
        return "apply" if s.get("approved") else "steer" if s.get("note") else "dismiss"

    def _steer(self, s):
        self._say(s["incident_id"], f"Re-planning with your guidance: “{s['note']}”", "steer", actor="you", actor_kind="human")
        return {"feedback": s["note"], "attempts": 0, "files": {}, "approved": False, "note": "", "approval_reasons": []}

    def _dismiss(self, s):
        self._set(s["incident_id"], status="dismissed")
        self._say(s["incident_id"], "Rejected. No changes were made.", "dismissed")
        return {}

    def _apply(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        stale = self.still_current(inc, rt)      # approval can take hours: never push onto a branch that has moved on
        if stale:
            self._set(iid, status=stale)
            self._say(iid, "Not applied: " + ("the branch has a newer commit." if stale == "superseded" else "the pipeline is already green."), "analysis")
            return {}
        repo, pr, fix = rt.repo, rt.inc_pr, s["fix"]
        files = s["files"]
        title = f"fix(ci): {(fix.get('solution') or inc['workflow'])[:70]}"
        message = f"{title}\n\nWarren CI Doctor — {inc['category'].replace('_', ' ')} failure in {inc['workflow']} / {inc['step_name']}.\n\n{fix.get('root_cause', '')}"
        writers = [self.c.bot_gh(repo["user_id"]), rt.gh] if settings.github_bot_token else [rt.gh]
        on_pr_branch = bool(pr and pr["state"] == "open" and pr["head_repo"] == repo["full_name"] and pr["head_ref"] == inc["branch"])
        fork_pr = bool(pr and pr["head_repo"] != repo["full_name"])
        if fork_pr and not pr.get("can_modify"):
            self._post_proposal(iid, inc, rt, s, "Warren cannot push to this branch because the pull request comes from a fork, so the change is proposed here instead.")
            return {}
        last = None
        for gh in writers:
            try:
                if fork_pr:      # the author allowed edits from maintainers
                    sha = gh.commit_files(pr["head_repo"], pr["head_ref"], files, message)
                    self.c.db.update("pull_requests", pr["id"], {"head_sha": sha, "reviewed_sha": sha, "updated_at": now()})
                    self._set(iid, status="fixed", fix_commit=sha)
                    self._say(iid, f"Fixed in `{sha[:7]}`: committed to `{pr['head_repo']}:{pr['head_ref']}` (maintainer edits are enabled).", "fix", pr["id"])
                    return {}
                if on_pr_branch:
                    sha = gh.commit_files(repo["full_name"], pr["head_ref"], files, message)
                    self.c.db.update("pull_requests", pr["id"], {"head_sha": sha, "reviewed_sha": sha, "updated_at": now()})
                    self._set(iid, status="fixed", fix_commit=sha)
                    self._say(iid, f"Fixed in `{sha[:7]}` — committed to `{pr['head_ref']}`.", "fix", pr["id"])
                    return {}
                base = repo["default_branch"] if inc["branch"] in (repo["default_branch"], "") else inc["branch"]
                branch = f"warren/ci-fix-{inc['run_id']}"
                try:
                    gh.create_branch(repo["full_name"], branch, base)
                except GitHubError as exc:
                    if exc.status != 422:
                        raise
                sha = gh.commit_files(repo["full_name"], branch, files, message)
                pr_json = gh.open_pr(repo["full_name"], title, self._report_md(inc, fix, s), branch, base)
                self._set(iid, status="fixed", fix_commit=sha, fix_pr_url=pr_json["html_url"])
                self._say(iid, f"Opened fix PR {pr_json['html_url']} (never pushing straight to `{base}`).", "fix")
                return {}
            except GitHubError as exc:
                last = exc
        if last:        # any push failure on a PR: never leave the author empty-handed, put the change in the conversation
            scope = "workflow" in str(last).lower() and any(p.startswith(WORKFLOW_DIR) for p in files)
            self._manual_patch(iid, inc, rt, s, "The token cannot modify workflow files (GitHub requires the `workflow` scope), so I could not push this change." if scope
                               else f"I could not push this change ({str(last)[:120]}), so it is proposed here instead.")
            return {}
        self._set(iid, status="failed", error=str(last))
        self._say(iid, f"Could not push the fix: {last}", "error")
        return {}

    def _manual_patch(self, iid: int, inc: dict, rt: Runtime, s, reason: str):
        """Warren may not push: hand the exact change to a human, in the pull request conversation when there is one."""
        if rt.inc_pr:
            self._post_proposal(iid, inc, rt, s, reason)
            return
        diffs = s.get("diffs") or {}
        blocks = "\n\n".join(f"`{path}`\n```diff\n{d[:2500]}\n```" for path, d in diffs.items())
        body = safe_markdown(f"### CI fix proposed\n\n**Why it broke.** {s['fix'].get('root_cause')}\n\n**Fix.** {s['fix'].get('solution')}\n\n{reason}\n\n{blocks}") + f"\n\n{MARK}"
        self._set(iid, status="proposed", meta={**self._inc(iid)["meta"], "diffs": diffs, "proposal_verified": s.get("validation_state") == "passed"})
        self._say(iid, body.replace(MARK, ""), "report")
        try:
            self.c.bot_gh(rt.repo["user_id"]).create_issue(rt.repo["full_name"], f"CI fix proposed: {inc['workflow']} on {inc['branch']}", body)
        except GitHubError:
            log.warning("could not open the proposal issue", exc_info=True)

    def _post_proposal(self, iid: int, inc: dict, rt: Runtime, s, headline: str) -> bool:
        """Put the fix where the author is looking: the pull request conversation. Lines that are part of the PR diff become GitHub
        'suggested changes' (one click to commit); anything else is posted as a diff in a comment."""
        repo, pr, fix = rt.repo, rt.inc_pr, s["fix"]
        files, diffs = s.get("files", {}), dict(s.get("diffs", {}))
        inline: list[dict] = []
        try:
            in_pr = {f["filename"]: f for f in rt.gh.pr_files(repo["full_name"], pr["number"])}
        except GitHubError:
            in_pr = {}
        for path, new in files.items():
            original, entry = rt.originals.get(path), in_pr.get(path)
            if original is None or not entry or not entry.get("patch"):
                continue
            hunks, visible = self._line_hunks(original, new), visible_lines(entry["patch"])
            if hunks and len(hunks) <= 8 and all(all(n in visible for n in range(a, b + 1)) for a, b, _ in hunks):
                for a, b, replacement in hunks:
                    item = {"path": path, "line": b, "side": "RIGHT",
                            "body": "**Suggested change**\n\n```suggestion\n" + "\n".join(replacement) + "\n```\n" + MARK}
                    if b > a:
                        item.update(start_line=a, start_side="RIGHT")
                    inline.append(item)
                diffs.pop(path, None)
        verified = s.get("validation_state") == "passed"
        blocks = "\n\n".join(f"`{path}`\n```diff\n{d[:2500]}\n```" for path, d in list(diffs.items())[:3])
        parts = [f"### CI fix proposed\n\n{headline}", f"**Why it broke.** {fix.get('root_cause')}", f"**Proposed fix.** {fix.get('solution')}",
                 f"**Verification.** {s.get('validation') or 'not verified'}"]
        if inline:
            parts.append("The change is attached to the affected lines as a suggested change. Select **Commit suggestion** on each one to apply it.")
        if blocks:
            parts.append(f"Apply this change to your branch:\n\n{blocks}")
        body = safe_markdown("\n\n".join(parts)) + f"\n\n<sub>Failing run: {inc['run_url']}</sub>\n{MARK}"
        bot, posted = self.c.bot_gh(repo["user_id"]), False
        try:
            if inline:
                try:
                    bot.create_review(repo["full_name"], pr["number"], pr["head_sha"], body, inline)
                    posted = True
                except GitHubError:     # a line GitHub will not accept for inline comments: fall back to one comment with every diff
                    inline = []
                    blocks = "\n\n".join(f"`{p}`\n```diff\n{d[:2500]}\n```" for p, d in list(s.get("diffs", {}).items())[:3])
                    body = safe_markdown("\n\n".join(parts[:4] + [f"Apply this change to your branch:\n\n{blocks}"])) + f"\n\n<sub>Failing run: {inc['run_url']}</sub>\n{MARK}"
            if not posted:
                bot.issue_comment(repo["full_name"], pr["number"], body)
                posted = True
        except GitHubError:
            log.warning("could not post the proposal", exc_info=True)
        self._set(iid, status="proposed", root_cause=fix.get("root_cause"), solution=fix.get("solution"),
                  meta={**self._inc(iid)["meta"], "diffs": s.get("diffs", {}), "proposal_verified": verified, "proposal_posted": posted})
        self._say(iid, f"Posted the proposed change on PR #{pr['number']}" + (" as suggested changes on the diff." if inline else " as a diff in a comment.")
                  if posted else "Could not post to the pull request; the proposed change is on this page.", "fix" if posted else "error")
        return posted

    @staticmethod
    def _line_hunks(original: str, new: str) -> list[tuple[int, int, list[str]]]:
        """Changed regions as (first_line, last_line, replacement_lines) on the ORIGINAL file, 1-based: exactly what a GitHub suggestion needs."""
        a, b, hunks = original.split("\n"), new.split("\n"), []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            if tag == "insert":      # a suggestion replaces lines, so anchor an insertion on its neighbour
                if i1 >= 1:
                    hunks.append((i1, i1, [a[i1 - 1], *b[j1:j2]]))
                else:
                    hunks.append((1, 1, [*b[j1:j2], a[0]]))
            else:
                hunks.append((i1 + 1, i2, b[j1:j2]))
        return hunks

    def _report(self, s):
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        if inc["status"] != "fixed" or not rt.inc_pr or inc["fix_pr_url"]:
            return {}
        try:
            self.c.bot_gh(rt.repo["user_id"]).issue_comment(rt.repo["full_name"], rt.inc_pr["number"], self._report_md(inc, s["fix"], s))
        except GitHubError:
            log.warning("could not post CI report", exc_info=True)
        return {}

    def _explain(self, s):
        """Escalation: a precise diagnosis + human steps, plus a revert PR when main is broken by its own head commit."""
        iid = s["incident_id"]
        inc, rt = self._inc(iid), self._runtime(iid)
        self._close_sandbox(rt)
        fix = s.get("fix") or {}
        if not fix.get("root_cause") or fix.get("needs_human") is False:
            try:
                fix = llm.ask_json(prompts.CI_EXPLAIN_SYSTEM, f"Workflow: {inc['workflow']} / {inc['job_name']} / {inc['step_name']}\nCategory: {s.get('category')}\n\n{s.get('digest', '')}",
                                   CiFix).model_dump()
            except Exception:
                first = next((l for l in s.get("digest", "").splitlines() if ci_logs.ERROR_LINE.search(l)), "see the log excerpt")
                fix = {"root_cause": f"The `{inc['step_name']}` step failed: {first[:200]}", "solution": "Fix the error shown in the log excerpt.", "confidence": 0.2,
                       "human_steps": "Open the run, read the failing step's log, and fix the reported error."}
        extra = {
            "fix_failed": "\n\nMy earlier fix was applied but the same failure happened again, so I stopped instead of pushing another change on top of it.",
            "own_fix_branch": "\n\nThis is one of Warren's own fix branches. I never auto-fix my own fixes — a human should look at it.",
            "pr_cap": "\n\nWarren already pushed 2 CI fixes to this PR. Stopping here to avoid an endless fix loop.",
            "mode_off": "\n\nThis repository is set to *diagnose only*, so I did not change any code.",
            "cd_no_retry": "\n\nThis is a deployment pipeline, so I did not re-run it automatically (a re-run could deploy twice).",
        }.get(s.get("reason", ""), "")
        diffs = s.get("diffs") or {}
        if s.get("validation_state") == "failed":
            extra = f"\n\nI tried {s.get('attempts', 0)} automated fix(es) but none passed validation, so nothing was committed." + (" The closest candidate is shown below for your review." if diffs else "")
        if diffs:
            blocks = "\n\n".join(f"`{path}`\n```diff\n{d[:2500]}\n```" for path, d in list(diffs.items())[:3])
            extra += f"\n\n**Proposed change{'' if s.get('validation_state') == 'passed' else ' (not verified)'}.**\n\n{blocks}"
        revert_url = self._propose_revert(iid, inc, rt, s)
        if revert_url:
            extra += f"\n\nThe failure is on the default branch and its head commit is the culprit, so I opened a revert PR: {revert_url}"
        self._set(iid, status="escalated", root_cause=fix.get("root_cause"), solution=(fix.get("solution") or "") + (f"\n\n{fix['human_steps']}" if fix.get("human_steps") else ""),
                  confidence=fix.get("confidence", 0), fix_pr_url=revert_url,
                  meta={**self._inc(iid)["meta"], "diffs": diffs, "validation": s.get("validation", ""), "proposal_verified": s.get("validation_state") == "passed"})
        body = safe_markdown(f"### CI failure analysis\n\n**Why it broke.** {fix.get('root_cause')}\n\n**Recommended fix.** {fix.get('solution')}\n\n"
                             f"{fix.get('human_steps', '')}{extra}") + f"\n\n<sub>Failing run: {inc['run_url']}</sub>\n{MARK}"
        self._say(iid, body.replace(MARK, ""), "report")
        try:
            gh = self.c.bot_gh(rt.repo["user_id"])
            if rt.inc_pr:
                gh.issue_comment(rt.repo["full_name"], rt.inc_pr["number"], body)
            elif inc["kind"] == "cd" or inc["branch"] == rt.repo["default_branch"]:
                issue = gh.create_issue(rt.repo["full_name"], f"CI/CD failure: {inc['workflow']} on {inc['branch']}", body)
                self._set(iid, meta={**self._inc(iid)["meta"], "issue_url": issue.get("html_url")})
        except GitHubError:
            log.warning("could not post escalation", exc_info=True)
        return {}

    # ------------------------------------------------------------------ helpers
    def _propose_revert(self, iid, inc, rt, s) -> str | None:
        repo = rt.repo
        if inc["branch"] != repo["default_branch"] or s.get("category") in ("flaky_infra", "permissions_config"):
            return None
        try:
            gh = self.c.bot_gh(repo["user_id"])
            if gh.branch_head(repo["full_name"], inc["branch"]) != inc["head_sha"]:
                return None   # newer commits landed; reverting the old one would be unsafe
            branch = f"warren/revert-{inc['head_sha'][:7]}"
            gh.revert_to_branch(repo["full_name"], branch, inc["head_sha"], f"Revert {inc['head_sha'][:7]} (breaks {inc['workflow']})")
            pr = gh.open_pr(repo["full_name"], f"Revert {inc['head_sha'][:7]}: {inc['workflow']} is failing on {inc['branch']}",
                            f"Warren reverted the head commit because **{inc['workflow']}** fails on `{inc['branch']}`.\n\n{s.get('digest', '')[-1200:]}", branch, inc["branch"])
            return pr["html_url"]
        except Exception as exc:
            log.info("revert proposal skipped: %s", exc)
            return None

    @staticmethod
    def _cwd(command: dict | None) -> str:
        return posixpath.join(REPO_DIR, command["cwd"]) if command and command.get("cwd") else REPO_DIR

    def _ensure_sandbox(self, iid, rt: Runtime, inc) -> bool:
        if rt.sandbox:
            return True
        try:
            sb = self.sandbox_factory().__enter__()
        except SandboxUnavailable as exc:
            self._say(iid, f"No sandbox available ({exc}); the fix cannot be verified automatically.", "analysis")
            return False
        token = self.c.user_token(rt.repo["user_id"])
        r = sb.clone_pr(rt.repo["full_name"], rt.inc_pr["number"], token) if rt.inc_pr \
            else sb.clone_ref(rt.repo["full_name"], inc["branch"], token, inc["head_sha"])
        if not r.ok:
            sb.__exit__(None, None, None)
            self._say(iid, f"Could not clone the repository in the sandbox: {r.output[:200]}", "error")
            return False
        rt.sandbox = sb
        return True

    def _apply_edits(self, rt: Runtime, fix: CiFix):
        files, diffs = {}, {}
        try:
            for item in (*fix.edits, *fix.new_files):
                item.path = safe_repo_path(item.path)
        except ValueError as exc:
            return {}, {}, str(exc)
        for e in fix.edits:
            if e.path not in files:
                text = self._read(rt, e.path)
                if text is None:
                    return {}, {}, f"{e.path} does not exist"
                rt.originals.setdefault(e.path, text)
                files[e.path] = text
            if files[e.path].count(e.old) != 1:
                return {}, {}, f"the text to replace was {'not found' if e.old not in files[e.path] else 'not unique'} in {e.path}"
            files[e.path] = files[e.path].replace(e.old, e.new, 1)
        for nf in fix.new_files:
            rt.originals.setdefault(nf.path, "")
            files[nf.path] = nf.content
        for path, new in files.items():
            diffs[path] = "".join(list(difflib.unified_diff(rt.originals.get(path, "").splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))[:400])
            if rt.sandbox:
                rt.sandbox.write(posixpath.join(REPO_DIR, path), new)
        return files, diffs, None

    def _read(self, rt: Runtime, path: str) -> str | None:
        if rt.sandbox:
            r = rt.sandbox.repo_run(f"cat {quote(path)}")
            return r.stdout if r.ok else None
        try:
            return rt.gh.file(rt.repo["full_name"], path, self.c.db.get("incidents", rt.incident_id)["head_sha"])[0]
        except GitHubError:
            return None

    def _report_md(self, inc, fix, s) -> str:
        diffs = "\n".join(f"```diff\n{d[:1800]}\n```" for d in list(s.get("diffs", {}).values())[:3])
        return (safe_markdown(f"### CI fix\n\n**Why it broke.** {fix.get('root_cause')}\n\n**Fix.** {fix.get('solution')}\n\n"
                              f"**Verification.** {s.get('validation') or 'not verified'}") + f"\n\n{diffs}\n\n<sub>Failing run: {inc['run_url']}</sub>\n{MARK}")

    def _inc(self, iid: int) -> dict:
        return self.c.db.get("incidents", iid)

    def _runtime(self, iid: int) -> Runtime:
        if iid not in self._rt:
            inc = self._inc(iid)
            repo = self.c.db.get("repos", inc["repo_id"])
            pr = self.c.db.get("pull_requests", inc["pr_id"]) if inc["pr_id"] else None
            self._rt[iid] = Runtime(incident_id=iid, repo=repo, inc_pr=pr, gh=self.c.user_gh(repo["user_id"]),
                                    command=(inc["meta"] or {}).get("command"))
        return self._rt[iid]

    def _close_sandbox(self, rt: Runtime):
        if rt.sandbox:
            rt.sandbox.__exit__(None, None, None)
            rt.sandbox = None

    def _drop(self, iid: int):
        rt = self._rt.pop(iid, None)
        if rt:
            self._close_sandbox(rt)

    def _set(self, iid: int, **values):
        self.c.db.update("incidents", iid, {**values, "updated_at": now()})
        self.c.bus.publish(f"incident:{iid}", {"type": "incident"})
        self.c.touch_board()

    def _say(self, iid: int, text: str, kind: str = "note", pr_id: int | None = None, actor: str = AGENT_NAME, actor_kind: str = "agent", to_pr: bool = True):
        pr_id = pr_id if pr_id is not None else ((self._inc(iid) or {}).get("pr_id") if to_pr else None)
        self.c.emit(pr_id, actor, actor_kind, f"ci_{kind}" if kind not in ("report", "fix", "error") else kind, text, {"incident_id": iid}, incident_id=iid)


class ToolCtx:
    """Adapter so the review agent's tools (read_file, grep, run_shell…) work for CI incidents too."""

    def __init__(self, c: Container, rt: Runtime, inc: dict):
        self.file_texts, self.vectors, self.repo, self.sandbox, self.gh = {}, c.vectors, rt.repo, rt.sandbox, rt.gh
        self.pr, self.source_repo = {"head_sha": inc["head_sha"]}, rt.repo["full_name"]
