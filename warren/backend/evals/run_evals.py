"""Eval-driven development for the CI Doctor's deterministic stage (classification + log digest).
    python evals/run_evals.py          # fast, offline, no keys - run it before changing ci_logs.py
Add a case to ci_cases.json every time Warren misreads a real failure."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.ci_logs import classify, digest, fingerprint  # noqa: E402

cases = json.load(open(os.path.join(os.path.dirname(__file__), "ci_cases.json")))
wrong = []
for case in cases:
    got = classify(digest(case["log"]))
    if got != case["category"]:
        wrong.append((case["name"], case["category"], got))
print(f"classifier accuracy: {len(cases) - len(wrong)}/{len(cases)}")
for name, want, got in wrong:
    print(f"  ✗ {name}: expected {want}, got {got}")
a = "2026-11-10T10:00:01Z E   AssertionError: assert 11 == 10 (took 3.2s)\nFAILED tests/test_total.py::test_total\n"
b = "2026-11-11T09:41:55Z E   AssertionError: assert 11 == 10 (took 4.9s)\nFAILED tests/test_total.py::test_total\n"
c = "FAILED tests/test_other.py::test_other\n"
checks = [("fingerprint stable across timestamps/durations", fingerprint(a) == fingerprint(b)), ("fingerprint differs per failing test", fingerprint(a) != fingerprint(c))]
for name, ok in checks:
    print(("  ✓ " if ok else "  ✗ ") + name)
    wrong += [] if ok else [(name, "", "")]
sys.exit(1 if wrong else 0)
