"""Dependency vulnerability scanning with OSV.dev (free, no key) + manifest bump helper."""
import json
import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger("warren.security")
OSV = "https://api.osv.dev/v1"
MANIFESTS = ("requirements.txt", "package.json")
SEVERITY_MAP = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}


@dataclass
class Dep:
    name: str
    version: str
    ecosystem: str
    manifest: str


def parse_manifests(files: dict[str, str]) -> list[Dep]:
    deps: list[Dep] = []
    for path, text in files.items():
        base = path.rsplit("/", 1)[-1]
        if base == "requirements.txt":
            for line in text.splitlines():
                m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==|>=|~=)\s*([0-9][^\s,;#]*)", line)
                if m:
                    deps.append(Dep(m.group(1), m.group(2), "PyPI", path))
        elif base == "package.json":
            try:
                data = json.loads(text)
            except ValueError:
                continue
            for section in ("dependencies", "devDependencies"):
                for name, spec in (data.get(section) or {}).items():
                    m = re.match(r"^[\^~>=<v\s]*([0-9]+\.[0-9]+(?:\.[0-9]+)?[^\s]*)$", str(spec))
                    if m:
                        deps.append(Dep(name, m.group(1), "npm", path))
    return deps


def _fixed_version(vuln: dict, dep: Dep) -> str | None:
    for aff in vuln.get("affected", []):
        if aff.get("package", {}).get("name", "").lower() != dep.name.lower():
            continue
        for rng in aff.get("ranges", []):
            for event in rng.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return None


def scan(deps: list[Dep], limit: int = 80) -> list[dict]:
    """Return one finding dict per (dependency, advisory). Never raises - network problems yield []."""
    deps = deps[:limit]
    if not deps:
        return []
    try:
        queries = [{"package": {"name": d.name, "ecosystem": d.ecosystem}, "version": d.version} for d in deps]
        batch = requests.post(f"{OSV}/querybatch", json={"queries": queries}, timeout=20).json().get("results", [])
        cache: dict[str, dict] = {}
        findings = []
        for dep, result in zip(deps, batch, strict=False):
            for hit in (result.get("vulns") or [])[:5]:
                vid = hit["id"]
                if vid not in cache:
                    cache[vid] = requests.get(f"{OSV}/vulns/{vid}", timeout=20).json()
                vuln = cache[vid]
                raw = (vuln.get("database_specific") or {}).get("severity", "MEDIUM")
                findings.append({
                    "package": dep.name, "version": dep.version, "ecosystem": dep.ecosystem, "manifest": dep.manifest,
                    "vuln_id": vid, "summary": vuln.get("summary") or vuln.get("details", "")[:160],
                    "severity": SEVERITY_MAP.get(str(raw).upper(), "medium"), "fixed_in": _fixed_version(vuln, dep),
                })
        return findings
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("OSV scan failed: %s", exc)
        return []


def apply_bump(manifest_path: str, text: str, package: str, new_version: str) -> str:
    """Rewrite one dependency's version in requirements.txt / package.json, keeping the range prefix."""
    if manifest_path.endswith("package.json"):
        return re.sub(rf'("{re.escape(package)}"\s*:\s*")([\^~]?)[^"]+(")', rf"\g<1>\g<2>{new_version}\g<3>", text, count=1)
    return re.sub(rf"(?im)^(\s*{re.escape(package)}\s*)(?:==|>=|~=)\s*[^\s,;#]+",
                  lambda m: f"{m.group(1)}=={new_version}", text, count=1)
