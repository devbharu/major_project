"""Turn a huge CI log into the few lines that matter, classify the failure, and recover the failing command."""
import hashlib
import re

import yaml

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ERROR_LINE = re.compile(r"(##\[error\]|Traceback|^E\s{2,}|\bFAILED\b|\bERROR\b|npm ERR!|error TS\d+|AssertionError|SyntaxError|"
                        r"ModuleNotFoundError|Cannot find module|✖|✘|error:|Error:|panic:|fatal:)", re.I)

# (category, regex) - first match wins, ordered from most specific to most generic
RULES = [
    ("workflow_config", re.compile(r"(An error occurred trying to start process .* with working directory|Unable to resolve action|Invalid workflow file|"
                                   r"The workflow is not valid|Unrecognized named-value|Path Validation Error|No file in .* matched to \[|Unexpected value '|"
                                   r"Required property is missing|Error: Unable to locate executable file)", re.I)),
    ("permissions_config", re.compile(r"(Resource not accessible by integration|Permission denied \(publickey\)|authentication failed|"
                                      r"secret .* (is )?(not set|missing)|Bad credentials|requires authentication|403 Forbidden|"
                                      r"Input required and not supplied|AccessDenied|Unauthorized)", re.I)),
    ("flaky_infra", re.compile(r"(exit code 137|The runner has received a shutdown signal|ECONNRESET|ETIMEDOUT|Could not resolve host|"
                               r"503 Service Unavailable|502 Bad Gateway|rate limit exceeded|Temporary failure in name resolution|"
                               r"socket hang up|Connection reset by peer|Operation timed out|No space left on device)", re.I)),
    ("dependency", re.compile(r"(ModuleNotFoundError|No module named|Cannot find module|ERESOLVE|Could not find a version that satisfies|"
                              r"No matching distribution|npm ERR! 404|Dependency .* not found|ImportError: cannot import name)", re.I)),
    ("syntax", re.compile(r"(SyntaxError|IndentationError|Unexpected token|Parsing error|unterminated string)", re.I)),
    ("type_error", re.compile(r"(error TS\d+|mypy|pyright|Type '.*' is not assignable|incompatible type)", re.I)),
    ("lint", re.compile(r"(ruff|flake8|eslint|prettier|black --check|would reformat|isort|pylint|✖ \d+ problems?|Run .* --fix)", re.I)),
    ("build", re.compile(r"(failed to solve|COPY failed|docker build|Build failed|compilation failed|webpack|vite build|rollup|cargo build)", re.I)),
    ("test_failure", re.compile(r"(AssertionError|FAILED tests?/|=+ FAILURES =+|\d+ failed|Tests?:\s+\d+ failed|✕|expect\(.*\)\.|Error: expect)", re.I)),
]
AUTO_FIXABLE = {"workflow_config", "test_failure", "lint", "type_error", "syntax", "dependency", "build", "unknown"}


def clean(text: str) -> list[str]:
    return [TIMESTAMP.sub("", ANSI.sub("", line)).rstrip() for line in text.splitlines()]


def digest(log_text: str, max_chars: int = 7000) -> str:
    """Keep the region around the errors (CI logs are 10k+ lines; the cause is usually in 50 of them)."""
    lines = clean(log_text)
    hits = [i for i, l in enumerate(lines) if ERROR_LINE.search(l)]
    if not hits:
        chunk = lines[-80:]
    else:
        chunk = lines[max(hits[0] - 35, 0):min(hits[-1] + 12, len(lines))]
    text = "\n".join(chunk)
    if len(text) > max_chars:
        head = int(max_chars * 0.55)
        text = text[:head] + "\n... [log trimmed] ...\n" + text[-(max_chars - head):]
    return text


def classify(text: str) -> str:
    for category, rx in RULES:
        if rx.search(text):
            return category
    return "unknown"


def workflow_command(yaml_text: str, job_name: str, step_name: str) -> dict | None:
    """Find the shell command of the failing step so we can re-run exactly it in the sandbox."""
    try:
        doc = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return None
    for key, job in (doc.get("jobs") or {}).items():
        names = {key, str(job.get("name", key))}
        if not any(job_name == n or job_name.startswith(n + " (") for n in names):
            continue
        defaults = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
        for step in job.get("steps", []):
            run = step.get("run")
            if not run:
                continue
            auto_name = f"Run {run.strip().splitlines()[0]}"
            if step_name in (step.get("name"), auto_name):
                if "${{" in run:
                    return None   # depends on CI-only expressions/secrets; cannot be replayed faithfully
                return {"run": run, "cwd": step.get("working-directory") or defaults}
    return None


VOLATILE = re.compile(r"(0x[0-9a-f]+|\b[0-9a-f]{7,40}\b|\b\d+(\.\d+)?\s?(ms|s|sec|m)\b|\b\d{2,}\b|/tmp/\S+|/home/\S+?/|runner/work/\S+?/\S+?/)", re.I)
STABLE_ID = re.compile(r"(FAILED [\w/.\-]+::[\w\[\]\-.]+|ModuleNotFoundError: [^\n]+|Cannot find module [^\n]+|error TS\d+: [^\n]+|"
                       r"[\w/.\-]+:\d+:\d+: [A-Z]+\d{2,4} [^\n]+|SyntaxError: [^\n]+)")


def fingerprint(text: str) -> str:
    """Stable id of *what* failed (not when/where): two workflows failing the same test share a fingerprint,
    runs differing only in timestamps, durations, hashes or temp paths do too."""
    lines = clean(text)
    ids = sorted({VOLATILE.sub("#", m.group(0)) for line in lines for m in STABLE_ID.finditer(line)})
    key = ids[:5] or [VOLATILE.sub("#", l.strip()) for l in lines if ERROR_LINE.search(l) and "exit code" not in l][:4] or [l for l in lines[-5:]]
    return hashlib.sha1("|".join(key).encode(), usedforsecurity=False).hexdigest()[:12]


def working_directories(yaml_text: str) -> list[str]:
    """Every literal `working-directory` a workflow declares (workflow, job defaults and steps); used to prove a workflow fix points at real folders."""
    try:
        doc = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return []
    found = [((doc.get("defaults") or {}).get("run") or {}).get("working-directory")]
    for job in (doc.get("jobs") or {}).values():
        found.append(((job.get("defaults") or {}).get("run") or {}).get("working-directory"))
        found += [step.get("working-directory") for step in job.get("steps", [])]
    return sorted({w for w in found if isinstance(w, str) and "${{" not in w})
