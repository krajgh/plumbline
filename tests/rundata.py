"""Builders for the files of a run: records under .plumbline/runs/<run-id>/, its
ledger, and the per-agent records of a review unit. Plain helpers, no fixtures.

A record that an agent wrote is traced in the ledger by the SubagentStop hook (the agent's entry, with the record's
hash), and a merged review by `merge-review`. `put` and `put_part` write those entries the way the hook and the
command do, so that a stage's record counts; `agent=False` writes the file alone, as a hand-written record would be."""
import json
from pathlib import Path

import plumbline as pl
from helpers import commit_all, git, write
from samples import sample

RUN = "r1"
SONNET = "claude-sonnet-test"
HAIKU = "claude-haiku-test"

PASSING = "true"  # a test command that succeeds
FAILING = "false"  # and one that fails
CONTROLLED = "sh -c 'exit $(cat .plumbline/exit-code 2>/dev/null || echo 0)'"  # ends with the status in .plumbline/exit-code, else 0


def exits(code: int) -> str:
    """A command that ends with this exit status."""
    return f"sh -c 'exit {code}'"


def set_exit_code(repo, code: int) -> None:
    """Make CONTROLLED end with `code`. The file is in .plumbline/, which is no part of any change."""
    write(repo / ".plumbline" / "exit-code", f"{code}\n")


def commands_toml(**commands) -> str:
    """A [commands] table for plumbline.toml: `test="..."`, `lint=...`, and `timeout=<seconds>`."""
    lines = ["", "[commands]"]
    for name, value in commands.items():
        lines.append(f"{name} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def adopt(repo, commit=True, commands=None) -> None:
    """Write plumbline.toml and a .gitignore that ignores .plumbline/, and commit both. `commands` maps a command
    class (test, lint, ...) to its command, and adds them to plumbline.toml: `gate` runs those."""
    pl.init_repo(repo, graft=False)
    if commands:
        with open(repo / "plumbline.toml", "a", encoding="utf-8") as handle:
            handle.write(commands_toml(**commands))
    if commit:
        commit_all(repo, "adopt plumbline")


def adopt_base(repo, commands=None) -> None:
    """Adopt, and make the adoption part of the base branch: what is changed afterwards is measured from a base that
    already has plumbline.toml, as in a repository that adopted plumbline long ago."""
    adopt(repo, commands=commands)
    git(repo, "branch", "-f", "main", "HEAD")


def run_path(repo, run_id=RUN, *parts) -> Path:
    return repo.joinpath(".plumbline", "runs", run_id, *parts)


# ---------------------------------------------------- the ledger: what the hook and the commands enter

STAGE_ROLES = {"plan": "planner", "tests": "test-writer", "build": "builder", "verify": "verifier"}
STAGE_RECORDS = {"plan": "spec", "tests": "tests_record", "build": "build_note", "verify": "verify_record"}
PART_ROLES = {"findings_record": "prosecutor", "defense_record": "defender", "gaps_record": "detective"}


def ledger(repo, run_id=RUN) -> list[dict]:
    return pl.read_ledger(repo, run_id)


def write_ledger(repo, rows, run_id=RUN) -> None:
    for row in rows:
        pl.append_ledger(repo, run_id, row)


def agent_stopped(repo, stage_id, path, role, record_type, valid=True, run_id=RUN, agent_id=None, sha=None) -> None:
    """Enter the stop of plumbline:<role> the way the SubagentStop hook does: the record and its hash as they are now. Each call is
    a new agent (a round of its stage) unless it names the `agent_id` of one that stopped before."""
    pl.append_ledger(
        repo,
        run_id,
        {
            "kind": "agent", "agent_id": agent_id or f"agent-{stage_id}-{len(ledger(repo, run_id)) + 1}", "agent_type": f"plumbline:{role}", "stage": stage_id,
            "record": pl.rel_path(repo, path), "record_type": record_type, "record_sha256": sha or pl.file_sha256(path), "valid": valid, "blocks": 0,
        },
    )


def merged(repo, stage_id, path, round_no=1, run_id=RUN, parts=()) -> None:
    """Enter a merge-review the way the command does: the review record's hash and the parts it was built from."""
    pl.append_ledger(
        repo, run_id,
        {"kind": "merge", "stage": stage_id, "round": round_no, "record": pl.rel_path(repo, path), "record_sha256": pl.file_sha256(path), "parts": list(parts)},
    )


def run_entry(repo, stage_id, diff=None, run_id=RUN, exit_code=0, cmd=None, junit=None, before=None, after=None, extra=()) -> None:
    """Enter a measured run the way `gate` does: the commands and how they ended, and the guarded files before and after. The
    test command is the repository's own when it declares one (as `gate` runs it), else `cmd`, else `true`. A verify run also
    runs the lint, typecheck and build commands the repository declares, and each of those ends 0."""
    declared = pl.load_project(repo).commands
    test = declared.get("test")
    command = {"name": "test", "cmd": cmd or (test[0] if test else PASSING), "exit_code": exit_code, "seconds": 0.01, **({"junit": junit} if junit else {})}
    others = [{"name": name, "cmd": declared[name][0], "exit_code": 0, "seconds": 0.01} for name in ("lint", "typecheck", "build") if stage_id == "verify" and declared.get(name)]
    pl.append_ledger(
        repo, run_id,
        {
            "kind": "run", "stage": stage_id, "gate": "verify_green" if stage_id == "verify" else "tests_fail_on_stub", "commands": [command, *others, *extra],
            "diff_sha256": diff, "guarded_before": before or {}, "guarded_after": after if after is not None else (before or {}), "timeout": 900,
        },
    )


def put(repo, stage_id, data, run_id=RUN, agent=True) -> Path:
    """Write a stage's record; `data` may be a string to write as it is. With `agent` (the default) the ledger is told what would
    have told it: the intake record by `plan --intent`, an agent's record by the hook, a merged review by `merge-review`.
    `agent=False` writes the file alone, as a person or a stray process would."""
    path = run_path(repo, run_id, f"{stage_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    if agent and isinstance(data, dict):
        if stage_id == "intake":
            pl.append_ledger(
                repo, run_id,
                {"kind": "intake", "stage": "intake", "record": pl.rel_path(repo, path), "record_sha256": pl.file_sha256(path), "intent": data.get("intent"), "row": data.get("row")},
            )
        elif stage_id in STAGE_ROLES:
            agent_stopped(repo, stage_id, path, STAGE_ROLES[stage_id], STAGE_RECORDS[stage_id], valid=not pl.check_record(STAGE_RECORDS[stage_id], data), run_id=run_id)
        elif stage_id in ("review", "test-review"):
            merged(repo, stage_id, path, data.get("round", 1), run_id)
    return path


def begin(repo, row="docs", run_id=RUN, **changes) -> Path:
    """Start a run without `plan --intent`: an intake record for `row`, entered in the ledger as the command would."""
    return put(repo, "intake", intake_record(row, repo, **changes), run_id)


def verify_now(repo, green=True, run_id=RUN) -> Path:
    """The verifier's record for the change as it is now, entered in the ledger as the hook would."""
    return put(repo, "verify", verify_record(green=green, diff=now_hash(repo, run_id)), run_id)


def merge_base_of(repo, run_id=RUN) -> str:
    """The run's merge base: the intake record's, or, before there is one, the merge base with main."""
    intake = run_path(repo, run_id, "intake.json")
    if intake.is_file():
        return json.loads(intake.read_text(encoding="utf-8"))["merge_base"]
    return git(repo, "merge-base", "main", "HEAD").strip()


def now_hash(repo, run_id=RUN) -> str:
    """The hash of the change as the files are now, uncommitted work and untracked files included: what a part must carry."""
    return pl.change_hash(repo, merge_base_of(repo, run_id))


def put_part(repo, stage_id, name, data, round_no=1, run_id=RUN, agent=True) -> Path:
    """Write one agent's record of a review unit's round. A record that lacks the hash of the change gets the change as it is now; with
    `agent` the ledger holds the agent's stop, as the hook would have entered it."""
    if isinstance(data, dict) and "diff_sha256" not in data and ({"lens", "defender", "gaps"} & set(data)):
        data = {**data, "diff_sha256": now_hash(repo, run_id)}
    path = run_path(repo, run_id, stage_id, f"round-{round_no}", f"{name}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    if agent and isinstance(data, dict):
        kind = pl._part_type(data) or pl._guess_part_type(data)
        agent_stopped(repo, stage_id, path, PART_ROLES[kind], kind, valid=not pl.check_record(kind, data), run_id=run_id, agent_id=f"agent-{name}")
    return path


def read(repo, stage_id, run_id=RUN):
    return json.loads(run_path(repo, run_id, f"{stage_id}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------- records


def intake_record(row, repo=None, **changes) -> dict:
    """An intake record for `row`. With a `repo`, its base, merge base and head are the repository's own, as a real classification has them."""
    record = sample("change_class")
    record["row"] = row
    if repo is not None:
        record.update(base="main", head=git(repo, "rev-parse", "HEAD").strip(), merge_base=git(repo, "merge-base", "main", "HEAD").strip())
    record.update(changes)
    return record


def change_of(repo) -> str:
    """The diff_sha256 of what HEAD holds, measured from the merge base with main: what `pass` compares the records with."""
    return pl.change_hash(repo, git(repo, "merge-base", "main", "HEAD").strip(), pl.head_tree(repo))


def spec_record(planned=("AC-1", "AC-2")) -> dict:
    """A spec with acceptance criteria AC-1 and AC-2 and a test plan entry for each of `planned`."""
    record = sample("spec")
    record["test_plan"] = [{"ac": ac, "scenario": f"the scenario of {ac}"} for ac in planned]
    return record


def written_tests_record(covering=("AC-1", "AC-2"), ran=True, all_failed=True) -> dict:
    """A tests_record with one test per acceptance criterion in `covering`: test_ac_1 in tests/test_app.py, and so on."""
    record = sample("tests_record")
    record["tests"] = [
        {"id": f"T-{n}", "file": "tests/test_app.py", "name": f"test_{ac.lower().replace('-', '_')}", "ac_ids": [ac], "scenario": f"the scenario of {ac}"}
        for n, ac in enumerate(covering, 1)
    ]
    record["stub_check"] = {"ran": ran, "all_failed_on_assertions": all_failed, "detail": "run against stubs"}
    return record


def write_test_file(repo, covering=("AC-1", "AC-2"), path="tests/test_app.py") -> Path:
    """The test file `written_tests_record` names, holding a test for each criterion in `covering`."""
    body = "".join(f"def test_{ac.lower().replace('-', '_')}():\n    assert False\n\n" for ac in covering)
    return write(repo / path, body)


def build_note_record() -> dict:
    return sample("build_note")


def verify_record(green=True, diff=None) -> dict:
    record = sample("verify_record")
    if diff is not None:
        record["diff_sha256"] = diff
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


def review_record(blockers=0, target="diff", round_no=1, diff=None) -> dict:
    """A review_record: clean when `blockers` is 0, else the sample with a surviving blocker."""
    record = sample("review_record")
    record.update(target=target, round=round_no)
    if diff is not None:
        record["diff_sha256"] = diff
    if not blockers:
        record.update(findings=[], defenses=[], survivors=[], gaps=[], blockers_surviving=0, routes={"builder": [], "test-writer": []})
    return record


def write_docs_run(repo, run_id=RUN) -> None:
    """A finished run of the docs row (intake, verify, review) awaiting its reduce. Its verify and review
    records cover the change HEAD holds, so `pass` accepts it as long as HEAD's change stays as it is. The ledger
    traces them as it would have: the verifier's stop, the run `gate` made of the commands, and the merge."""
    diff = change_of(repo)
    put(repo, "intake", intake_record("docs", repo), run_id)
    put(repo, "verify", verify_record(diff=diff), run_id)
    run_entry(repo, "verify", diff, run_id)
    put(repo, "review", review_record(diff=diff), run_id)


def write_code_s_run(repo, run_id=RUN) -> None:
    """A finished run of the code.S row (intake, plan, tests, build, verify, review) awaiting its reduce. The test file its
    tests record names is written and committed first, so that the tree is clean, as `pass` needs."""
    write_test_file(repo)
    commit_all(repo, "the tests")
    diff = change_of(repo)
    put(repo, "intake", intake_record("code.S", repo), run_id)
    put(repo, "plan", spec_record(), run_id)
    put(repo, "tests", written_tests_record(), run_id)
    run_entry(repo, "tests", None, run_id, exit_code=1)
    put(repo, "build", build_note_record(), run_id)
    put(repo, "verify", verify_record(diff=diff), run_id)
    run_entry(repo, "verify", diff, run_id)
    put(repo, "review", review_record(diff=diff), run_id)


def genuine_pass(repo, run_id=RUN) -> dict:
    """Run a docs-row pipeline to its end in `repo` (adopted, its tree clean) and record the pass for HEAD, as
    the `pass` command does: the pass file, the run's copy, and the ledger entry that pins every record."""
    write_docs_run(repo, run_id)
    project = pl.load_project(repo)
    record, problems, run_copy = pl.make_pass_record(project, run_id)
    assert record is not None, problems
    pass_file = repo / ".plumbline" / "pass" / f"{record['commit']}.json"
    pl.write_json_atomic(run_copy, record)
    pl.write_json_atomic(pass_file, record)
    pl.note_pass(repo, run_id, record, pass_file)
    return record


def gate_stages(run_cli, repo, stages, run_id=RUN) -> None:
    """Evaluate the gates of `stages` through the CLI, so that the ledger has them."""
    for stage in stages:
        result = run_cli("gate", run_id, stage, cwd=repo)
        assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------- transcripts and the ledger


def assistant_record(message_id, model, output, inp=0, cache_write=0, cache_read=0, block="text", stop_reason="end_turn") -> dict:
    """One transcript record of an assistant message, as Claude Code writes them: one per streamed content block. The first records of a
    message carry a snapshot of the usage taken as it started and `stop_reason` null (pass `stop_reason=None`); the last carries the
    real count and the reason the message ended, which is what the default stands for."""
    content = {"type": "text", "text": "words"} if block == "text" else {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
    return {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": model,
            "content": [content],
            "stop_reason": stop_reason,
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


def text_record(text) -> dict:
    """One transcript record of an assistant message that says `text`."""
    return {
        "type": "assistant", "isSidechain": True,
        "message": {"id": "msg_text", "role": "assistant", "model": SONNET, "content": [{"type": "text", "text": text}]},
    }


def handback_record(report, call_id="toolu_hb") -> dict:
    """One transcript record of an assistant message that calls SubagentHandback with `report`, as Claude Code writes a tool call."""
    return {
        "type": "assistant", "isSidechain": True,
        "message": {
            "id": f"msg_{call_id}", "role": "assistant", "model": SONNET,
            "content": [{"type": "tool_use", "id": call_id, "name": "SubagentHandback", "input": {"message": report}}],
        },
    }


def refusal_record(call_id="toolu_hb", text="Only the auto-mode classifier can allow SubagentHandback: the session is not in auto mode") -> dict:
    """The transcript record of the answer to a tool call that was refused."""
    return {
        "type": "user", "isSidechain": True,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": True}]},
    }


def agent_row(agent_id, **fields) -> dict:
    """A ledger row of a plumbline agent that stopped."""
    return {"kind": "agent", "agent_id": agent_id, "agent_type": "plumbline:builder", "stage": "build", "record": "x", "valid": True, **fields}
