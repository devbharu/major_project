"""Guards against 'fake has the method, real client doesn't': every GitHub/sandbox call made by the app must exist on the real class."""
import ast
import os
import unittest

from app.services.github import GitHubClient
from app.services.sandbox import SandboxSession

APP = os.path.join(os.path.dirname(__file__), "..", "app")
GH_NAMES = {"gh", "bot", "user_gh", "writer"}


def called_methods(receivers: set[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for root, _, files in os.walk(APP):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path) as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    base = node.func.value
                    label = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
                    if label in receivers:
                        found.setdefault(node.func.attr, set()).add(os.path.relpath(path, APP))
    return found


class ContractTest(unittest.TestCase):
    def test_every_github_call_exists_on_the_real_client(self):
        missing = {m: sorted(f) for m, f in called_methods(GH_NAMES).items() if not hasattr(GitHubClient, m)}
        self.assertEqual(missing, {}, "methods called on a GitHub client but not defined on GitHubClient")

    def test_every_sandbox_call_exists_on_the_real_session(self):
        missing = {m: sorted(f) for m, f in called_methods({"sb", "sandbox"}).items() if not hasattr(SandboxSession, m)}
        self.assertEqual(missing, {}, "methods called on a sandbox but not defined on SandboxSession")


if __name__ == "__main__":
    unittest.main()
