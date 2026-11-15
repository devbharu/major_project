"""Deterministic import graph (Python / JS / TS) used for blast-radius analysis and review context."""
import posixpath
import re

PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*|import\s+)['"]([^'"]+)['"]""")
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs")


class CodeGraph:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.edges: dict[str, set[str]] = {p: self._imports(p, t) for p, t in files.items()}

    def _imports(self, path: str, text: str) -> set[str]:
        found: set[str] = set()
        if path.endswith(".py"):
            for m in PY_IMPORT.finditer(text):
                dotted = (m.group(1) or m.group(2)).lstrip(".")
                suffix = dotted.replace(".", "/")
                for cand in self.files:
                    if cand.endswith((suffix + ".py", suffix + "/__init__.py")) and cand != path:
                        found.add(cand)
        elif path.endswith(JS_EXT):
            for m in JS_IMPORT.finditer(text):
                spec = m.group(1)
                if not spec.startswith("."):
                    continue
                base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
                for cand in [base, *(base + e for e in JS_EXT), *(f"{base}/index{e}" for e in JS_EXT)]:
                    if cand in self.files and cand != path:
                        found.add(cand)
        return found

    def dependents(self, changed: list[str]) -> dict[str, list[str]]:
        """For each changed file, the indexed files that import it (one hop)."""
        out = {}
        for path in changed:
            users = sorted(src for src, deps in self.edges.items() if path in deps)
            if users:
                out[path] = users
        return out

    def summary(self, changed: list[str], limit: int = 12) -> str:
        deps = self.dependents(changed)
        if not deps:
            return "No indexed files import the changed files."
        return "\n".join(f"- {p} is imported by: {', '.join(u[:limit])}" for p, u in deps.items())
