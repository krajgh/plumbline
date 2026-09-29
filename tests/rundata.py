"""Builders for the files of a run: records under .plumbline/runs/<run-id>/, its
ledger, and the per-agent records of a review unit. Plain helpers, no fixtures."""
import json
from pathlib import Path

import plumbline as pl
from helpers import commit_all
from samples import sample

RUN = "r1"
SONNET = "claude-sonnet-test"
HAIKU = "claude-haiku-test"


def adopt(repo, commit=True) -> None:
    """Write plumbline.toml and a .gitignore that ignores .plumbline/, and commit both."""
    pl.init_repo(repo, graft=False)
    if commit:
        commit_all(repo, "adopt plumbline")


def run_path(repo, run_id=RUN, *parts) -> Path:
    return repo.joinpath(".plumbline", "runs", run_id, *parts)


def put(repo, stage_id, data, run_id=RUN) -> Path:
    """Write a stage's record; `data` may be a string to write as it is."""
    path = run_path(repo, run_id, f"{stage_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    return path


def put_part(repo, stage_id, name, data, round_no=1, run_id=RUN) -> Path:
    """Write one agent's record of a review unit's round."""
    path = run_path(repo, run_id, stage_id, f"round-{round_no}", f"{name}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    return path


def read(repo, stage_id, run_id=RUN):
    return json.loads(run_path(repo, run_id, f"{stage_id}.json").read_text(encoding="utf-8"))


def ledger(repo, run_id=RUN) -> list[dict]:
    return pl.read_ledger(repo, run_id)


def write_ledger(repo, rows, run_id=RUN) -> None:
    for row in rows:
        pl.append_ledger(repo, run_id, row)


# ------------------------------------------------------------- records


def intake_record(row, **changes) -> dict:
    record = sample("change_class")
    record["row"] = row
    record.update(changes)
    return record


def spec_record(planned=("AC-1", "AC-2")) -> dict:
    """A spec with acceptance criteria AC-1 and AC-2 and a test plan entry for each of `planned`."""
    record = sample("spec")
    record["test_plan"] = [{"ac": ac, "scenario": f"the scenario of {ac}"} for ac in planned]
    return record


def written_tests_record(covering=("AC-1", "AC-2"), ran=True, all_failed=True) -> dict:
    """A tests_record with one test per acceptance criterion in `covering`."""
    record = sample("tests_record")
    record["tests"] = [
        {"id": f"T-{n}", "file": "tests/test_app.py", "name": f"test_{ac.lower().replace('-', '_')}", "ac_ids": [ac], "scenario": f"the scenario of {ac}"}
        for n, ac in enumerate(covering, 1)
    ]
    record["stub_check"] = {"ran": ran, "all_failed_on_assertions": all_failed, "detail": "run against stubs"}
    return record


def build_note_record() -> dict:
    return sample("build_note")


def verify_record(green=True) -> dict:
    record = sample("verify_record")
    if green:
        record["commands"] = [
            {"name": "tests", "command": "python3 -m pytest -q", "exit_code": 0, "summary": "13 passed"},
            {"name": "lint", "command": "ruff check .", "exit_code": 0, "summary": "clean"},
        ]
        record["tests"] = {"passed": 13, "failed": 0, "skipped": 0}
        record["failing_acs"] = []
        record["checks"] = {"lint": True, "secrets": True, "symlinks": True, "abs_paths": True, "graft_fresh": None}
        record["green"] = True
    return record


def review_record(blockers=0, target="diff", round_no=1) -> dict:
    """A review_record: clean when `blockers` is 0, else the sample with a surviving blocker."""
    record = sample("review_record")
    record.update(target=target, round=round_no)
    if not blockers:
        record.update(findings=[], defenses=[], survivors=[], gaps=[], blockers_surviving=0)
    return record


def write_docs_run(repo, run_id=RUN) -> None:
    """A finished run of the docs row (intake, verify, review) awaiting its reduce."""
    put(repo, "intake", intake_record("docs"), run_id)
    put(repo, "verify", verify_record(), run_id)
    put(repo, "review", review_record(), run_id)


def write_code_s_run(repo, run_id=RUN) -> None:
    """A finished run of the code.S row (intake, plan, tests, build, verify, review) awaiting its reduce."""
    put(repo, "intake", intake_record("code.S"), run_id)
    put(repo, "plan", spec_record(), run_id)
    put(repo, "tests", written_tests_record(), run_id)
    put(repo, "build", build_note_record(), run_id)
    put(repo, "verify", verify_record(), run_id)
    put(repo, "review", review_record(), run_id)


def gate_stages(run_cli, repo, stages, run_id=RUN) -> None:
    """Evaluate the gates of `stages` through the CLI, so that the ledger has them."""
    for stage in stages:
        result = run_cli("gate", run_id, stage, cwd=repo)
        assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------- transcripts and the ledger


def assistant_record(message_id, model, output, inp=0, cache_write=0, cache_read=0, block="text") -> dict:
    """One transcript record of an assistant message, as Claude Code writes them: one per streamed content block."""
    content = {"type": "text", "text": "words"} if block == "text" else {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
    return {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": model,
            "content": [content],
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
            },
        },
    }


def write_transcript(path, records, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def agent_row(agent_id, **fields) -> dict:
    """A ledger row of a plumbline agent that stopped."""
    return {"kind": "agent", "agent_id": agent_id, "agent_type": "plumbline:builder", "stage": "build", "record": "x", "valid": True, **fields}
