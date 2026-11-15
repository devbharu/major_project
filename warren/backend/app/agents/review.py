"""The review pipeline, built as a LangGraph state machine:

  fetch -> index -> static -> analyze -> verify -> checks -> enrich -> publish
            |                                                (triage + change stack)
            +-- (skip: closed / draft / nothing to review) --> END
"""
import fnmatch
import logging
import posixpath
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from ..config import settings
from ..container import BOT_NAME, Container, repo_settings
from ..db import now
from ..services import llm
from ..services.codegraph import CodeGraph
from ..services.github import GitHubError, added_lines, annotate_patch, snap_to_diff, visible_lines
from ..services.safety import clean_untrusted, safe_markdown, wrap_untrusted
from ..services.sandbox import SandboxSession, SandboxUnavailable, quote
from ..services.sync import upsert_pr
from . import prompts
from .change_stack import ChangeStackBuilder
from .schemas import Finding, ReviewOutput, VerifyOutput
from .tools import build_tools, run_agent
from .triage import TriageScorer

log = logging.getLogger("warren.review")
CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs", ".php", ".cs", ".c", ".cpp", ".h", ".kt", ".swift", ".sql", ".yml", ".yaml", ".toml")
CONFIG_FILES = ("requirements.txt", "package.json", "Dockerfile")
VENDOR = re.compile(r"(node_modules|/dist/|/build/|\.min\.|vendor/|__pycache__|\.lock$)")
PROFILE_SEVERITIES = {"quiet": {"critical", "major"}, "chill": {"critical", "major", "minor"}, "assertive": {"critical", "major", "minor", "nitpick"}}
PROFILE_HINT = {"quiet": "Report only critical and major issues.", "chill": "Report critical, major and minor issues. Skip nitpicks.",
                "assertive": "Be thorough: report everything including nitpicks."}
SEV = {"critical": "Critical", "major": "Major", "minor": "Minor", "nitpick": "Nitpick"}
STATUS_WORD = {"passed": "Passed", "warning": "Needs attention", "failed": "Failed"}


@dataclass
class ReviewContext:
    pr_id: int
    progress: Any
    manual: bool = False
    pr: dict = field(default_factory=dict)
    repo: dict = field(default_factory=dict)
    gh: Any = None
    files: list = field(default_factory=list)
    source_repo: str = ""
    file_texts: dict = field(default_factory=dict)
    graph: CodeGraph | None = None
    learnings: list = field(default_factory=list)
    static: list = field(default_factory=list)
    sandbox: Any = None
    vectors: Any = None
    summary: str = ""
    file_summaries: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    premerge: list = field(default_factory=list)
    triage: dict = field(default_factory=dict)
    stack: dict = field(default_factory=dict)

    def close(self):
        if self.sandbox:
            self.sandbox.__exit__(None, None, None)
            self.sandbox = None


class ReviewState(TypedDict, total=False):
    ctx: ReviewContext
    skip: str


class ReviewAgent:
    def __init__(self, container: Container):
        self.c = container
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ graph
    def _build_graph(self):
        g = StateGraph(ReviewState)
        for name in ("fetch", "index", "static", "analyze", "verify", "checks", "enrich", "publish"):
            g.add_node(name, getattr(self, f"_{name}"))
        g.add_edge(START, "fetch")
        g.add_conditional_edges("fetch", lambda s: END if s.get("skip") else "index")
        for a, b in [("index", "static"), ("static", "analyze"), ("analyze", "verify"), ("verify", "checks"),
                     ("checks", "enrich"), ("enrich", "publish"), ("publish", END)]:
            g.add_edge(a, b)
        return g.compile()

    def run(self, pr_id: int, progress, manual: bool = False):
        ctx = ReviewContext(pr_id=pr_id, progress=progress, manual=manual, vectors=self.c.vectors)
        self.c.db.update("pull_requests", pr_id, {"review_status": "queued"})
        try:
            state = self.graph.invoke({"ctx": ctx})
            if state.get("skip"):
                self.c.db.update("pull_requests", pr_id, {"review_status": "none"})
                progress.step(f"Skipped: {state['skip']}")
        except Exception as exc:
            self.c.db.update("pull_requests", pr_id, {"review_status": "failed"})
            self.c.emit(pr_id, BOT_NAME, "bot", "error", f"Review failed: {exc}")
            raise
        finally:
            ctx.close()

    # ------------------------------------------------------------------ nodes
    def _fetch(self, state):
        ctx = state["ctx"]
        db = self.c.db
        ctx.progress.step("Fetching pull request")
        pr = db.get("pull_requests", ctx.pr_id)
        ctx.repo = db.get("repos", pr["repo_id"])
        ctx.gh = self.c.user_gh(ctx.repo["user_id"])
        gh_pr = ctx.gh.pr(ctx.repo["full_name"], pr["number"])
        upsert_pr(db, ctx.repo, gh_pr)
        ctx.pr = pr = db.get("pull_requests", ctx.pr_id)
        if pr["state"] != "open":
            return {"skip": "pull request is not open"}
        if pr["draft"] and not ctx.manual:
            return {"skip": "draft pull request"}
        ignore = repo_settings(ctx.repo)["ignore_paths"]
        ctx.files = [f for f in ctx.gh.pr_files(ctx.repo["full_name"], pr["number"])
                     if f["status"] != "removed" and f.get("patch") and not any(fnmatch.fnmatch(f["filename"], g) for g in ignore)]
        ctx.source_repo = pr["head_repo"] or ctx.repo["full_name"]
        if not ctx.files:
            return {"skip": "no reviewable changes"}
        db.update("pull_requests", ctx.pr_id, {"review_status": "running"})
        self._status(ctx, "pending", "Review in progress")
        return {}

    def _index(self, state):
        ctx = state["ctx"]
        ctx.progress.step("Indexing repository context")
        try:
            tree = ctx.gh.tree(ctx.source_repo, ctx.pr["head_sha"])
            changed = {f["filename"] for f in ctx.files}
            dirs = {posixpath.dirname(p) for p in changed}

            def eligible(t):
                p = t["path"]
                return (t["type"] == "blob" and t.get("size", 0) <= settings.max_index_file_bytes and not VENDOR.search(p)
                        and (p.endswith(CODE_EXT) or p.rsplit("/", 1)[-1] in CONFIG_FILES))

            cands = sorted((t for t in tree if eligible(t)),
                           key=lambda t: (0 if t["path"] in changed else 1 if posixpath.dirname(t["path"]) in dirs else 2,
                                          t["path"].count("/"), t.get("size", 0)))[:settings.max_index_files]

            def fetch(t):
                try:
                    return t["path"], t["sha"], ctx.gh.blob_text(ctx.source_repo, t["sha"])
                except GitHubError:
                    return None

            with ThreadPoolExecutor(8) as pool:
                for item in filter(None, pool.map(fetch, cands)):
                    path, sha, text = item
                    ctx.file_texts[path] = text
                    ctx.vectors.index_file(ctx.repo["id"], path, sha, text)
            ctx.graph = CodeGraph(ctx.file_texts)
            query = ctx.pr["title"] + "\n" + "\n".join(changed)
            ctx.learnings = ctx.vectors.search_learnings(ctx.repo["id"], query)
        except Exception:
            log.warning("indexing degraded for PR %s", ctx.pr_id, exc_info=True)
        return {}

    def _static(self, state):
        ctx = state["ctx"]
        if not settings.sandbox_enabled:
            ctx.static = [{"name": "Sandbox", "status": "skipped", "detail": "E2B_API_KEY not set - reviewed without executing code."}]
            return {}
        ctx.progress.step("Starting E2B sandbox and cloning the PR")
        try:
            sb = SandboxSession().__enter__()
        except SandboxUnavailable as exc:
            ctx.static = [{"name": "Sandbox", "status": "skipped", "detail": f"E2B unavailable: {exc}"}]
            return {}
        r = sb.clone_pr(ctx.repo["full_name"], ctx.pr["number"], self.c.user_token(ctx.repo["user_id"]))
        if not r.ok:
            ctx.static = [{"name": "Clone & checkout", "status": "failed", "detail": r.output[:300]}]
            sb.__exit__(None, None, None)
            return {}
        ctx.sandbox = sb
        ctx.static = [{"name": "Clone & checkout", "status": "passed", "detail": f"PR #{ctx.pr['number']} at {ctx.pr['head_sha'][:7]}"}]
        ctx.progress.step("Running static analysis in the sandbox")
        changed = [f["filename"] for f in ctx.files]
        py = [p for p in changed if p.endswith(".py")]
        if py:
            r = sb.repo_run("python3 -m py_compile " + " ".join(quote(p) for p in py))
            ctx.static.append({"name": "Python syntax", "status": "passed" if r.ok else "failed", "detail": r.output[:600] or "all files compile"})
            sb.repo_run("pip install -q ruff 2>&1 | tail -1", timeout=120)
            r = sb.repo_run("ruff check --output-format=concise " + " ".join(quote(p) for p in py) + " | head -40")
            clean = not r.stdout.strip() or "All checks passed" in r.stdout
            ctx.static.append({"name": "Ruff lint", "status": "passed" if clean else "failed", "detail": "" if clean else r.stdout[:1500]})
        for p in (x for x in changed if x.endswith((".js", ".mjs", ".cjs"))):
            r = sb.repo_run(f"node --check {quote(p)}")
            ctx.static.append({"name": f"node --check {p}", "status": "passed" if r.ok else "failed", "detail": r.output[:400]})
        return {}

    def _analyze(self, state):
        ctx = state["ctx"]
        s = repo_settings(ctx.repo)
        batches = self._batches(ctx.files)
        tools = build_tools(ctx)
        llm_ = llm.chat_model(temperature=0.1)
        static_txt = "\n".join(f"- {c['name']}: {c['status']} {c['detail'][:500]}" for c in ctx.static) or "none"
        found, summaries, errors = [], [], 0
        for i, diff in enumerate(batches, 1):
            ctx.progress.step(f"Reviewing changes with the agent (batch {i}/{len(batches)})")
            user = prompts.REVIEW_USER.format(
                number=ctx.pr["number"], title=clean_untrusted(ctx.pr["title"], 300), author=ctx.pr["author"], base=ctx.pr["base_ref"], head=ctx.pr["head_ref"],
                description=wrap_untrusted("pr_description", ctx.pr["body"] or "(none)", 2500), instructions=s["instructions"] or "(none)",
                learnings="\n".join(f"- {l}" for l in ctx.learnings) or "(none)",
                graph=ctx.graph.summary([f["filename"] for f in ctx.files]) if ctx.graph else "unavailable",
                static=wrap_untrusted("static_analysis", static_txt, 4000), profile=PROFILE_HINT[s["profile"]], batch=i, batches=len(batches), diff=wrap_untrusted("diff", diff, settings.max_diff_chars + 5000))
            messages = [SystemMessage(content=prompts.REVIEW_SYSTEM.replace("{max_findings}", str(settings.max_findings))), HumanMessage(content=user)]
            try:
                reply = run_agent(llm_, tools, messages, settings.max_agent_steps)
                text = reply.content if isinstance(reply.content, str) else str(reply.content)
                try:
                    out = ReviewOutput.model_validate(llm.extract_json(text))
                except Exception:
                    messages += [reply, HumanMessage(content="Return ONLY the final JSON object described in the system prompt.")]
                    out = ReviewOutput.model_validate(llm.extract_json(str(llm_.invoke(messages).content)))
                found += out.findings
                summaries.append(out.summary)
                ctx.file_summaries.update(out.file_summaries)
            except Exception:
                errors += 1
                log.warning("review batch %s failed", i, exc_info=True)
        if errors == len(batches):
            raise RuntimeError("the LLM did not return a usable review (check OMNIROUTE_BASE_URL / OMNIROUTE_API_KEY / LLM_MODEL)")
        allowed = PROFILE_SEVERITIES[s["profile"]]
        changed = {f["filename"] for f in ctx.files}
        seen, merged = set(), []
        for f in sorted(found, key=lambda f: list(SEV).index(f.severity)):
            key = (f.path, f.line, f.title.lower())
            if f.severity in allowed and f.confidence >= settings.min_confidence and key not in seen and (f.path in changed or f.path is None):
                seen.add(key)
                merged.append(f)
        ctx.findings = merged[:settings.max_findings]
        ctx.summary = " ".join(x for x in summaries if x).strip() or ctx.pr["title"]
        return {}

    def _verify(self, state):
        ctx = state["ctx"]
        risky = [(i, f) for i, f in enumerate(ctx.findings) if f.severity in ("critical", "major") and f.path]
        if not risky:
            return {}
        ctx.progress.step("Verifying suspected issues")
        try:
            blocks = []
            for i, f in risky:
                lines = ctx.file_texts.get(f.path, "").splitlines()
                lo = max((f.line or 1) - 15, 0)
                excerpt = "\n".join(f"{n}: {t}" for n, t in enumerate(lines[lo:lo + 30], lo + 1))
                blocks.append(f"[id {i}] {f.severity} {f.title} @ {f.path}:{f.line}\n{f.body}\n```\n{excerpt}\n```")
            out = llm.ask_json(prompts.VERIFY_SYSTEM, "\n\n".join(blocks), VerifyOutput)
            drop = {item.id for item in out.items if not item.keep}
            ctx.findings = [f for i, f in enumerate(ctx.findings) if i not in drop]
        except Exception:
            log.info("verification pass unavailable, keeping all findings")
        return {}

    def _checks(self, state):
        ctx = state["ctx"]
        pr, checks = ctx.pr, []
        body = (pr["body"] or "").strip()
        checks.append(("Description Check", "passed" if len(body) >= 20 else "warning",
                       "The PR description gives reviewers context." if len(body) >= 20 else "The PR description is empty or very short."))
        generic = re.fullmatch(r"(update|fix|changes?|wip|misc|stuff)\.?", pr["title"].strip().lower())
        ok = 8 <= len(pr["title"]) <= 100 and not generic
        checks.append(("Title Check", "passed" if ok else "warning", "The title is specific and readable." if ok else "The title is too generic or too long."))
        defs = documented = 0
        for f in ctx.files:
            if f["filename"].endswith(".py"):
                added = dict(added_lines(f.get("patch")))
                for n, text in added.items():
                    if re.match(r"\s*(async\s+def|def|class)\s+\w", text) and not text.strip().startswith("def _"):
                        defs += 1
                        if any(re.match(r"\s*[ru]?(\"\"\"|''')", added.get(n + k, "")) for k in (1, 2, 3)):
                            documented += 1
        if defs:
            pct = round(100 * documented / defs)
            checks.append(("Docstring Coverage", "passed" if pct >= 80 else "warning", f"{documented}/{defs} new public functions/classes documented ({pct}%, threshold 80%)."))
        else:
            checks.append(("Docstring Coverage", "passed", "No changed functions require docstrings."))
        names = [f["filename"] for f in ctx.files]
        src = [n for n in names if n.endswith(CODE_EXT) and not re.search(r"(test|spec)", n, re.I)]
        tests = [n for n in names if re.search(r"(test|spec)", n, re.I)]
        checks.append(("Test Changes", "passed" if tests or not src else "warning",
                       "Tests were updated alongside the code." if tests or not src else "Source files changed but no tests were touched."))
        for s in ctx.static:
            if s["status"] in ("passed", "failed") and s["name"] != "Clone & checkout":
                checks.append((s["name"], "passed" if s["status"] == "passed" else "failed", (s["detail"] or "ok")[:160]))
        ctx.premerge = [{"name": n, "status": s, "explanation": e} for n, s, e in checks]
        return {}

    def _enrich(self, state):
        ctx = state["ctx"]
        ctx.progress.step("Scoring priority and building the Change Stack")
        changed = [f["filename"] for f in ctx.files]
        dependents = ctx.graph.dependents(changed) if ctx.graph else {}
        findings = [{"severity": f.severity, "status": "open"} for f in ctx.findings]
        scorer = TriageScorer(self.c)
        try:
            reviewers = scorer.suggest_reviewers(ctx.gh, ctx.repo, ctx.pr, ctx.files)
            pr = {**ctx.pr, "review_status": "done", "reviewed_sha": ctx.pr["head_sha"]}
            ctx.triage = scorer.score(pr, ctx.files, findings, len(sum(dependents.values(), [])), use_llm=True, reviewers=reviewers)
        except Exception:
            log.warning("triage failed", exc_info=True)
        try:
            ctx.stack = ChangeStackBuilder(self.c).build(ctx.pr, ctx.files, dependents, findings, ctx.static + [
                {"name": c["name"], "status": c["status"], "detail": c["explanation"]} for c in ctx.premerge])
        except Exception:
            log.warning("change stack failed", exc_info=True)
        return {}

    def _publish(self, state):
        ctx = state["ctx"]
        ctx.progress.step("Posting review")
        ReviewPublisher(self.c).publish(ctx)
        return {}

    # ------------------------------------------------------------------ helpers
    def _status(self, ctx, state: str, description: str):
        """Publish the 'warren/pre-merge' commit status so branch protection can require it (best effort)."""
        if not (repo_settings(ctx.repo)["post_comments"] and repo_settings(ctx.repo)["commit_status"]):
            return
        try:
            self.c.bot_gh(ctx.repo["user_id"]).set_status(ctx.repo["full_name"], ctx.pr["head_sha"], state, description, url=ctx.pr.get("url", ""))
        except GitHubError:
            log.info("could not publish the commit status (token needs the repo:status scope)")

    @staticmethod
    def _batches(files: list[dict]) -> list[str]:
        batches, cur, size = [], [], 0
        for f in files:
            chunk = f"### {f['filename']} ({f['status']}, +{f['additions']}/-{f['deletions']})\n```diff\n{annotate_patch(f['patch'])[:12000]}\n```\n"
            if cur and size + len(chunk) > settings.max_diff_chars:
                batches.append("".join(cur))
                cur, size = [], 0
                if len(batches) >= settings.max_review_batches:
                    return batches
            cur.append(chunk)
            size += len(chunk)
        if cur:
            batches.append("".join(cur))
        return batches


# ---------------------------------------------------------------------- publishing
def finding_markdown(fid: int, f: Finding, with_suggestion: bool = True) -> str:
    """One review comment: severity and category, a specific title, the reasoning, the impact, and a committable fix when exact."""
    parts = [f"**{SEV[f.severity]}** · {f.category}", f"**{safe_markdown(f.title.strip())}**", safe_markdown(f.body.strip(), settings.bot_mention)]
    if f.impact.strip():
        parts.append(f"**Impact.** {safe_markdown(f.impact.strip(), settings.bot_mention)}")
    if with_suggestion and f.suggestion and f.suggestion.strip():
        parts.append("**Suggested change**\n\n```suggestion\n" + f.suggestion.rstrip("\n") + "\n```")
    parts.append(f"<!-- warren:finding:{fid} -->")
    return "\n\n".join(p for p in parts if p)


def assessment(rows: list[tuple[int, Finding]], checks: list[dict]) -> tuple[str, str, str]:
    """(label, commit-status state, detail). Blocking = critical/major findings or a failed check."""
    crit = sum(f.severity == "critical" for _, f in rows)
    major = sum(f.severity == "major" for _, f in rows)
    failed = [c["name"] for c in checks if c["status"] == "failed"]
    if crit or major or failed:
        parts = [f"{n} {w}" for n, w in ((crit, "critical"), (major, "major")) if n] + ([f"{len(failed)} failed check(s)"] if failed else [])
        return "Changes requested", "failure", ", ".join(parts) + " to resolve before merge"
    if rows:
        return "Approved with comments", "success", f"{len(rows)} non-blocking finding(s)"
    return "No issues found", "success", "No blocking findings"


def summary_markdown(ctx: ReviewContext, rows: list[tuple[int, Finding]], outside: list[tuple[int, Finding]]) -> str:
    label, _, detail = assessment(rows, ctx.premerge)
    out = ["<!-- warren:summary -->", f"## Review summary\n\n{safe_markdown(ctx.summary, settings.bot_mention)}", f"**Assessment: {label}.** {detail[0].upper() + detail[1:]}."]
    if rows:
        table = "\n".join(f"| {SEV[f.severity]} | {f.category} | `{f.path or '-'}{':' + str(f.line) if f.line else ''}` | {safe_markdown(f.title.strip()).replace('|', '/')} |" for _, f in rows)
        out.append(f"| Severity | Category | Location | Finding |\n| --- | --- | --- | --- |\n{table}")
    if ctx.file_summaries:
        table = "\n".join(f"| `{p}` | {safe_markdown(t).replace('|', '/')} |" for p, t in list(ctx.file_summaries.items())[:25])
        out.append(f"<details><summary>Changes by file</summary>\n\n| File | Summary |\n| --- | --- |\n{table}\n\n</details>")
    passed = sum(c["status"] == "passed" for c in ctx.premerge)
    rows_md = "\n".join(f"| {c['name']} | {STATUS_WORD[c['status']]} | {safe_markdown(c['explanation']).replace('|', '/')} |" for c in ctx.premerge)
    out.append(f"<details><summary>Pre-merge checks ({passed} of {len(ctx.premerge)} passed)</summary>\n\n| Check | Result | Details |\n| --- | --- | --- |\n{rows_md}\n\n</details>")
    if outside:
        out.append("**Additional findings** (not commented inline)\n\n" + "\n\n".join(finding_markdown(i, f, False) for i, f in outside))
    out.append(f"<sub>Reviewed commit `{ctx.pr['head_sha'][:7]}`. Reply `{settings.bot_mention} fix` to apply fixes, `{settings.bot_mention} review` to run again, or ask a question. "
               f"Review profile: {repo_settings(ctx.repo)['profile']}.</sub>")
    return "\n\n".join(out)


class ReviewPublisher:
    def __init__(self, container: Container):
        self.c = container

    def publish(self, ctx: ReviewContext):
        db, pr = self.c.db, ctx.pr
        existing = {(f["path"], f["title"].lower()) for f in db.query("SELECT path,title FROM findings WHERE pr_id=? AND source='review'", (ctx.pr_id,))}
        visible = {f["filename"]: visible_lines(f.get("patch")) for f in ctx.files}
        rows, inline, outside = [], [], []
        for f in sorted(ctx.findings, key=lambda f: (list(SEV).index(f.severity), -f.confidence)):
            if (f.path, f.title.lower()) in existing:
                continue
            vis = visible.get(f.path or "", set())
            line = snap_to_diff(f.line, vis) if f.path else None
            exact = line is not None and line == f.line and (f.end_line is None or f.end_line in vis)
            if not exact:
                f.suggestion = None
            old = None
            if f.suggestion and f.path in ctx.file_texts and f.line:
                old = "\n".join(ctx.file_texts[f.path].splitlines()[f.line - 1:(f.end_line or f.line)])
            fid = db.insert("findings", {
                "pr_id": ctx.pr_id, "repo_id": ctx.repo["id"], "source": "review", "severity": f.severity, "category": f.category,
                "title": f.title, "body": f.body, "path": f.path, "line": line or f.line, "end_line": f.end_line if exact else None,
                "suggestion": f.suggestion, "status": "open", "meta": {"old_code": old, "impact": f.impact, "confidence": f.confidence}, "created_at": now()})
            rows.append((fid, f))
            if line is not None and len(inline) < settings.max_inline_comments:     # precision over volume: the rest live in the summary
                item = {"path": f.path, "line": f.end_line if exact and f.end_line else line, "side": "RIGHT", "body": finding_markdown(fid, f)}
                if exact and f.end_line and f.end_line > line:
                    item.update(start_line=line, start_side="RIGHT")
                inline.append((fid, item))
            else:
                outside.append((fid, f))
        body = summary_markdown(ctx, rows, outside)
        label, state, detail = assessment(rows, ctx.premerge)
        posted_ok = False
        if repo_settings(ctx.repo)["post_comments"]:
            posted_ok = self._post(ctx, body, inline, rows, outside)
            if repo_settings(ctx.repo)["commit_status"]:
                try:
                    self.c.bot_gh(ctx.repo["user_id"]).set_status(ctx.repo["full_name"], pr["head_sha"], state, f"{label}: {detail}", url=pr.get("url", ""))
                except GitHubError:
                    log.info("could not publish the commit status")
        # timeline (what the dashboard shows): readable prose, not the GitHub table markup
        self.c.emit(ctx.pr_id, BOT_NAME, "bot", "summary", f"{safe_markdown(ctx.summary)}\n\n**Assessment: {label}.** {detail[0].upper() + detail[1:]}.",
                    {"posted": posted_ok, "head_sha": pr["head_sha"]})
        for fid, f in rows:
            row = db.get("findings", fid)
            self.c.emit(ctx.pr_id, BOT_NAME, "bot", "finding", f"**{safe_markdown(f.title)}**\n\n{safe_markdown(f.body)}" + (f"\n\n**Impact.** {safe_markdown(f.impact)}" if f.impact else ""),
                        {"finding_id": fid, "severity": f.severity, "category": f.category, "path": row["path"], "line": row["line"],
                         "old_code": row["meta"].get("old_code"), "new_code": f.suggestion})
        db.update("pull_requests", ctx.pr_id, {
            "review_status": "done", "reviewed_sha": pr["head_sha"], "summary": ctx.summary, "premerge": ctx.premerge,
            "triage": ctx.triage or pr.get("triage") or {}, "change_stack": ctx.stack or {}, "updated_at": now()})
        self.c.touch_board()
        if repo_settings(ctx.repo)["auto_fix"] and rows:
            from .autofix import AutofixAgent
            self.c.jobs.submit("autofix", lambda p: AutofixAgent(self.c).run(ctx.pr_id, None, p), pr_id=ctx.pr_id)

    def _post(self, ctx, body, inline, rows, outside) -> bool:
        bot = self.c.bot_gh(ctx.repo["user_id"])
        name, number = ctx.repo["full_name"], ctx.pr["number"]
        try:
            bot.create_review(name, number, ctx.pr["head_sha"], body, [item for _, item in inline])
        except GitHubError as exc:
            log.warning("inline review rejected (%s); falling back to a single comment", exc)
            try:
                inline_ids = {i for i, _ in inline}
                details = "\n\n".join(f"**{f.path}:{f.line}**\n\n{finding_markdown(i, f, False)}" for i, f in rows if i in inline_ids)
                bot.issue_comment(name, number, body + ("\n\n" + details if details else ""))
            except GitHubError as exc2:
                self.c.emit(ctx.pr_id, BOT_NAME, "bot", "error", f"Could not post to GitHub: {exc2}")
                return False
        try:  # remember GitHub comment ids so replies/fixes can target the right thread
            markers = {int(m.group(1)): c["id"] for c in bot.review_comments(name, number)
                       if (m := re.search(r"warren:finding:(\d+)", c["body"]))}
            for fid, _ in rows:
                if fid in markers:
                    self.c.db.update("findings", fid, {"comment_id": markers[fid]})
        except GitHubError:
            pass
        return True
