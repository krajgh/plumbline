"""The SubagentStop hook, fed the payloads Claude Code sends: it lets an agent go when its record validates,
blocks it (exit 2, the errors on stderr, the decision as JSON) until then, and gives up after 3 blocks."""
import json

import pytest

import plumbline as pl
import subagent_stop
from helpers import git, write
from hookdata import AGENT, stop_payload
from rundata import (
    RUN, adopt, build_note_record, ledger, put, put_part, review_record, run_path, spec_record, verify_record,
    written_tests_record,
)
from samples import sample

PLAN_LINE = "Planned it.\nRECORD: .plumbline/runs/r1/plan.json"

# the record each plumbline agent must end with, written out here as the oracle
RECORD_OF = {
    "planner": "spec", "test-writer": "tests_record", "builder": "build_note", "verifier": "verify_record",
    "prosecutor": "findings_record", "defender": "defense_record", "detective": "gaps_record",
}


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def stop(run_stop, repo, message=PLAN_LINE, **fields):
    return run_stop(stop_payload(repo, message=message, **fields), repo)


def blocked(result):
    """The reason of a blocked stop, after checking every way it is delivered."""
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert list(payload) == ["decision", "reason"] and payload["decision"] == "block"
    assert result.stderr == payload["reason"] + "\n"
    return payload["reason"]


def let_go(result):
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""


def test_every_record_type_an_agent_ends_with_has_a_schema_and_the_table_here_is_the_scripts():
    assert subagent_stop_records() == RECORD_OF
    assert set(RECORD_OF.values()) <= set(pl.record_types())


def subagent_stop_records():
    return {name: record_type for name, record_type in pl.AGENT_RECORDS.items()}


# --- a valid record lets the agent go, and is logged


def test_a_valid_record_lets_the_agent_go_and_is_entered_in_the_ledger(run_stop, adopted):
    put(adopted, "plan", spec_record())
    payload = stop_payload(adopted, message=PLAN_LINE)
    let_go(run_stop(payload, adopted))
    [entry] = ledger(adopted)
    assert entry["kind"] == "agent" and entry["at"].endswith("Z")
    assert (entry["agent_id"], entry["agent_type"], entry["stage"], entry["record"]) == (payload["agent_id"], "plumbline:planner", "plan", ".plumbline/runs/r1/plan.json")
    assert (entry["record_type"], entry["valid"], entry["blocks"]) == ("spec", True, 0)
    assert entry["session_id"] == payload["session_id"]
    assert entry["transcript"] == payload["agent_transcript_path"]
    assert entry["session_transcript"] == payload["transcript_path"]
    assert "errors" not in entry


# (agent type, stage the record belongs to, the record's path under the run, the record)
AGENTS = [
    ("planner", "plan", "plan.json", lambda: spec_record()),
    ("test-writer", "tests", "tests.json", lambda: written_tests_record()),
    ("builder", "build", "build.json", lambda: build_note_record()),
    ("verifier", "verify", "verify.json", lambda: verify_record()),
    ("prosecutor", "review", "review/round-1/prosecutor-correctness.json", lambda: sample("findings_record")),
    ("defender", "review", "review/round-1/defender-1.json", lambda: sample("defense_record")),
    ("detective", "review", "review/round-1/detective.json", lambda: sample("gaps_record")),
]


@pytest.mark.parametrize("name,stage,path,make", AGENTS, ids=[a[0] for a in AGENTS])
def test_each_agent_is_checked_against_its_own_record_type(run_stop, adopted, name, stage, path, make):
    record_type = RECORD_OF[name]
    target = run_path(adopted, RUN, *path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(make()), encoding="utf-8")
    message = f"All done.\nRECORD: .plumbline/runs/r1/{path}"
    let_go(stop(run_stop, adopted, message, agent_type=f"plumbline:{name}"))
    [entry] = ledger(adopted)
    assert (entry["stage"], entry["record_type"], entry["valid"]) == (stage, record_type, True)
    # the record of another agent's type does not do
    other = "plumbline:builder" if name != "builder" else "plumbline:planner"
    assert blocked(stop(run_stop, adopted, message, agent_type=other, agent_id="someone-else"))


def test_the_last_line_may_dress_the_label_and_the_path(run_stop, adopted):
    put(adopted, "plan", spec_record())
    for line in ("**RECORD:** .plumbline/runs/r1/plan.json", "RECORD: `.plumbline/runs/r1/plan.json`", "RECORD:.plumbline/runs/r1/plan.json", "  RECORD:   .plumbline/runs/r1/plan.json  "):
        let_go(stop(run_stop, adopted, f"Done.\n\n{line}\n\n"))
    assert len(ledger(adopted)) == 4


def test_a_relative_path_is_resolved_from_the_cwd_and_then_from_the_root(run_stop, adopted):
    put(adopted, "plan", spec_record())
    src = adopted / "src"
    payload = stop_payload(adopted, message="RECORD: .plumbline/runs/r1/plan.json")
    payload["cwd"] = str(src)  # the agent works in a subdirectory: the path is found from the root
    let_go(run_stop(payload, src))
    absolute = f"RECORD: {run_path(adopted, RUN, 'plan.json')}"
    let_go(run_stop(stop_payload(adopted, message=absolute), adopted))
    assert [e["record"] for e in ledger(adopted)] == [".plumbline/runs/r1/plan.json"] * 2


# --- an invalid record blocks, exit 2 with the errors


def test_an_invalid_record_blocks_with_exit_2_and_the_errors_with_their_paths(run_stop, adopted):
    record = spec_record()
    del record["goal"]
    record["acceptance_criteria"][0]["id"] = "AC1"
    put(adopted, "plan", record)
    reason = blocked(stop(run_stop, adopted))
    assert "the record you named is not a valid spec (2 problems)" in reason
    assert "- .plumbline/runs/r1/plan.json: $.goal: missing required key" in reason
    assert "$.acceptance_criteria[0].id: 'AC1' does not match ^AC-[0-9]+$" in reason
    assert "(attempt 1 of 3" in reason
    assert ledger(adopted) == []  # nothing is logged until the agent is let go


def test_the_block_reaches_claude_code_through_the_or_true_as_json_on_stdout(run_stop, adopted):
    # the hook is registered with `|| true`, which turns exit status 2 into 0: the JSON is what blocks
    put(adopted, "plan", {"goal": ""})
    result = stop(run_stop, adopted)
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block" and "not a valid spec" in decision["reason"]
    assert result.returncode == 2 and result.stderr.strip() == decision["reason"]


@pytest.mark.parametrize(
    "message,expect",
    [
        ("", "no RECORD line"),
        ("Done, no record.", "no RECORD line"),
        ("RECORD: .plumbline/runs/r1/plan.json\nAnd then I kept talking.", "no RECORD line"),
        ("RECORD:", "no RECORD line"),
    ],
)
def test_a_reply_that_does_not_end_with_a_record_line_blocks(run_stop, adopted, message, expect):
    put(adopted, "plan", spec_record())
    reason = blocked(stop(run_stop, adopted, message))
    assert "must end with a line of the form `RECORD: <path>`" in reason
    assert expect in reason
    assert ledger(adopted) == []


def test_a_missing_last_message_field_blocks_too(run_stop, adopted):
    payload = stop_payload(adopted)
    del payload["last_assistant_message"]
    assert "must end with a line" in blocked(run_stop(payload, adopted))


def test_a_record_that_does_not_exist_or_is_not_json_blocks(run_stop, adopted):
    assert "plan.json: no such file" in blocked(stop(run_stop, adopted, agent_id="one"))
    put(adopted, "plan", "{not json")
    assert "plan.json: not valid JSON" in blocked(stop(run_stop, adopted, agent_id="two"))


@pytest.mark.parametrize(
    "named",
    ["README.md", "/etc/hostname", "../outside.json", ".plumbline/runs/plan.json", ".plumbline/runs/.hidden/plan.json", ".plumbline/runs/r1/plan.txt", ".plumbline/pass/x.json"],
)
def test_a_record_outside_a_run_directory_blocks(run_stop, adopted, named):
    write(adopted / "README.md", "# demo\n")
    reason = blocked(stop(run_stop, adopted, f"RECORD: {named}"))
    assert "the record must be" in reason and ".plumbline/runs/<run-id>/" in reason


def test_a_long_list_of_errors_is_cut_short(run_stop, adopted):
    record = spec_record()
    record["acceptance_criteria"] = [{"id": f"bad{n}", "statement": "s", "verification": "v"} for n in range(30)]
    put(adopted, "plan", record)
    reason = blocked(stop(run_stop, adopted))
    assert reason.count("does not match") == 20 and "- and 10 more" in reason


# --- three blocks, then the agent is let go and the record marked invalid


def test_after_three_blocks_the_agent_is_let_go_and_the_record_is_marked_invalid(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    for attempt in (1, 2, 3):
        reason = blocked(stop(run_stop, adopted, active=attempt > 1))
        assert f"(attempt {attempt} of 3" in reason
    let_go(stop(run_stop, adopted, active=True))  # the fourth stop
    [entry] = ledger(adopted)
    assert (entry["valid"], entry["blocks"], entry["stage"], entry["record"]) == (False, 3, "plan", ".plumbline/runs/r1/plan.json")
    assert entry["record_type"] == "spec" and any("$.goal" in e or "missing required key" in e for e in entry["errors"])
    assert not (adopted / ".plumbline" / "blocks" / AGENT).exists()  # the count is cleared


def test_an_agent_that_never_names_a_record_is_let_go_after_three_blocks_without_a_ledger_entry(run_stop, adopted):
    for attempt in (1, 2, 3):
        blocked(stop(run_stop, adopted, "no record here", active=attempt > 1))
    let_go(stop(run_stop, adopted, "no record here", active=True))
    assert not list((adopted / ".plumbline").rglob("ledger.jsonl"))


def test_fixing_the_record_after_a_block_lets_the_agent_go_with_a_valid_entry(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    blocked(stop(run_stop, adopted))
    put(adopted, "plan", spec_record())
    let_go(stop(run_stop, adopted, active=True))
    [entry] = ledger(adopted)
    assert (entry["valid"], entry["blocks"]) == (True, 1)
    assert not (adopted / ".plumbline" / "blocks" / AGENT).exists()


def test_a_fresh_stop_starts_a_fresh_count(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    blocked(stop(run_stop, adopted))
    blocked(stop(run_stop, adopted, active=True))
    reason = blocked(stop(run_stop, adopted, active=False))  # stop_hook_active false: not a continuation of the earlier stops
    assert "(attempt 1 of 3" in reason


def test_each_agent_has_its_own_count(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    for _ in range(2):
        blocked(stop(run_stop, adopted, agent_id="first", active=True))
    assert "(attempt 1 of 3" in blocked(stop(run_stop, adopted, agent_id="second", active=True))
    assert "(attempt 3 of 3" in blocked(stop(run_stop, adopted, agent_id="first", active=True))


def test_an_agent_id_cannot_write_outside_the_blocks_directory(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    blocked(stop(run_stop, adopted, agent_id="../../escape", active=True))
    assert not (adopted / "escape").exists() and not (adopted.parent / "escape").exists()
    assert list((adopted / ".plumbline" / "blocks").iterdir())


# --- what it leaves alone


@pytest.mark.parametrize(
    "agent_type",
    ["Explore", "general-purpose", "probe:echo", "someone:builder", "plumbline:wizard", "plumbline:", "plumbline", "", None, 5,
     "plumbline_builder", "Plumbline:builder", "xplumbline:builder", " plumbline:builder"],  # look-alikes of a plumbline agent
)
def test_agents_that_are_not_plumbline_agents_are_ignored(run_stop, adopted, agent_type):
    put(adopted, "plan", {"goal": ""})  # even an invalid record would not matter
    payload = stop_payload(adopted, message=PLAN_LINE)
    payload["agent_type"] = agent_type
    let_go(run_stop(payload, adopted))
    assert ledger(adopted) == [] and not (adopted / ".plumbline" / "blocks").exists()


def test_a_repository_without_plumbline_toml_is_never_held_up(run_stop, repo):
    result = stop(run_stop, repo, "no record, and no plumbline.toml either")
    let_go(result)
    assert not (repo / ".plumbline").exists()


def test_an_invalid_plumbline_toml_still_counts_as_adopted(run_stop, repo):
    write(repo / "plumbline.toml", "schema = = 1\n")
    assert blocked(stop(run_stop, repo, "no record"))


def test_a_cwd_that_is_not_in_a_repository_or_does_not_exist_is_ignored(run_stop, adopted, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    let_go(run_stop(stop_payload(plain, message="no record"), plain, GIT_CEILING_DIRECTORIES=str(tmp_path)))
    gone = stop_payload(adopted, message="no record") | {"cwd": str(tmp_path / "gone")}
    let_go(run_stop(gone, adopted))
    missing = stop_payload(adopted, message="no record")
    del missing["cwd"]
    let_go(run_stop(missing, adopted))


@pytest.mark.parametrize("garbage", ["", "not json", "[]", "null", "5", '"text"', "{", '{"agent_type": {"a": 1}}', '{"agent_type": "plumbline:planner", "cwd": 5}'])
def test_garbage_on_stdin_lets_everything_go(run_stop, adopted, garbage):
    let_go(run_stop(garbage, adopted))


def test_a_ledger_that_cannot_be_written_does_not_hold_the_agent_up(run_stop, adopted):
    put(adopted, "plan", spec_record())
    ledger_dir = run_path(adopted, RUN, "ledger.jsonl")
    ledger_dir.mkdir()  # a directory where the ledger file should be
    let_go(stop(run_stop, adopted))


def test_the_hook_never_writes_anything_the_agent_did_not_cause(run_stop, adopted):
    put(adopted, "plan", spec_record())
    before = git(adopted, "status", "--porcelain")
    stop(run_stop, adopted)
    assert git(adopted, "status", "--porcelain") == before  # .plumbline is ignored, nothing else is touched


# --- the parsing of the record line, in process


@pytest.mark.parametrize(
    "text,path",
    [
        ("done\nRECORD: a/b.json", "a/b.json"),
        ("RECORD: a/b.json\n\n\n", "a/b.json"),
        ("**RECORD:** `a/b.json`", "a/b.json"),
        ("RECORD: <a/b.json>", "a/b.json"),
        ('RECORD: "a b/c.json"', "a b/c.json"),
        ("RECORD: a/b.json\nmore", None),
        ("record: a/b.json", None),
        ("RECORD:", None),
        ("", None),
        (None, None),
    ],
)
def test_record_line(text, path):
    assert subagent_stop.record_line(text) == path
