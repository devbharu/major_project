"""Thin GitHub REST client + diff helpers. Everything GitHub-related lives here."""
import base64
import re

import requests

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class GitHubClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _req(self, method: str, path: str, **kwargs):
        resp = self.session.request(method, path if path.startswith("http") else f"{API}{path}", timeout=30, **kwargs)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise GitHubError(resp.status_code, message[:300])
        return resp.json() if resp.content else {}

    def _paginate(self, path: str, limit: int = 100, **params):
        items, page = [], 1
        while len(items) < limit:
            batch = self._req("GET", path, params={"per_page": 100, "page": page, **params})
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items[:limit]

    # ---- identity & repos ----
    def user(self):
        return self._req("GET", "/user")

    def list_repos(self, limit: int = 100):
        return self._paginate("/user/repos", limit, sort="pushed", affiliation="owner,collaborator,organization_member")

    def repo(self, full_name: str):
        return self._req("GET", f"/repos/{full_name}")

    # ---- pull requests ----
    def open_prs(self, full_name: str, limit: int = 30):
        return self._paginate(f"/repos/{full_name}/pulls", limit, state="open", sort="updated", direction="desc")

    def pr(self, full_name: str, number: int):
        return self._req("GET", f"/repos/{full_name}/pulls/{number}")

    def pr_files(self, full_name: str, number: int, limit: int = 300):
        return self._paginate(f"/repos/{full_name}/pulls/{number}/files", limit)

    def review_comments(self, full_name: str, number: int):
        return self._paginate(f"/repos/{full_name}/pulls/{number}/comments", 300)

    def create_review(self, full_name: str, number: int, head_sha: str, body: str, comments: list[dict]):
        payload = {"commit_id": head_sha, "body": body, "event": "COMMENT", "comments": comments}
        return self._req("POST", f"/repos/{full_name}/pulls/{number}/reviews", json=payload)

    def issue_comment(self, full_name: str, number: int, body: str):
        return self._req("POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body})

    def reply_to_review_comment(self, full_name: str, number: int, comment_id: int, body: str):
        return self._req("POST", f"/repos/{full_name}/pulls/{number}/comments/{comment_id}/replies", json={"body": body})

    # ---- repository content ----
    def file(self, full_name: str, path: str, ref: str) -> tuple[str, str]:
        data = self._req("GET", f"/repos/{full_name}/contents/{path}", params={"ref": ref})
        if isinstance(data, list) or data.get("encoding") != "base64":
            raise GitHubError(415, f"{path} is not a regular file")
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace"), data["sha"]

    def tree(self, full_name: str, sha: str):
        return self._req("GET", f"/repos/{full_name}/git/trees/{sha}", params={"recursive": "1"}).get("tree", [])

    def blob_text(self, full_name: str, sha: str) -> str:
        data = self._req("GET", f"/repos/{full_name}/git/blobs/{sha}")
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def path_authors(self, full_name: str, path: str, ref: str, limit: int = 15) -> list[str]:
        commits = self._req("GET", f"/repos/{full_name}/commits", params={"path": path, "sha": ref, "per_page": limit})
        return [c["author"]["login"] for c in commits if c.get("author")]

    def permission(self, full_name: str, login: str) -> str:
        return self._req("GET", f"/repos/{full_name}/collaborators/{login}/permission").get("permission", "none")

    def set_status(self, full_name: str, sha: str, state: str, description: str, context: str = "warren/pre-merge", url: str = ""):
        """Commit status (works with OAuth/PAT tokens, unlike check runs) so branch protection can require the pre-merge gate."""
        payload = {"state": state, "description": description[:140], "context": context}
        if url:
            payload["target_url"] = url
        return self._req("POST", f"/repos/{full_name}/statuses/{sha}", json=payload)

    # ---- CI / Actions ----
    def workflow_run(self, full_name: str, run_id: int):
        return self._req("GET", f"/repos/{full_name}/actions/runs/{run_id}")

    def workflow_runs(self, full_name: str, workflow_id: int, branch: str, limit: int = 3):
        return self._req("GET", f"/repos/{full_name}/actions/workflows/{workflow_id}/runs",
                         params={"branch": branch, "status": "completed", "per_page": limit}).get("workflow_runs", [])

    def runs_for_sha(self, full_name: str, sha: str):
        return self._req("GET", f"/repos/{full_name}/actions/runs", params={"head_sha": sha, "per_page": 20}).get("workflow_runs", [])

    def pulls_for_branch(self, full_name: str, owner: str, branch: str):
        return self._req("GET", f"/repos/{full_name}/pulls", params={"head": f"{owner}:{branch}", "state": "open"})

    def failed_runs(self, full_name: str, limit: int = 10):
        return self._req("GET", f"/repos/{full_name}/actions/runs", params={"status": "failure", "per_page": limit}).get("workflow_runs", [])

    def run_jobs(self, full_name: str, run_id: int):
        return self._req("GET", f"/repos/{full_name}/actions/runs/{run_id}/jobs", params={"filter": "latest", "per_page": 50}).get("jobs", [])

    def job_logs(self, full_name: str, job_id: int) -> str:
        resp = self.session.get(f"{API}/repos/{full_name}/actions/jobs/{job_id}/logs", timeout=60)   # redirects to the log blob
        if resp.status_code >= 400:
            raise GitHubError(resp.status_code, "could not download job logs")
        return resp.text[-600_000:]

    def rerun_failed(self, full_name: str, run_id: int):
        return self._req("POST", f"/repos/{full_name}/actions/runs/{run_id}/rerun-failed-jobs")

    def commit_pulls(self, full_name: str, sha: str):
        return self._req("GET", f"/repos/{full_name}/commits/{sha}/pulls")

    def branch_head(self, full_name: str, branch: str) -> str:
        return self._req("GET", f"/repos/{full_name}/git/ref/heads/{branch}")["object"]["sha"]

    def revert_to_branch(self, full_name: str, new_branch: str, bad_sha: str, message: str) -> str:
        """New branch whose tree is the parent of `bad_sha` (a revert of exactly that commit). Caller must ensure it is the branch head."""
        bad = self._req("GET", f"/repos/{full_name}/git/commits/{bad_sha}")
        if len(bad["parents"]) != 1:
            raise GitHubError(422, "cannot auto-revert a merge commit")
        parent_tree = self._req("GET", f"/repos/{full_name}/git/commits/{bad['parents'][0]['sha']}")["tree"]["sha"]
        commit = self._req("POST", f"/repos/{full_name}/git/commits", json={"message": message, "tree": parent_tree, "parents": [bad_sha]})
        self._req("POST", f"/repos/{full_name}/git/refs", json={"ref": f"refs/heads/{new_branch}", "sha": commit["sha"]})
        return commit["sha"]

    def create_issue(self, full_name: str, title: str, body: str):
        return self._req("POST", f"/repos/{full_name}/issues", json={"title": title, "body": body})

    # ---- writes: commits, branches, PRs, hooks ----
    def commit_files(self, full_name: str, branch: str, files: dict[str, str], message: str) -> str:
        """Create ONE commit on `branch` containing all `files` (Git Data API). Returns the new commit sha."""
        base = self._req("GET", f"/repos/{full_name}/git/ref/heads/{branch}")["object"]["sha"]
        base_tree = self._req("GET", f"/repos/{full_name}/git/commits/{base}")["tree"]["sha"]
        entries = []
        for path, content in files.items():
            blob = self._req("POST", f"/repos/{full_name}/git/blobs", json={"content": content, "encoding": "utf-8"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._req("POST", f"/repos/{full_name}/git/trees", json={"base_tree": base_tree, "tree": entries})
        commit = self._req("POST", f"/repos/{full_name}/git/commits",
                           json={"message": message, "tree": tree["sha"], "parents": [base]})
        self._req("PATCH", f"/repos/{full_name}/git/refs/heads/{branch}", json={"sha": commit["sha"]})
        return commit["sha"]

    def create_branch(self, full_name: str, new_branch: str, from_branch: str):
        sha = self._req("GET", f"/repos/{full_name}/git/ref/heads/{from_branch}")["object"]["sha"]
        self._req("POST", f"/repos/{full_name}/git/refs", json={"ref": f"refs/heads/{new_branch}", "sha": sha})

    def open_pr(self, full_name: str, title: str, body: str, head: str, base: str):
        return self._req("POST", f"/repos/{full_name}/pulls", json={"title": title, "body": body, "head": head, "base": base})

    def create_webhook(self, full_name: str, url: str, secret: str) -> int:
        payload = {"name": "web", "active": True, "events": ["pull_request", "issue_comment", "pull_request_review_comment", "workflow_run"],
                   "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}}
        return self._req("POST", f"/repos/{full_name}/hooks", json=payload)["id"]

    def request_reviewers(self, full_name: str, number: int, logins: list[str]):
        return self._req("POST", f"/repos/{full_name}/pulls/{number}/requested_reviewers", json={"reviewers": logins})

    def delete_webhook(self, full_name: str, hook_id: int):
        self._req("DELETE", f"/repos/{full_name}/hooks/{hook_id}")


# ---------------------------------------------------------------- diff helpers
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def visible_lines(patch: str | None) -> set[int]:
    """New-file line numbers that appear in a unified-diff patch (the only lines GitHub lets us comment on)."""
    lines: set[int] = set()
    if not patch:
        return lines
    current = 0
    for raw in patch.splitlines():
        m = HUNK.match(raw)
        if m:
            current = int(m.group(1))
            continue
        if raw.startswith("-") or raw.startswith("\\"):
            continue
        lines.add(current)
        current += 1
    return lines


def added_lines(patch: str | None) -> list[tuple[int, str]]:
    out, current = [], 0
    for raw in (patch or "").splitlines():
        m = HUNK.match(raw)
        if m:
            current = int(m.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            out.append((current, raw[1:]))
            current += 1
        elif not raw.startswith("-") and not raw.startswith("\\"):
            current += 1
    return out


def snap_to_diff(line: int | None, visible: set[int]) -> int | None:
    """Return `line` if commentable, else the nearest commentable line (or None)."""
    if not visible:
        return None
    if line in visible:
        return line
    if line is None:
        return min(visible)
    return min(visible, key=lambda v: abs(v - line))


def annotate_patch(patch: str | None) -> str:
    """Prefix every visible line with its NEW-file line number so the LLM can cite exact lines."""
    out, current = [], 0
    for raw in (patch or "").splitlines():
        m = HUNK.match(raw)
        if m:
            current = int(m.group(1))
            out.append(raw)
        elif raw.startswith("-") or raw.startswith("\\"):
            out.append("      " + raw)
        else:
            out.append(f"{current:>5} {raw}")
            current += 1
    return "\n".join(out)
