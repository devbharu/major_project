"""Change Stack: explain a PR as semantic layers, blast radius and architecture impact (not just a diff)."""
import logging
import re

from ..services import llm
from . import prompts
from .schemas import StackOutput
from .triage import SENSITIVE

log = logging.getLogger("warren.stack")
LAYER_RULES = [
    ("Data model & migrations", re.compile(r"(model|schema|migration|entity|prisma|\.sql$|database|/db)", re.I)),
    ("Core logic & services", re.compile(r"(service|core|lib|util|domain|agent|worker|job|logic)", re.I)),
    ("API & routes", re.compile(r"(api|route|controller|handler|endpoint|server|webhook)", re.I)),
    ("UI components & pages", re.compile(r"(component|page|view|screen|\.css$|\.scss$|\.jsx$|\.tsx$|\.vue$|frontend|ui/)", re.I)),
    ("Configuration & infrastructure", re.compile(r"(config|\.ya?ml$|dockerfile|workflow|package\.json|requirements|\.toml$|\.env|terraform)", re.I)),
    ("Tests", re.compile(r"(test|spec|__tests__)", re.I)),
    ("Documentation", re.compile(r"(\.md$|\.rst$|docs?/)", re.I)),
]
MAX_PATCH = 15_000
MAX_TOTAL = 300_000


class ChangeStackBuilder:
    def __init__(self, container):
        self.c = container

    def build(self, pr: dict, files: list[dict], dependents: dict[str, list[str]], findings, verify: list[dict], use_llm=True) -> dict:
        by_path = {f["filename"]: f for f in files}
        blast = self._blast(files, dependents)
        out = None
        if use_llm and files:
            try:
                digest = "\n".join(f"- {f['filename']} ({f['status']} +{f['additions']} -{f['deletions']})\n"
                                   f"{(f.get('patch') or '')[:500]}" for f in files[:60])
                out = llm.ask_json(prompts.STACK_SYSTEM, f"PR #{pr['number']}: {pr['title']}\n{pr.get('body', '')[:800]}\n\nFILES:\n{digest[:20000]}",
                                   StackOutput, temperature=0.2)
            except Exception:
                log.info("LLM change stack unavailable, using heuristic layers")
        layers = self._layers(out, by_path)
        total = 0
        packed = []
        for i, layer in enumerate(layers, 1):
            entries = []
            for path in layer["files"]:
                f = by_path[path]
                patch = (f.get("patch") or "")[:MAX_PATCH] if total < MAX_TOTAL else ""
                total += len(patch)
                entries.append({"path": path, "status": f["status"], "additions": f["additions"], "deletions": f["deletions"], "patch": patch})
            packed.append({"id": i, "title": layer["title"], "description": layer["description"], "files": entries})
        risk = out.risk if out else ("high" if any(b["severity"] == "high" for b in blast) else "medium" if blast else "low")
        return {
            "overview": {"summary": out.summary if out else f"{pr['title']} touches {len(files)} files.",
                         "intent": out.intent if out else "", "risk": risk, "files": len(files),
                         "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)},
            "layers": packed,
            "blast_radius": [b.model_dump() for b in out.blast_radius] if out and out.blast_radius else blast,
            "dependents": dependents,
            "architecture": [a.model_dump() for a in out.architecture] if out and out.architecture else self._arch(packed),
            "verify": verify,
        }

    @staticmethod
    def _blast(files, dependents) -> list[dict]:
        items = []
        sensitive = [f["filename"] for f in files if SENSITIVE.search(f["filename"])]
        if sensitive:
            items.append({"area": "Security-sensitive code", "impact": "Touches authentication, secrets, permissions or deployment config.",
                          "severity": "high", "files": sensitive[:8]})
        for path, users in dependents.items():
            items.append({"area": f"Callers of {path.rsplit('/', 1)[-1]}", "impact": f"{len(users)} file(s) import this module and may be affected.",
                          "severity": "medium" if len(users) > 3 else "low", "files": users[:8]})
        return items[:8]

    @staticmethod
    def _arch(layers) -> list[dict]:
        return [{"component": l["title"], "change": f"{len(l['files'])} file(s) changed", "note": l["description"]} for l in layers]

    @staticmethod
    def _layers(out: StackOutput | None, by_path: dict) -> list[dict]:
        layers, assigned = [], set()
        if out:
            for l in out.layers:
                paths = [p for p in l.files if p in by_path and p not in assigned]
                if paths:
                    assigned.update(paths)
                    layers.append({"title": l.title, "description": l.description, "files": paths})
        else:
            buckets = {name: [] for name, _ in LAYER_RULES}
            for path in by_path:
                name = next((n for n, rx in LAYER_RULES if rx.search(path)), "Core logic & services")
                buckets[name].append(path)
            for name, paths in buckets.items():
                if paths:
                    assigned.update(paths)
                    layers.append({"title": f"Update {name.lower()}", "description": f"{len(paths)} file(s) in this layer.", "files": paths})
        rest = [p for p in by_path if p not in assigned]
        if rest:
            layers.append({"title": "Other changes", "description": "Files not grouped into a layer.", "files": rest})
        return layers
