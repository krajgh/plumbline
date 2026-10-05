"""One valid sample of each record type. Generic names only."""
import copy

SHA_A = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "89abcdef0123456789abcdef0123456789abcdef"
DIFF_HASH = "0123456789abcdef" * 4  # a diff_sha256: 64 hex digits

_SAMPLES = {
    "change_class": {
        "base": "origin/main",
        "head": SHA_A,
        "merge_base": SHA_B,
        "files": [
            {"path": "src/app.py", "type": "code", "added": 80, "removed": 20, "generated": False, "symlink": False},
            {"path": "uv.lock", "type": "code", "added": 300, "removed": 0, "generated": True, "symlink": False},
            {"path": "docs/link.md", "type": "docs", "added": 1, "removed": 0, "generated": False, "symlink": True},
        ],
        "types": ["code", "docs"],
        "lines": 101,
        "size": "M",
        "row": "code.M",
        "intent": "feature",
        "symlinks": ["docs/link.md"],
        "notes": ["1 untracked file(s) that are not ignored were counted as added lines"],
    },
    "spec": {
        "goal": "Add a retry to the fetch helper.",
        "non_goals": ["Changing the timeout"],
        "acceptance_criteria": [
            {"id": "AC-1", "statement": "A failed fetch is retried up to three times.", "verification": "test"},
            {"id": "AC-2", "statement": "The final error is raised unchanged.", "verification": "test"},
        ],
        "interfaces": [{"name": "fetch", "file": "src/app.py", "signature": "fetch(url: str, retries: int = 3) -> bytes"}],
        "test_plan": [{"ac": "AC-1", "scenario": "the server fails twice, then succeeds"}],
        "risks": ["A retry on a non-idempotent request"],
        "split_proposal": None,
    },
    "tests_record": {
        "tests": [
            {
                "id": "T-1",
                "file": "tests/test_app.py",
                "name": "test_fetch_retries_then_succeeds",
                "ac_ids": ["AC-1"],
                "scenario": "the server fails twice, then succeeds",
            }
        ],
        "files_written": ["tests/test_app.py"],
        "stub_check": {"ran": True, "all_failed_on_assertions": True, "detail": "1 test, 1 failed on an assertion"},
    },
    "build_note": {
        "files_changed": ["src/app.py"],
        "summary": "Wrapped the request in a bounded retry loop.",
        "acs_addressed": ["AC-1", "AC-2"],
        "assumptions": ["Three retries means four attempts in total"],
    },
    "verify_record": {
        "commands": [
            {"name": "tests", "command": "python3 -m pytest -q", "exit_code": 1, "summary": "1 failed, 12 passed"},
            {"name": "lint", "command": "ruff check .", "exit_code": 0, "summary": "clean"},
        ],
        "tests": {"passed": 12, "failed": 1, "skipped": 0},
        "failing_acs": [{"ac": "AC-1", "error_type": "AssertionError"}],
        "checks": {"lint": True, "secrets": True, "symlinks": False, "abs_paths": True, "graft_fresh": None},
        "green": False,
        "diff_sha256": DIFF_HASH,
    },
    "review_record": {
        "target": "diff",
        "round": 1,
        "lenses": ["correctness", "security"],
        "findings": [
            {
                "id": "F-1",
                "lens": "correctness",
                "file": "src/app.py",
                "line": 14,
                "claim": "The retry loop swallows the last error.",
                "failure_scenario": "Every attempt fails: the caller receives None instead of the error.",
                "rule": "AC-2",
                "evidence": "except OSError: continue",
                "outside_code": None,
                "severity": "BLOCKING",
                "evidence_unverified": False,
            },
            {
                "id": "F-2",
                "lens": "security",
                "file": "src/app.py",
                "line": 9,
                "claim": "The URL is logged with its query string.",
                "failure_scenario": "A token in the query string reaches the log.",
                "rule": "Severity rubric: a secret is exposed",
                "evidence": "log.info(url)",
                "outside_code": "The log is shipped to a third party.",
                "severity": "MAJOR",
                "evidence_unverified": True,
            },
        ],
        "defenses": [
            {"finding_id": "F-1", "defender": "d1", "verdict": "conceded", "quote": "continue", "reason": "The loop never raises."},
            {"finding_id": "F-2", "defender": "d1", "verdict": "refuted", "quote": "log.info(redact(url))", "reason": "The URL is redacted first."},
        ],
        "survivors": ["F-1"],
        "gaps": [
            {"id": "G-1", "kind": "uncovered_ac", "detail": "No test covers AC-2.", "ac": "AC-2"},
            {"id": "G-2", "kind": "edge_case", "detail": "retries=0 is not handled.", "ac": None},
        ],
        "blockers_surviving": 1,
        "routes": {"builder": ["F-1"], "test-writer": []},
        "panel_needed": [],
        "diff_sha256": DIFF_HASH,
    },
    "findings_record": {
        "lens": "correctness",
        "findings": [
            {
                "id": "correctness-1",
                "lens": "correctness",
                "file": "src/app.py",
                "line": 14,
                "claim": "The retry loop swallows the last error.",
                "failure_scenario": "Every attempt fails: the caller receives None instead of the error.",
                "rule": "AC-2",
                "evidence": "except OSError: continue",
                "outside_code": None,
                "severity": "BLOCKING",
            },
            {
                "id": "correctness-2",
                "lens": "correctness",
                "file": "src/app.py",
                "line": 21,
                "claim": "retries=0 is treated as no limit.",
                "failure_scenario": "fetch(url, retries=0) loops forever on a dead server.",
                "rule": "Severity rubric: an edge case",
                "evidence": "while not retries or attempt < retries:",
                "outside_code": None,
                "severity": "MINOR",
            },
        ],
        "diff_sha256": DIFF_HASH,
    },
    "defense_record": {
        "defender": "defender-1",
        "defenses": [
            {"finding_id": "correctness-1", "defender": "defender-1", "verdict": "conceded", "quote": "continue", "reason": "The loop never raises."},
            {"finding_id": "correctness-2", "defender": "defender-1", "verdict": "refuted", "quote": "retries: int = 3", "reason": "The default is 3, and 0 is documented as no retries."},
        ],
        "diff_sha256": DIFF_HASH,
    },
    "gaps_record": {
        "gaps": [
            {"id": "G-1", "kind": "uncovered_ac", "detail": "No test covers AC-2.", "ac": "AC-2"},
            {"id": "G-2", "kind": "edge_case", "detail": "retries=0 is not handled.", "ac": None},
        ],
        "diff_sha256": DIFF_HASH,
    },
    "pass_record": {
        "commit": SHA_A,
        "run_id": "demo-20260101T000000Z",
        "row": "code.M",
        "stages": [
            {"id": "intake", "record": ".plumbline/runs/demo/intake.json", "gate": None, "passed": True, "rounds": 1},
            {"id": "verify", "record": ".plumbline/runs/demo/verify.json", "gate": "verify_green", "passed": True, "rounds": 2},
        ],
        "tokens": {"by_model": {"sonnet": {"output": 1200, "fresh_input": 3400, "cache_read": 56000}}},
        "verdict": "pass",
        "notes": ["verify needed two rounds"],
        "open_findings": [
            {
                "stage": "review",
                "id": "correctness-2",
                "lens": "correctness",
                "severity": "MINOR",
                "file": "src/app.py",
                "line": 21,
                "claim": "retries=0 is treated as no limit.",
                "failure_scenario": "fetch(url, retries=0) loops forever on a dead server.",
            }
        ],
        "gaps": [{"stage": "review", "id": "G-2", "kind": "edge_case", "detail": "retries=0 is not handled.", "ac": None}],
    },
    "override_record": {
        "commit": SHA_A,
        "reason": "The pipeline cannot run offline and this is a one-line typo fix.",
        "by": "the builder",
        "at": "2026-01-01T00:00:00Z",
        "stages_skipped": ["plan", "tests", "review"],
    },
}

RECORD_TYPES = tuple(_SAMPLES)


def sample(name: str) -> dict:
    """A fresh, mutable copy of the valid sample for `name`."""
    return copy.deepcopy(_SAMPLES[name])
