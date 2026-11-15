"""Security agent: dependency advisories (OSV) + AI deep scan of risky files + 'fix it in a PR' for dependency bumps."""
import logging
import posixpath
import re

from ..config import settings
from ..container import Container
from ..db import now
from ..services import llm
from ..services import security as osv
from ..services.github import GitHubError
from . import prompts
from .review import CODE_EXT, VENDOR
from .schemas import SecurityOutput, VerifyOutput

log = logging.getLogger("warren.security")
RISKY = re.compile(r"(auth|login|session|token|secret|crypt|passw|admin|upload|payment|billing|sql|query|exec|shell|webhook|route|api|handler|server|db|oauth|jwt)", re.I)


class SecurityScanner:
    def __init__(self, container: Container):
        self.c = container

    def run(self, repo_id: int, progress):
        db = self.c.db
        repo = db.get("repos", repo_id)
        gh = self.c.user_gh(repo["user_id"])
        progress.step("Listing repository files")
        tree = [t for t in gh.tree(repo["full_name"], repo["default_branch"]) if t["type"] == "blob" and not VENDOR.search(t["path"])]

        progress.step("Checking dependencies against OSV")
        manifests = {t["path"]: gh.blob_text(repo["full_name"], t["sha"]) for t in tree
                     if posixpath.basename(t["path"]) in osv.MANIFESTS and t["path"].count("/") < 4}
        dep_findings = osv.scan(osv.parse_manifests(manifests))

        code = [t for t in tree if t["path"].endswith(CODE_EXT) and t.get("size", 0) <= settings.max_index_file_bytes]
        risky = sorted((t for t in code if RISKY.search(t["path"])), key=lambda t: t["path"].count("/"))[:8]
        ai_findings, texts = [], {}
        for i, t in enumerate(risky, 1):
            progress.step(f"Deep scanning {t['path']} ({i}/{len(risky)})")
            try:
                texts[t["path"]] = text = gh.blob_text(repo["full_name"], t["sha"])
                numbered = "\n".join(f"{n}: {l}" for n, l in enumerate(text.splitlines(), 1))[:24000]
                ai_findings += llm.ask_json(prompts.SECURITY_SYSTEM, f"File: {t['path']}\n\n{numbered}", SecurityOutput).findings
            except Exception:
                log.warning("deep scan failed for %s", t["path"], exc_info=True)
        ai_findings = self._verify(ai_findings, texts)

        db.execute("DELETE FROM findings WHERE repo_id=? AND source IN ('security','dependency') AND status IN ('open','failed')", (repo_id,))
        for d in dep_findings:
            db.insert("findings", {"repo_id": repo_id, "source": "dependency", "severity": d["severity"], "category": "Dependency vulnerability",
                                   "title": f"{d['package']} · {d['vuln_id']}", "body": d["summary"], "path": d["manifest"], "status": "open",
                                   "meta": {k: d[k] for k in ("package", "version", "ecosystem", "fixed_in", "vuln_id")}, "created_at": now()})
        for f in ai_findings:
            db.insert("findings", {"repo_id": repo_id, "source": "security", "severity": f.severity, "category": "Security",
                                   "title": f.title, "body": f.body, "path": f.path, "line": f.line, "suggestion": f.suggestion,
                                   "status": "open", "meta": {"verified": True}, "created_at": now()})
        bad_paths = {f.path for f in ai_findings}
        counts = dict.fromkeys(("critical", "high", "medium", "low"), 0)
        for x in dep_findings:
            counts[x["severity"]] += 1
        for x in ai_findings:
            counts[x.severity] += 1
        counts.update(healthy=max(len(risky) - len(bad_paths), 0), unscanned=max(len(code) - len(risky), 0),
                      files_scanned=len(risky), dependencies=len(dep_findings))
        db.insert("scans", {"repo_id": repo_id, "kind": "full", "counts": counts, "created_at": now()})

    def _verify(self, findings, texts):
        """Second pass: keep only findings the model can justify from the code (cuts false positives)."""
        if not findings:
            return findings
        try:
            blocks = []
            for i, f in enumerate(findings):
                lines = texts.get(f.path, "").splitlines()
                lo = max((f.line or 1) - 12, 0)
                blocks.append(f"[id {i}] {f.severity} {f.title} @ {f.path}:{f.line}\n{f.body}\n```\n" +
                              "\n".join(f"{n}: {t}" for n, t in enumerate(lines[lo:lo + 26], lo + 1)) + "\n```")
            out = llm.ask_json(prompts.VERIFY_SYSTEM, "\n\n".join(blocks), VerifyOutput)
            drop = {item.id for item in out.items if not item.keep}
            return [f for i, f in enumerate(findings) if i not in drop]
        except Exception:
            return findings

    def open_fix_pr(self, finding_id: int) -> str:
        """Bump a vulnerable dependency on a new branch and open a PR (the 'Fix ready' button)."""
        db = self.c.db
        f = db.get("findings", finding_id)
        meta = f["meta"]
        if f["source"] != "dependency" or not meta.get("fixed_in"):
            raise ValueError("no fixed version is known for this advisory")
        repo = db.get("repos", f["repo_id"])
        gh = self.c.bot_gh(repo["user_id"])
        text, _ = self.c.user_gh(repo["user_id"]).file(repo["full_name"], f["path"], repo["default_branch"])
        new_text = osv.apply_bump(f["path"], text, meta["package"], meta["fixed_in"])
        if new_text == text:
            raise ValueError("could not locate the dependency in the manifest")
        branch = re.sub(r"[^A-Za-z0-9._/-]", "-", f"warren/fix-{meta['package']}-{meta['vuln_id']}")[:80]
        try:
            gh.create_branch(repo["full_name"], branch, repo["default_branch"])
        except GitHubError as exc:
            if exc.status != 422:  # 422 = branch already exists, reuse it
                raise
        gh.commit_files(repo["full_name"], branch, {f["path"]: new_text}, f"fix(deps): bump {meta['package']} to {meta['fixed_in']}")
        pr = gh.open_pr(repo["full_name"], f"fix(deps): bump {meta['package']} to {meta['fixed_in']}",
                        f"Fixes **{meta['vuln_id']}** in `{meta['package']}` {meta['version']}.\n\n{f['body']}\n\n_Opened by Warren Security._",
                        branch, repo["default_branch"])
        db.update("findings", finding_id, {"status": "fixed", "meta": {**meta, "pr_url": pr["html_url"]}})
        return pr["html_url"]
