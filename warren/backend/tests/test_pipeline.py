"""End-to-end test of review -> autofix -> chat with a fake GitHub and a fake LLM (no network, no keys).
Run:  python -m unittest discover -s tests -v"""
import hmac
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.update(EMBEDDINGS_PROVIDER="hash", CHROMA_PATH=tempfile.mkdtemp(), POLL_INTERVAL_SECONDS="0",
                  GITHUB_WEBHOOK_SECRET="topsecret", DEMO_SEED="false", E2B_API_KEY="")

from langchain_core.messages import AIMessage  # noqa: E402

from app import create_app  # noqa: E402
from app.agents.autofix import AutofixAgent  # noqa: E402
from app.agents.chat import ChatAgent  # noqa: E402
from app.agents.review import ReviewAgent  # noqa: E402
from app.db import now  # noqa: E402

SOURCE = "def login(user, pw):\n    query = f\"SELECT * FROM users WHERE name='{user}'\"\n    return db.run(query)\n"
PATCH = "@@ -1,3 +1,3 @@\n def login(user, pw):\n+    query = f\"SELECT * FROM users WHERE name='{user}'\"\n-    query = 'x'\n     return db.run(query)"


class StubProgress:
    def step(self, text):
        pass


class FakeLLM:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        system = messages[0].content
        if "reviewing a pull request for a professional team" in system:
            out = {"summary": "Adds login.", "file_summaries": {"app/auth.py": "login query"}, "findings": [
                {"severity": "critical", "category": "Security", "title": "SQL injection in login", "body": "User input is interpolated into SQL.",
                 "impact": "An attacker can bypass authentication.", "confidence": 0.95, "path": "app/auth.py", "line": 2, "end_line": 2,
                 "suggestion": "    query = db.prepare('SELECT * FROM users WHERE name=?', user)"},
                {"severity": "nitpick", "title": "Rename var", "body": "meh", "confidence": 0.9, "path": "app/auth.py", "line": 1},
                {"severity": "major", "title": "Speculative race", "body": "maybe", "confidence": 0.3, "path": "app/auth.py", "line": 3}]}
        elif "verification stage" in system:
            out = {"items": [{"id": 0, "keep": True, "reason": "real"}]}
        elif "Write one sentence" in system:
            out = {"reason": "Review first: injection risk in auth."}
        elif "explain pull requests" in system:
            out = {"summary": "s", "intent": "i", "risk": "high", "layers": [{"title": "Add login", "description": "d", "files": ["app/auth.py"]}],
                   "blast_radius": [], "architecture": []}
        elif "verify an automated fix" in system:
            out = {"resolved": True, "reason": "Query is parameterised now."}
        elif "pull request conversation" in system:
            out = {"reply": "Handlers map errors.", "learning": "Handlers own HTTP error mapping.", "learning_path": "app/"}
        else:
            out = {}
        return AIMessage(content=json.dumps(out))


class FakeGitHub:
    def __init__(self):
        self.reviews, self.replies, self.comments, self.commits, self.statuses = [], [], [], [], []

    def pr(self, repo, n):
        return {"number": n, "title": "Add login", "body": "Adds a login function for users.", "state": "open", "draft": False,
                "user": {"login": "dev", "type": "User", "avatar_url": ""}, "head": {"ref": "feat", "sha": "abc1234", "repo": {"full_name": repo}},
                "base": {"ref": "main"}, "labels": [], "requested_reviewers": [], "html_url": "u", "additions": 2, "deletions": 1, "changed_files": 1,
                "created_at": "2026-11-10T00:00:00Z", "updated_at": "2026-11-10T00:00:00Z", "mergeable_state": "clean"}

    def pr_files(self, repo, n, limit=300):
        return [{"filename": "app/auth.py", "status": "modified", "additions": 2, "deletions": 1, "changes": 3, "patch": PATCH}]

    def tree(self, repo, sha):
        return [{"type": "blob", "path": "app/auth.py", "sha": "b1", "size": 100}]

    def blob_text(self, repo, sha):
        return SOURCE

    def file(self, repo, path, ref):
        return SOURCE, "b1"

    def path_authors(self, *a, **k):
        return ["alice", "dev", "alice"]

    def create_review(self, repo, n, sha, body, comments):
        self.reviews.append((body, comments))
        self.comments = [{"id": 900 + i, "body": c["body"]} for i, c in enumerate(comments)]

    def review_comments(self, repo, n):
        return self.comments

    def issue_comment(self, repo, n, body):
        self.replies.append(body)

    def reply_to_review_comment(self, repo, n, cid, body):
        self.replies.append(body)

    def set_status(self, repo, sha, state, description, context="warren/pre-merge", url=""):
        self.statuses.append((state, description))

    def commit_files(self, repo, branch, files, message):
        self.commits.append((branch, files, message))
        return "9f2c4a1deadbeef"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.c = self.app.container
        self.gh = FakeGitHub()
        uid = self.c.db.insert("users", {"github_id": 1, "login": "me", "name": "Me", "avatar_url": "", "token": "t", "created_at": now()})
        self.repo_id = self.c.db.insert("repos", {"user_id": uid, "full_name": "acme/app", "owner": "acme", "name": "app", "default_branch": "main",
                                                  "enabled": 1, "created_at": now()})
        self.pr_id = self.c.db.insert("pull_requests", {"repo_id": self.repo_id, "number": 7, "title": "Add login", "author": "dev", "state": "open",
                                                        "head_repo": "acme/app", "head_ref": "feat", "head_sha": "abc1234", "base_ref": "main"})
        self.patches = [mock.patch("app.services.llm.chat_model", lambda *a, **k: FakeLLM()),
                        mock.patch.object(type(self.c), "user_gh", lambda s, u: self.gh),
                        mock.patch.object(type(self.c), "bot_gh", lambda s, u: self.gh)]
        for p in self.patches:
            p.start()
        self.progress = StubProgress()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_review_fix_chat(self):
        db = self.c.db
        ReviewAgent(self.c).run(self.pr_id, self.progress)
        pr = db.get("pull_requests", self.pr_id)
        self.assertEqual(pr["review_status"], "done")
        findings = db.query("SELECT * FROM findings WHERE pr_id=?", (self.pr_id,))
        self.assertEqual(len(findings), 1, "nitpicks (profile) and low-confidence findings are filtered out")
        self.assertEqual(findings[0]["comment_id"], 900)
        body, comments = self.gh.reviews[0]
        self.assertTrue(comments[0]["body"].startswith("**Critical** · Security"), "professional header, no emoji")
        self.assertNotRegex(body + comments[0]["body"], r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "bot output carries no emoji")
        self.assertIn("**Assessment: Changes requested.**", body)
        self.assertIn("**Impact.**", comments[0]["body"])
        self.assertEqual(self.gh.statuses[0][0], "pending")
        self.assertEqual(self.gh.statuses[-1][0], "failure")
        self.assertIn("1 critical", self.gh.statuses[-1][1])
        self.assertIn("```suggestion", comments[0]["body"])
        self.assertEqual(comments[0]["line"], 2)
        self.assertEqual(pr["triage"]["priority"], "P0")
        self.assertEqual(pr["triage"]["reviewers"], ["alice"])
        self.assertEqual(pr["change_stack"]["layers"][0]["files"][0]["path"], "app/auth.py")
        self.assertTrue(any(e["kind"] == "finding" for e in db.query("SELECT * FROM events WHERE pr_id=?", (self.pr_id,))))

        AutofixAgent(self.c).run(self.pr_id, None, self.progress)
        branch, files, message = self.gh.commits[0]
        self.assertEqual(branch, "feat")
        self.assertIn("db.prepare", files["app/auth.py"])
        self.assertEqual(db.get("findings", findings[0]["id"])["status"], "verified")
        self.assertTrue(any("Fixed in `9f2c4a1`" in r for r in self.gh.replies))
        self.assertEqual(db.get("pull_requests", self.pr_id)["reviewed_sha"], "9f2c4a1deadbeef")

        ChatAgent(self.c).respond(self.pr_id, "@warren why do handlers map errors?", "dev")
        self.assertEqual(len(db.query("SELECT * FROM learnings")), 1)
        self.assertIn("Learning recorded", self.gh.replies[-1])
        self.assertTrue(self.c.vectors.search_learnings(self.repo_id, "http error mapping"))

    def test_autofix_on_a_fork_explains_in_the_pr_instead_of_failing_silently(self):
        from app.agents.autofix import AutofixAgent
        ReviewAgent(self.c).run(self.pr_id, self.progress)
        self.c.db.update("pull_requests", self.pr_id, {"head_repo": "contrib/app", "can_modify": 0})
        AutofixAgent(self.c).run(self.pr_id, None, self.progress)
        notice = next(r for r in self.gh.replies if "comes from a fork" in r)
        self.assertIn("1 finding(s) include a committable suggestion", notice)
        self.assertIn("Commit suggestion", notice)
        self.assertEqual(self.gh.commits, [])

    def test_webhook_signature_and_loop_guard(self):
        import dataclasses

        from app.config import settings
        patcher = mock.patch("app.routes.webhooks.settings", dataclasses.replace(settings, github_webhook_secret="topsecret"))
        patcher.start()
        self.addCleanup(patcher.stop)
        client = self.app.test_client()
        raw = json.dumps({"zen": "x"}).encode()
        sig = "sha256=" + hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
        self.assertEqual(client.post("/api/webhooks/github", data=raw, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig}).status_code, 200)
        self.assertEqual(client.post("/api/webhooks/github", data=raw, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "bad"}).status_code, 401)
        payload = {"action": "created", "repository": {"full_name": "acme/app"}, "sender": {"login": "dev"}, "issue": {"number": 7, "pull_request": {}},
                   "comment": {"id": 1, "body": "<!-- warren:summary --> reply `@warren fix`"}}
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
        r = client.post("/api/webhooks/github", data=raw, headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sig})
        self.assertEqual(r.get_json()["ignored"], "bot comment")


if __name__ == "__main__":
    unittest.main()
