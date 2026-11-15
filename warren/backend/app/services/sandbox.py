"""E2B sandbox wrapper. All untrusted work (cloning, linters, tests, AI-written scripts) runs here."""
import logging
import shlex
from dataclasses import dataclass

from ..config import settings
from .safety import valid_full_name

log = logging.getLogger("warren.sandbox")
REPO_DIR = "/home/user/repo"


class SandboxUnavailable(Exception):
    pass


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


class SandboxSession:
    """Context manager around one E2B microVM:  with SandboxSession() as sb: sb.run('ls')"""

    def __init__(self):
        if not settings.sandbox_enabled:
            raise SandboxUnavailable("E2B_API_KEY is not set")
        self._sb = None

    def __enter__(self):
        from e2b import Sandbox

        try:
            self._sb = Sandbox.create(timeout=settings.sandbox_timeout, api_key=settings.e2b_api_key)
        except Exception as exc:  # network, quota, bad key
            raise SandboxUnavailable(str(exc)) from exc
        return self

    def __exit__(self, *exc):
        if self._sb:
            try:
                self._sb.kill()
            except Exception:
                log.warning("could not kill sandbox", exc_info=True)

    def run(self, cmd: str, cwd: str | None = None, timeout: int = 60, envs: dict | None = None) -> ExecResult:
        from e2b import CommandExitException

        try:
            r = self._sb.commands.run(cmd, cwd=cwd, timeout=timeout, envs=envs or {})
            return ExecResult(r.stdout or "", r.stderr or "", r.exit_code)
        except CommandExitException as exc:  # non-zero exit is a normal outcome, not an error
            return ExecResult(exc.stdout or "", exc.stderr or "", exc.exit_code)
        except Exception as exc:
            return ExecResult("", f"sandbox error: {exc}", 124)

    def write(self, path: str, content: str):
        self._sb.files.write(path, content)

    def read(self, path: str) -> str:
        return self._sb.files.read(path)

    def clone_pr(self, full_name: str, pr_number: int, token: str) -> ExecResult:
        """Clone the repo and check out the PR head. The token only lives in an env var, never in the remote URL."""
        valid_full_name(full_name)
        script = (
            f"git clone --depth 50 https://x-access-token:$GH_TOKEN@github.com/{full_name}.git {REPO_DIR} -q && cd {REPO_DIR} "
            f"&& git fetch --depth 50 origin pull/{int(pr_number)}/head -q && git checkout -q FETCH_HEAD "
            f"&& git remote set-url origin https://github.com/{full_name}.git"
        )
        return self.run(script, timeout=180, envs={"GH_TOKEN": token})

    def clone_ref(self, full_name: str, ref: str, token: str, sha: str | None = None) -> ExecResult:
        """Clone a branch (optionally pinned to the exact failing commit) without leaking the token into the remote."""
        valid_full_name(full_name)
        pin = f" && (git fetch --depth 50 origin {quote(sha)} -q && git checkout -q {quote(sha)} || true)" if sha else ""
        script = (f"git clone --depth 50 --branch {quote(ref)} https://x-access-token:$GH_TOKEN@github.com/{full_name}.git {REPO_DIR} -q "
                  f"&& cd {REPO_DIR}{pin} && git remote set-url origin https://github.com/{full_name}.git")
        return self.run(script, timeout=180, envs={"GH_TOKEN": token})

    def repo_run(self, cmd: str, timeout: int = 60) -> ExecResult:
        return self.run(cmd, cwd=REPO_DIR, timeout=timeout)


def quote(path: str) -> str:
    return shlex.quote(path)
