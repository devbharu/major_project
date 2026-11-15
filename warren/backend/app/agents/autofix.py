"""Autofix agent: turn review findings into a verified commit on the PR branch.

  patch (deterministic suggestion, else LLM) -> verify in E2B sandbox -> one commit -> reply in thread -> re-check
"""
import logging
import posixpath

from ..config import settings
from ..container import AGENT_NAME, BOT_NAME, Container
from ..db import now
from ..services import llm
from ..services.github import GitHubError
from ..services.safety import safe_markdown, safe_repo_path
from ..services.sandbox import REPO_DIR, SandboxSession, SandboxUnavailable, quote
from . import prompts
from .schemas import FixCheck, FixPatch

log = logging.getLogger("warren.autofix")


class AutofixAgent:
    def __init__(self, container: Container):
        self.c = container

    def run(self, pr_id: int, finding_ids: list[int] | None, progress):
        db = self.c.db
        pr = db.get("pull_requests", pr_id)
        repo = db.get("repos", pr["repo_id"])
        user_gh = self.c.user_gh(repo["user_id"])
        if pr["head_repo"] != repo["full_name"] and not pr.get("can_modify"):
            suggestions = db.one("SELECT COUNT(*) n FROM findings WHERE pr_id=? AND status='open' AND suggestion IS NOT NULL", (pr_id,))["n"]
            text = ("Warren cannot push to this branch because the pull request comes from a fork. "
                    + (f"{suggestions} finding(s) include a committable suggestion in the review above; select **Commit suggestion** to apply each one."
                       if suggestions else "The findings above describe the changes needed; none has an exact committable suggestion."))
            self.c.emit(pr_id, AGENT_NAME, "agent", "note", text)
            try:
                self.c.bot_gh(repo["user_id"]).issue_comment(repo["full_name"], pr["number"], f"{text}\n\n<!-- warren:autofix -->")
            except GitHubError:
                log.warning("could not post the fork notice", exc_info=True)
            return
        rows = db.query("SELECT * FROM findings WHERE pr_id=? AND source='review' AND status='open' AND path IS NOT NULL ORDER BY line DESC", (pr_id,))
        if finding_ids:
            rows = [r for r in rows if r["id"] in finding_ids]
        if not rows:
            self.c.emit(pr_id, AGENT_NAME, "agent", "note", "No open findings to fix.")
            return

        progress.step("Preparing patches")
        files: dict[str, str] = {}
        applied: dict[int, dict] = {}   # finding id -> {old, new, explanation, path}
        tests: dict[str, str] = {}
        for path in sorted({r["path"] for r in rows}):
            try:
                text, _ = user_gh.file(repo["full_name"], path, pr["head_ref"])
            except GitHubError as exc:
                for r in (r for r in rows if r["path"] == path):
                    self._fail(r, f"cannot read {path}: {exc}")
                continue
            lines, floor = text.split("\n"), len(text.split("\n")) + 1
            for f in (r for r in rows if r["path"] == path):   # already ordered bottom-to-top
                patch = self._patch_for(f, lines)
                if not patch or not (1 <= patch.start_line <= patch.end_line <= len(lines)) or patch.end_line >= floor:
                    self._fail(f, "could not produce a safe patch")
                    continue
                old = "\n".join(lines[patch.start_line - 1:patch.end_line])
                lines[patch.start_line - 1:patch.end_line] = patch.replacement.rstrip("\n").split("\n")
                floor = patch.start_line
                applied[f["id"]] = {"old": old, "new": patch.replacement.rstrip("\n"), "explanation": patch.explanation or f["title"], "path": path}
                if patch.test_path and patch.test_content:
                    try:
                        tests[safe_repo_path(patch.test_path)] = patch.test_content
                    except ValueError:
                        log.warning("ignored unsafe test path %r", patch.test_path)
            files[path] = "\n".join(lines)

        progress.step("Verifying patches in the sandbox")
        sandbox_note = self._verify_in_sandbox(pr, repo, user_gh, files, tests, applied)
        if not files:
            self.c.emit(pr_id, AGENT_NAME, "agent", "error", "None of the fixes passed verification, so nothing was committed.")
            return

        progress.step("Committing to the PR branch")
        titles = "\n".join(f"- {db.get('findings', fid)['title']}" for fid in applied)
        message = f"fix: apply Warren autofix for {len(applied)} review finding(s)\n\n{titles}"
        sha = None
        for gh in (self.c.bot_gh(repo["user_id"]), user_gh):
            try:
                sha = gh.commit_files(repo["full_name"], pr["head_ref"], {**files, **tests}, message)
                break
            except GitHubError as exc:
                log.warning("commit failed with one token: %s", exc)
                last_error = exc
        if not sha:
            self.c.emit(pr_id, AGENT_NAME, "agent", "error", f"Could not push the fix: {last_error}")
            return
        db.update("pull_requests", pr_id, {"head_sha": sha, "reviewed_sha": sha, "updated_at": now()})

        progress.step("Re-checking the fixes")
        bot = self.c.bot_gh(repo["user_id"])
        for fid, info in applied.items():
            f = db.get("findings", fid)
            short = sha[:7]
            text = f"Fixed in `{short}`. {safe_markdown(info['explanation'])}" + (f" Added a regression test: `{next(iter(tests))}`." if tests else "")
            self.c.emit(pr_id, AGENT_NAME, "agent", "fix", text, {"finding_id": fid, "commit": sha, "path": info["path"],
                                                                 "old_code": info["old"], "new_code": info["new"]})
            self._reply(bot, repo, pr, f, text)
            resolved, reason = self._recheck(f, info, sandbox_note)
            db.update("findings", fid, {"status": "verified" if resolved else "fixed", "commit_sha": sha})
            verdict = f"Verified. {safe_markdown(reason)}" if resolved else f"Applied, but not fully verified: {safe_markdown(reason)}"
            self.c.emit(pr_id, BOT_NAME, "bot", "verify", verdict, {"finding_id": fid})
            self._reply(bot, repo, pr, f, verdict)

    # ------------------------------------------------------------------ steps
    def _patch_for(self, f: dict, lines: list[str]) -> FixPatch | None:
        start, end = f["line"] or 1, f["end_line"] or f["line"] or 1
        old_recorded = (f["meta"] or {}).get("old_code")
        current = "\n".join(lines[start - 1:end])
        if f["suggestion"] is not None and f["line"] and (old_recorded is None or old_recorded == current):
            return FixPatch(start_line=start, end_line=end, replacement=f["suggestion"], explanation=f["title"])
        lo = max(start - 40, 0)
        excerpt = "\n".join(f"{n}: {t}" for n, t in enumerate(lines[lo:lo + 90], lo + 1))
        try:
            return llm.ask_json(prompts.FIX_SYSTEM, f"Finding ({f['severity']}): {f['title']}\n{f['body']}\nFile: {f['path']} around line {start}\n\n{excerpt}", FixPatch)
        except Exception:
            log.warning("LLM could not produce a patch for finding %s", f["id"], exc_info=True)
            return None

    def _verify_in_sandbox(self, pr, repo, gh, files, tests, applied) -> str:
        """Syntax-check every patched file in E2B; drop files that break. Keep generated tests only if they pass."""
        if not settings.sandbox_enabled:
            tests.clear()   # never commit a test nobody has run
            return "unverified (no sandbox)"
        try:
            with SandboxSession() as sb:
                token = self.c.user_token(repo["user_id"])
                if not sb.clone_pr(repo["full_name"], pr["number"], token).ok:
                    tests.clear()
                    return "unverified (clone failed)"
                for path, content in list(files.items()):
                    sb.write(f"{REPO_DIR}/{path}", content)
                    cmd = {".py": "python3 -m py_compile", ".js": "node --check", ".mjs": "node --check"}.get(posixpath.splitext(path)[1])
                    if cmd:
                        r = sb.repo_run(f"{cmd} {quote(path)}")
                        if not r.ok:
                            files.pop(path)
                            for fid, info in list(applied.items()):
                                if info["path"] == path:
                                    applied.pop(fid)
                                    self._fail(self.c.db.get("findings", fid), f"fix failed the syntax check: {r.output[:200]}")
                if tests and files:
                    sb.repo_run("pip install -q pytest 2>&1 | tail -1", timeout=120)
                    for tpath, tcontent in list(tests.items()):
                        sb.write(f"{REPO_DIR}/{tpath}", tcontent)
                        r = sb.repo_run(f"python3 -m pytest -x -q {quote(tpath)}", timeout=180) if tpath.endswith(".py") else None
                        if r is None or not r.ok:
                            tests.pop(tpath)
                return "verified by syntax check" + (" and generated tests" if tests else "") + " in the E2B sandbox"
        except SandboxUnavailable as exc:
            tests.clear()
            return f"unverified (sandbox unavailable: {exc})"

    def _recheck(self, f: dict, info: dict, sandbox_note: str) -> tuple[bool, str]:
        try:
            check = llm.ask_json(prompts.FIXCHECK_SYSTEM, f"Finding: {f['title']}\n{f['body']}\n\nBefore:\n{info['old']}\n\nAfter:\n{info['new']}\n\nSandbox: {sandbox_note}", FixCheck)
            return check.resolved, check.reason or sandbox_note
        except Exception:
            return sandbox_note.startswith("verified"), sandbox_note

    def _reply(self, bot, repo, pr, f, text):
        marker = "<!-- warren:autofix -->"
        try:
            if f.get("comment_id"):
                bot.reply_to_review_comment(repo["full_name"], pr["number"], f["comment_id"], f"{text}\n\n{marker}")
            else:
                bot.issue_comment(repo["full_name"], pr["number"], f"**{f['title']}** — {text}\n\n{marker}")
        except GitHubError:
            log.warning("could not reply on GitHub for finding %s", f["id"], exc_info=True)

    def _fail(self, f: dict, reason: str):
        self.c.db.update("findings", f["id"], {"status": "failed", "meta": {**(f["meta"] or {}), "fail_reason": reason}})
        self.c.emit(f["pr_id"], AGENT_NAME, "agent", "error", f"Skipped “{f['title']}”: {reason}")
