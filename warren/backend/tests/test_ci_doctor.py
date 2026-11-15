"""CI Doctor end-to-end with fake GitHub / sandbox / LLM: failed run -> reproduce -> fix -> validate -> approval -> commit.
Run:  python -m unittest discover -s tests -v"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.update(EMBEDDINGS_PROVIDER="hash", CHROMA_PATH=tempfile.mkdtemp(), POLL_INTERVAL_SECONDS="0", DEMO_SEED="false",
                  CHECKPOINT_PATH=os.path.join(tempfile.mkdtemp(), "cp.db"), GITHUB_WEBHOOK_SECRET="")

from langchain_core.messages import AIMessage  # noqa: E402

from app import create_app  # noqa: E402
from app.agents.ci_doctor import CiDoctor  # noqa: E402
from app.db import now  # noqa: E402
from app.services.github import GitHubError  # noqa: E402
from app.services.sandbox import REPO_DIR, ExecResult  # noqa: E402

BUGGY = "def total(items):\n    return sum(i.price for i in items) + 1\n"
LOG = ("2026-11-10T10:00:00.1Z ##[group]Run pytest -q\n" + "\n".join(f"collecting {i}" for i in range(60)) +
       "\nE   AssertionError: assert 11 == 10\nFAILED tests/test_total.py::test_total\n##[error]Process completed with exit code 1.\n")
WORKFLOW_BAD = ("jobs:\n  test:\n    name: Tests\n    defaults:\n      run:\n        working-directory: backend-service\n    steps:\n      - uses: actions/checkout@v4\n"
               "      - run: pip install -r requirements.txt\n")
WRONG_DIR_LOG = (
    "##[group]Run pip install -r requirements.txt\n##[error]An error occurred trying to start process '/usr/bin/bash' with working directory "
    "'/home/runner/work/app/app/backend-service'. No such file or directory\n")
WORKFLOW = "jobs:\n  test:\n    name: Tests\n    steps:\n      - uses: actions/checkout@v4\n      - run: pytest -q\n"


class Progress:
    def step(self, text):
        pass


class FakeSandbox:
    """Pretends to be E2B: the test command passes only once app/cart.py has been fixed."""
    fixed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def clone_pr(self, *a):
        return ExecResult("", "", 0)

    clone_ref = clone_pr

    def write(self, path, content):
        if path.endswith("app/cart.py"):
            FakeSandbox.fixed = "+ 1" not in content

    def run(self, cmd, cwd=None, timeout=60, envs=None):
        if cmd.startswith("pytest"):
            return ExecResult("1 passed", "", 0) if FakeSandbox.fixed else ExecResult("E   AssertionError: assert 11 == 10", "", 1)
        return ExecResult("", "", 0)

    def repo_run(self, cmd, timeout=60):
        return ExecResult(BUGGY, "", 0) if cmd.startswith("cat") else self.run(cmd)


class WorkflowSandbox(FakeSandbox):
    """A repository whose only top-level folder is `backend`, so `working-directory: backend-service` really fails."""
    folders = {"backend"}
    workflow_text = WORKFLOW_BAD

    def _exists(self, path):
        rel = (path or REPO_DIR).replace(REPO_DIR, "").strip("/")
        return not rel or rel.split("/")[0] in self.folders

    def run(self, cmd, cwd=None, timeout=60, envs=None):
        if cmd.startswith("test -d "):
            return ExecResult("", "", 0 if self._exists(cmd.split(" ", 2)[2].strip("'")) else 1)
        if not self._exists(cwd):
            return ExecResult("", f"sandbox error: cwd '{cwd}' does not exist", 124)
        return ExecResult("ok", "", 0)

    def repo_run(self, cmd, timeout=60):
        return ExecResult(self.workflow_text, "", 0) if cmd.startswith("cat") else self.run(cmd, REPO_DIR)

    def write(self, path, content):
        pass


class FakeGitHub:
    def __init__(self):
        self.commits, self.comments, self.reruns, self.prs, self.reviews, self.commit_repos, self.pr_files_data = [], [], [], [], [], [], []
        self.runs_by_branch, self.sha_runs, self.pr_states, self.main_log, self.heads = {}, [], {}, "", {}

    def workflow_run(self, repo, run_id):
        return {"id": run_id, "path": ".github/workflows/ci.yml", "workflow_id": 5}

    step_name = "Run pytest -q"
    workflow_text = None
    commit_error = None

    def pr_files(self, repo, number, limit=300):
        return self.pr_files_data

    def create_review(self, repo, number, sha, body, comments):
        self.reviews.append((body, comments))

    def run_jobs(self, repo, run_id):
        return [{"id": 6 if run_id == 777 else 5, "name": "Tests", "conclusion": "failure", "steps": [{"name": self.step_name, "conclusion": "failure"}]}]

    def job_logs(self, repo, job_id):
        return self.main_log if job_id == 6 else LOG

    def file(self, repo, path, ref):
        return ((self.workflow_text or WORKFLOW) if path.endswith("ci.yml") else BUGGY), "sha"

    def commit_files(self, repo, branch, files, message):
        if self.commit_error:
            raise self.commit_error
        self.commits.append((branch, files, message))
        self.commit_repos.append(repo)
        self.heads[branch] = "c0ffee1234567"
        return "c0ffee1234567"

    def issue_comment(self, repo, n, body):
        self.comments.append(body)

    def rerun_failed(self, repo, run_id):
        self.reruns.append(run_id)

    def create_branch(self, *a):
        pass

    def branch_head(self, repo, branch):
        return self.heads.get(branch, "abc")

    def workflow_runs(self, repo, workflow_id, branch, limit=3):
        return self.runs_by_branch.get(branch, [])

    def pr(self, repo, number):
        return {"number": number, "title": "t", "body": "", "state": self.pr_states.get(number, "open"), "draft": False, "merged_at": "x" if self.pr_states.get(number) == "merged" else None,
                "user": {"login": "dev", "type": "User"}, "head": {"ref": "feat", "sha": "abc", "repo": {"full_name": repo}}, "base": {"ref": "main"},
                "labels": [], "requested_reviewers": [], "html_url": "u", "created_at": "2026-11-10T00:00:00Z", "updated_at": "2026-11-10T00:00:00Z"}

    def pulls_for_branch(self, *a):
        return []

    def runs_for_sha(self, repo, sha):
        return self.sha_runs


    def open_pr(self, repo, title, body, head, base):
        self.prs.append((title, head, base))
        return {"html_url": "https://github.com/acme/app/pull/99"}


class FakeLLM:
    confidence = 0.9
    ci_edit = None

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        system = messages[0].content
        if "CI Doctor, an autonomous engineer" in system and self.ci_edit:
            out = self.ci_edit
        elif "CI Doctor, an autonomous engineer" in system:
            out = {"root_cause": "total() adds a stray +1.", "solution": "Remove the stray +1.", "confidence": FakeLLM.confidence,
                   "edits": [{"path": "app/cart.py", "old": "+ 1\n", "new": "\n"}]}
        else:
            out = {"root_cause": "r", "solution": "s", "confidence": 0.3, "needs_human": True, "human_steps": "1. do x"}
        return AIMessage(content=json.dumps(out))


class CiDoctorTest(unittest.TestCase):
    def setUp(self):
        FakeSandbox.fixed = False
        FakeLLM.confidence = 0.9
        FakeLLM.ci_edit = None
        self.app = create_app()
        self.c = self.app.container
        self.gh = FakeGitHub()
        uid = self.c.db.insert("users", {"github_id": 1, "login": "me", "name": "Me", "avatar_url": "", "token": "t", "created_at": now()})
        self.repo = self.c.db.get("repos", self.c.db.insert("repos", {"user_id": uid, "full_name": "acme/app", "owner": "acme", "name": "app",
                                                                    "default_branch": "main", "enabled": 1, "created_at": now()}))
        self.pr_id = self.c.db.insert("pull_requests", {"repo_id": self.repo["id"], "number": 7, "title": "Cart", "author": "dev", "state": "open",
                                                        "head_repo": "acme/app", "head_ref": "feat", "head_sha": "abc", "base_ref": "main"})
        for p in (mock.patch("app.services.llm.chat_model", lambda *a, **k: FakeLLM()),
                  mock.patch.object(type(self.c), "user_gh", lambda s, u: self.gh), mock.patch.object(type(self.c), "bot_gh", lambda s, u: self.gh)):
            p.start()
            self.addCleanup(p.stop)
        self.c.ci = CiDoctor(self.c, sandbox_factory=FakeSandbox)
        self.run_payload = {"id": 321, "name": "CI", "path": ".github/workflows/ci.yml", "head_branch": "feat", "head_sha": "abc",
                            "html_url": "https://github.com/acme/app/actions/runs/321", "run_attempt": 1, "pull_requests": [{"number": 7}]}

    def test_fix_needs_no_approval_when_verified_and_safe(self):
        iid, new = self.c.ci.open_incident(self.repo, self.run_payload)
        self.assertTrue(new)
        self.assertEqual(self.c.ci.open_incident(self.repo, self.run_payload), (iid, False), "duplicate webhook must not start a second run")
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual((inc["status"], inc["category"], inc["pr_id"]), ("fixed", "test_failure", self.pr_id))
        branch, files, message = self.gh.commits[0]
        self.assertEqual(branch, "feat")
        self.assertNotIn("+ 1", files["app/cart.py"])
        self.assertTrue(any("Why it broke" in c for c in self.gh.comments))
        self.assertEqual(self.c.db.get("pull_requests", self.pr_id)["reviewed_sha"], "c0ffee1234567", "loop guard: our own commit is marked reviewed")

    def test_low_confidence_pauses_for_approval_then_applies(self):
        FakeLLM.confidence = 0.4
        iid, _ = self.c.ci.open_incident(self.repo, self.run_payload)
        self.c.ci.run(iid, Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "awaiting_approval")
        self.assertEqual(self.gh.commits, [], "nothing may be pushed before approval")
        self.assertIn("low model confidence", self.c.db.get("incidents", iid)["meta"]["approval_reasons"][0])
        self.assertIn("app/cart.py", self.c.db.get("incidents", iid)["meta"]["diffs"])
        self.c.ci.resume(iid, True, "", Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "fixed")
        self.assertEqual(len(self.gh.commits), 1)

    def test_reject_dismisses_and_never_commits(self):
        FakeLLM.confidence = 0.4
        iid, _ = self.c.ci.open_incident(self.repo, self.run_payload)
        self.c.ci.run(iid, Progress())
        self.c.ci.resume(iid, False, "", Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "dismissed")
        self.assertEqual(self.gh.commits, [])

    def test_flaky_failure_is_retried_once_then_escalated(self):
        self.gh.job_logs = lambda repo, job: "##[error]Error: read ECONNRESET\n##[error]Process completed with exit code 1."
        iid, _ = self.c.ci.open_incident(self.repo, self.run_payload)
        self.c.ci.run(iid, Progress())
        self.assertEqual((self.c.db.get("incidents", iid)["status"], self.gh.reruns), ("retrying", [321]))
        again, start = self.c.ci.open_incident(self.repo, {**self.run_payload, "run_attempt": 2})
        self.assertEqual((again, start), (iid, True))
        self.c.ci.run(iid, Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "escalated")
        self.assertEqual(self.gh.reruns, [321], "must not retry twice")

    def test_permissions_failure_gets_human_steps_not_a_commit(self):
        self.gh.job_logs = lambda repo, job: "##[error]Resource not accessible by integration"
        iid, _ = self.c.ci.open_incident(self.repo, self.run_payload)
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual((inc["status"], inc["category"]), ("escalated", "permissions_config"))
        self.assertIn("do x", inc["solution"])
        self.assertEqual(self.gh.commits, [])

    def test_main_branch_failure_opens_a_pr_instead_of_pushing(self):
        run = {**self.run_payload, "head_branch": "main", "pull_requests": []}
        iid, _ = self.c.ci.open_incident(self.repo, run)
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual(inc["status"], "fixed")
        self.assertTrue(inc["fix_pr_url"].endswith("/pull/99"))
        self.assertEqual(self.gh.commits[0][0], "warren/ci-fix-321", "never commits straight to the default branch")

    def test_webhook_and_api_flow(self):
        import dataclasses
        import hashlib
        import hmac

        from app.config import settings
        patcher = mock.patch("app.routes.webhooks.settings", dataclasses.replace(settings, github_webhook_secret="s3cret"))
        patcher.start()
        self.addCleanup(patcher.stop)
        client, counter = self.app.test_client(), iter(range(1000))

        def post(payload):
            raw = json.dumps(payload).encode()
            sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
            return client.post("/api/webhooks/github", data=raw, headers={"X-GitHub-Event": "workflow_run", "X-Hub-Signature-256": sig,
                                                                         "X-GitHub-Delivery": f"d{next(counter)}{id(self)}", "Content-Type": "application/json"})

        failing = {"action": "completed", "repository": {"full_name": "acme/app"}, "workflow_run": {**self.run_payload, "conclusion": "failure"}}
        with mock.patch.object(type(self.c.ci), "start") as start:
            self.assertEqual(post(failing).status_code, 200)
            start.assert_called_once()
        self.assertEqual(client.post("/api/webhooks/github", data=b"{}", headers={"X-GitHub-Event": "workflow_run"}).status_code, 401, "unsigned deliveries are rejected")
        green = {**failing, "workflow_run": {**failing["workflow_run"], "conclusion": "success"}}
        self.assertEqual(post(green).get_json()["resolved"], 1, "a green run closes the open incident")
        cancelled = {**failing, "workflow_run": {**failing["workflow_run"], "id": 999, "conclusion": "cancelled"}}
        self.assertEqual(post(cancelled).get_json()["ignored"], "run did not fail")

    # ---------------------------------------------------------------- loop / duplicate protection
    def _run(self, run_id, **over):
        payload = {**self.run_payload, "id": run_id, **over}
        iid, _ = self.c.ci.open_incident(self.repo, payload)
        self.c.ci.run(iid, Progress())
        return self.c.db.get("incidents", iid)

    def test_two_workflows_failing_on_one_commit_are_one_incident(self):
        a, start_a, _ = self.c.ci.ingest(self.repo, {**self.run_payload, "id": 1001, "name": "TaskFlow CI", "conclusion": "failure", "workflow_id": 5})
        b, start_b, why = self.c.ci.ingest(self.repo, {**self.run_payload, "id": 1002, "name": "Backend tests", "conclusion": "failure", "workflow_id": 6})
        self.assertEqual((start_a, b, start_b), (True, None, False))
        self.assertIn(f"incident #{a}", why)
        self.assertEqual(self.c.db.get("incidents", a)["meta"]["also"], ["Backend tests"], "the other workflow is noted on the one incident")
        self.c.ci.run(a, Progress())
        self.assertEqual(len(self.gh.commits), 1)
        self.assertEqual(self.c.db.one("SELECT COUNT(*) n FROM incidents")["n"], 1, "no second row, hidden or otherwise")

    def test_fix_that_did_not_hold_stops_instead_of_pushing_again(self):
        first = self._run(2001)
        self.assertEqual(first["fix_commit"], "c0ffee1234567")
        again = self._run(2002, head_sha="c0ffee1234567")                # CI re-ran on OUR commit and failed the same way
        self.assertEqual(again["status"], "escalated")
        self.assertEqual(len(self.gh.commits), 1, "no second commit on top of a fix that did not work")
        self.assertTrue(any("same failure happened again" in c for c in self.gh.comments))

    def test_failure_already_on_main_is_not_fixed_on_the_branch(self):
        self.gh.runs_by_branch["main"] = [{"id": 777, "name": "CI", "conclusion": "failure", "path": ".github/workflows/ci.yml", "head_branch": "main", "head_sha": "m1",
                              "html_url": "u", "run_attempt": 1, "pull_requests": []}]
        self.gh.main_log = LOG
        self.gh.heads["main"] = "m1"
        with mock.patch.object(type(self.c.ci), "start"):                 # don't run main's investigation inline
            branch_inc = self._run(3001, workflow_id=5)
        self.assertEqual((branch_inc["status"], branch_inc["meta"]["reason"]), ("merged", "inherited"))
        main_inc = self.c.db.get("incidents", branch_inc["parent_id"])
        self.assertEqual((main_inc["branch"], main_inc["run_id"]), ("main", 777))
        self.assertEqual(main_inc["meta"]["blocked_prs"], [7], "the PR is recorded on the main incident, not as a separate incident")
        self.assertEqual(self.gh.commits, [], "the PR branch must stay untouched")
        self.assertTrue(any("not because of this pull request" in c for c in self.gh.comments))

    def test_branches_wardrenfix_branches_are_never_auto_fixed(self):
        for branch in ("warren/ci-fix-55", "warren-ci-fix-55", "warren/revert-abc1234"):
            inc = self._run(4000 + len(branch), head_branch=branch, pull_requests=[])
            self.assertEqual(inc["status"], "escalated", branch)
        self.assertEqual(self.gh.commits, [])

    def test_pr_gets_at_most_two_ci_fixes(self):
        self.c.db.insert("incidents", {"repo_id": self.repo["id"], "pr_id": self.pr_id, "run_id": 1, "status": "fixed", "fix_commit": "a", "created_at": now(), "updated_at": now()})
        self.c.db.insert("incidents", {"repo_id": self.repo["id"], "pr_id": self.pr_id, "run_id": 2, "status": "fixed", "fix_commit": "b", "created_at": now(), "updated_at": now()})
        inc = self._run(5001)
        self.assertEqual(inc["status"], "escalated")
        self.assertEqual(self.gh.commits, [])

    def test_green_run_on_the_fix_commit_resolves_the_incident(self):
        inc = self._run(6001)
        self.assertEqual(inc["status"], "fixed")
        self.assertEqual(self.c.ci.confirm_green(self.repo, {"name": "CI", "head_sha": "other", "head_branch": "feat"}), 0)
        self.assertEqual(self.c.ci.confirm_green(self.repo, {"name": "CI", "head_sha": "c0ffee1234567", "head_branch": "feat"}), 1)
        self.assertEqual(self.c.db.get("incidents", inc["id"])["status"], "resolved")
        self.gh.sha_runs = []   # poller path: nothing completed yet -> stays as is
        self.assertEqual(self.c.ci.confirm_pending(self.repo), 0)

    def test_pr_is_looked_up_when_the_webhook_beats_the_sync(self):
        self.c.db.execute("DELETE FROM pull_requests")
        iid, _ = self.c.ci.open_incident(self.repo, {**self.run_payload, "id": 8001})
        inc = self.c.db.get("incidents", iid)
        self.assertIsNotNone(inc["pr_id"], "fix must target the PR branch, not open a PR into it")

    # ---------------------------------------------------------------- only a CURRENT break is work
    def _failing(self, run_id=9100, **over):
        return {**self.run_payload, "id": run_id, "conclusion": "failure", "workflow_id": 5, **over}

    def test_stale_run_for_an_old_commit_is_ignored(self):
        self.gh.heads["feat"] = "newer-commit"
        iid, start, why = self.c.ci.ingest(self.repo, self._failing())
        self.assertEqual((iid, start, why), (None, False, "superseded by a newer commit"))
        self.assertEqual(self.c.db.one("SELECT COUNT(*) n FROM incidents")["n"], 0)

    def test_history_before_watching_started_is_ignored(self):
        self.c.db.update("repos", self.repo["id"], {"watch_since": "2026-11-14T10:00:00+00:00"})
        repo = self.c.db.get("repos", self.repo["id"])
        old = self.c.ci.ingest(repo, self._failing(created_at="2026-11-13T09:00:00Z"))
        self.assertEqual(old[0], None)
        fresh = self.c.ci.ingest(repo, self._failing(9101, created_at="2026-11-14T11:00:00Z"))
        self.assertTrue(fresh[1])

    def test_cancelled_and_superseded_workflow_runs_are_ignored(self):
        self.assertEqual(self.c.ci.ingest(self.repo, self._failing(conclusion="cancelled"))[2], "the run did not fail")
        self.gh.runs_by_branch["feat"] = [{"id": 9999, "conclusion": "success"}]
        self.assertIn("newer run", self.c.ci.ingest(self.repo, self._failing())[2])

    def test_a_new_failing_commit_supersedes_the_old_incident(self):
        first, _, _ = self.c.ci.ingest(self.repo, self._failing(9200))
        self.gh.heads["feat"] = "def"
        second, start, _ = self.c.ci.ingest(self.repo, self._failing(9201, head_sha="def"))
        self.assertTrue(start)
        self.assertEqual(self.c.db.get("incidents", first)["status"], "superseded")

    def test_green_pipeline_closes_every_open_incident_and_nothing_retriggers(self):
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9300))
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "detected")
        self.assertEqual(self.c.ci.confirm_green(self.repo, {"name": "CI", "head_branch": "feat", "head_sha": "abc"}), 1)
        inc = self.c.db.get("incidents", iid)
        self.assertEqual((inc["status"], inc["meta"]["ci_green"]), ("resolved", True))
        # the same old failure event arrives again (redelivery / poller): nothing happens
        self.gh.runs_by_branch["feat"] = [{"id": 9301, "conclusion": "success"}]
        self.assertEqual(self.c.ci.ingest(self.repo, self._failing(9300))[1], False)
        self.assertEqual(self.c.db.one("SELECT COUNT(*) n FROM incidents")["n"], 1)

    def test_green_after_a_rerun_marks_the_incident_flaky(self):
        self.gh.job_logs = lambda repo, job: "##[error]Error: read ECONNRESET"
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9400))
        self.c.ci.run(iid, Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "retrying")
        self.c.ci.confirm_green(self.repo, {"name": "CI", "head_branch": "feat", "head_sha": "abc"})
        self.assertTrue(self.c.db.get("incidents", iid)["meta"]["flaky"])

    def test_branch_moving_while_waiting_for_approval_prevents_the_push(self):
        FakeLLM.confidence = 0.4
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9500))
        self.c.ci.run(iid, Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "awaiting_approval")
        self.gh.heads["feat"] = "someone-pushed"
        self.c.ci.resume(iid, True, "", Progress())
        self.assertEqual((self.c.db.get("incidents", iid)["status"], self.gh.commits), ("superseded", []))

    def test_deployment_failures_are_never_auto_rerun_and_always_need_approval(self):
        self.gh.job_logs = lambda repo, job: "##[error]Error: read ECONNRESET"
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9600, name="Deploy to production"))
        self.c.ci.run(iid, Progress())
        self.assertEqual((self.c.db.get("incidents", iid)["status"], self.gh.reruns), ("escalated", []))
        self.gh.job_logs = lambda repo, job: LOG
        fixable, _, _ = self.c.ci.ingest(self.repo, self._failing(9601, name="Release deploy", head_sha="abc", workflow_id=7))
        self.assertIsNone(fixable) if fixable is None else self.c.ci.run(fixable, Progress())
        self.assertEqual(self.gh.commits, [], "nothing is pushed for a deployment pipeline without approval")

    def test_hidden_rows_never_reach_the_api_and_the_board_shows_blocked_prs(self):
        from app.auth import issue_token
        uid = self.repo["user_id"]
        self.c.db.update("repos", self.repo["id"], {"enabled": 1})
        main = self.c.db.insert("incidents", {"repo_id": self.repo["id"], "run_id": 1, "workflow": "CI", "branch": "main", "status": "escalated", "meta": {"blocked_prs": [7]},
                                              "created_at": now(), "updated_at": now()})
        self.c.db.insert("incidents", {"repo_id": self.repo["id"], "run_id": 2, "workflow": "CI", "branch": "feat", "status": "merged", "parent_id": main,
                                       "created_at": now(), "updated_at": now()})
        client, headers = self.app.test_client(), {"Authorization": f"Bearer {issue_token(uid)}"}
        listed = client.get("/api/incidents", headers=headers).get_json()
        self.assertEqual([i["id"] for i in listed["incidents"]], [main], "merged rows are bookkeeping, not incidents")
        self.c.db.update("pull_requests", self.pr_id, {"triage": {"priority": "P1", "tags": []}})
        cards = [c for col in client.get("/api/prs", headers=headers).get_json()["columns"].values() for c in col["items"]]
        self.assertEqual(cards[0]["ci"], "inherited")

    # ---------------------------------------------------------------- workflow-file fixes (the `backend-service` case)
    def _wrong_directory_setup(self, new_dir="backend"):
        self.gh.job_logs = lambda repo, job: WRONG_DIR_LOG
        self.gh.workflow_text, self.gh.step_name = WORKFLOW_BAD, "Run pip install -r requirements.txt"
        FakeLLM.ci_edit = {"root_cause": "The step runs in `backend-service`, which does not exist.", "solution": f"Point working-directory at `{new_dir}`.", "confidence": 0.95,
                           "edits": [{"path": ".github/workflows/ci.yml", "old": "working-directory: backend-service", "new": f"working-directory: {new_dir}"}]}
        self.c.ci = CiDoctor(self.c, sandbox_factory=WorkflowSandbox)

    def test_workflow_fix_is_validated_against_the_patched_workflow_then_approved(self):
        self._wrong_directory_setup()
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9700))
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual(inc["category"], "workflow_config", "the error text is recognised, not 'unknown'")
        self.assertEqual(inc["status"], "awaiting_approval", "workflow files are protected, so a human approves")
        diff = inc["meta"]["diffs"][".github/workflows/ci.yml"]
        self.assertIn("-        working-directory: backend-service", diff)
        self.assertIn("+        working-directory: backend", diff)
        self.assertIn("pip install", inc["meta"]["validation"], "the step was replayed with the corrected directory")
        self.assertEqual(self.gh.commits, [])
        self.c.ci.resume(iid, True, "", Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "fixed")
        self.assertIn("working-directory: backend\n", self.gh.commits[0][1][".github/workflows/ci.yml"])

    def test_token_without_workflow_scope_gets_the_exact_patch_in_a_comment(self):
        self._wrong_directory_setup()
        self.gh.commit_error = GitHubError(404, "refusing to allow an OAuth App to create or update workflow `.github/workflows/ci.yml` without `workflow` scope")
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9701))
        self.c.ci.run(iid, Progress())
        self.c.ci.resume(iid, True, "", Progress())
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "proposed")
        comment = next(c for c in self.gh.comments if "CI fix proposed" in c)
        self.assertIn("```diff", comment)
        self.assertIn("+        working-directory: backend", comment)
        self.assertIn("`workflow` scope", comment)

    def test_a_fix_that_cannot_validate_is_still_shown_as_a_diff_not_discarded(self):
        self._wrong_directory_setup(new_dir="backend-typo")
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9702))
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual(inc["status"], "escalated")
        self.assertEqual(self.gh.commits, [])
        self.assertIn("+        working-directory: backend-typo", inc["meta"]["diffs"][".github/workflows/ci.yml"])
        self.assertFalse(inc["meta"]["proposal_verified"])
        report = next(c for c in self.gh.comments if "Proposed change (not verified)" in c)
        self.assertIn("```diff", report)
        events = [e["body"] for e in self.c.db.query("SELECT body FROM events WHERE incident_id=?", (iid,))]
        self.assertTrue(any("identical to the previous proposal" in e for e in events), "stops retrying when an attempt repeats itself")

    def test_rerunning_the_analysis_marks_where_it_starts(self):
        iid, _, _ = self.c.ci.ingest(self.repo, self._failing(9703))
        self.c.ci.run(iid, Progress())
        self.c.ci.run(iid, Progress(), "keep the code, change the test")
        bodies = [e["body"] for e in self.c.db.query("SELECT body FROM events WHERE incident_id=?", (iid,))]
        self.assertTrue(any(b.startswith("Re-running the analysis with this guidance") for b in bodies))

    # ---------------------------------------------------------------- the bot speaks in the PR conversation
    CART_PATCH = "@@ -1,2 +1,2 @@\n def total(items):\n-    return sum(i.price for i in items)\n+    return sum(i.price for i in items) + 1"

    def _fork(self, can_modify=0):
        self.c.db.update("pull_requests", self.pr_id, {"head_repo": "contrib/app", "can_modify": can_modify})
        return self._failing(head_repository={"full_name": "contrib/app", "owner": {"login": "contrib"}})

    def test_fork_pr_gets_one_click_suggested_changes_in_the_conversation(self):
        run = self._fork()
        self.gh.pr_files_data = [{"filename": "app/cart.py", "patch": self.CART_PATCH}]
        iid, _, _ = self.c.ci.ingest(self.repo, run)
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual(inc["status"], "proposed", "no approval needed: nothing is pushed")
        self.assertEqual((self.gh.commits, inc["meta"]["proposal_posted"]), ([], True))
        body, comments = self.gh.reviews[0]
        self.assertIn("comes from a fork", body)
        self.assertIn("Why it broke", body)
        self.assertEqual(comments[0]["line"], 2)
        self.assertIn("```suggestion", comments[0]["body"])
        self.assertIn("return sum(i.price for i in items)", comments[0]["body"])
        self.assertNotIn("+ 1", comments[0]["body"])

    def test_fork_pr_change_outside_the_diff_is_posted_as_a_diff_comment(self):
        run = self._fork()
        self.gh.pr_files_data = []
        iid, _, _ = self.c.ci.ingest(self.repo, run)
        self.c.ci.run(iid, Progress())
        comment = next(c for c in self.gh.comments if "CI fix proposed" in c)
        self.assertIn("```diff", comment)
        self.assertIn("comes from a fork", comment)
        self.assertEqual((self.gh.reviews, self.gh.commits), ([], []))

    def test_fork_with_maintainer_edits_asks_then_pushes_to_the_fork(self):
        run = self._fork(can_modify=1)
        iid, _, _ = self.c.ci.ingest(self.repo, run)
        self.c.ci.run(iid, Progress())
        inc = self.c.db.get("incidents", iid)
        self.assertEqual(inc["status"], "awaiting_approval")
        self.assertIn("contributor's fork", inc["meta"]["approval_reasons"][0])
        self.c.ci.resume(iid, True, "", Progress())
        self.assertEqual((self.gh.commit_repos, self.gh.commits[0][0]), (["contrib/app"], "feat"))
        self.assertEqual(self.c.db.get("incidents", iid)["status"], "fixed")

    def test_line_hunks_match_what_github_suggestions_need(self):
        hunks = CiDoctor._line_hunks
        self.assertEqual(hunks("a\nb\nc", "a\nB\nc"), [(2, 2, ["B"])])
        self.assertEqual(hunks("a\nb\nc", "a\nc"), [(2, 2, [])])
        self.assertEqual(hunks("a\nb", "a\nx\nb"), [(1, 1, ["a", "x"])], "insertions are anchored on the previous line")
        self.assertEqual(hunks("a\nb", "x\na\nb"), [(1, 1, ["x", "a"])])


if __name__ == "__main__":
    unittest.main()
