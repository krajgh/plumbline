"""The SubagentStop hook, fed the payloads Claude Code sends: it lets an agent go when its record validates and sits where that
agent's record belongs, blocks it (exit 2, the errors on stderr, the decision as JSON) until then, and gives up after 3 blocks."""
import json
import os
from pathlib import Path

import pytest

import plumbline as pl
import subagent_stop
from helpers import git, write
from hookdata import AGENT, stop_payload
from rundata import (
    RUN, adopt, begin, build_note_record, handback_record, ledger, put as put_file, refusal_record, run_path, spec_record, text_record, verify_record,
    write_transcript, written_tests_record,
)
from samples import sample

PLAN_LINE = "Planned it.\nRECORD: .plumbline/runs/r1/plan.json"

# the record each plumbline agent must end with, written out here as the oracle
RECORD_OF = {
    "planner": "spec", "test-writer": "tests_record", "builder": "build_note", "verifier": "verify_record",
    "prosecutor": "findings_record", "defender": "defense_record", "detective": "gaps_record", "canary": "findings_record",
}


def put(repo, stage_id, data, run_id=RUN):
    """The agent's file as it is when it stops: the ledger holds nothing of it yet (the hook enters it)."""
    return put_file(repo, stage_id, data, run_id, agent=False)


def agents_entered(repo, run_id=RUN):
    return [e for e in ledger(repo, run_id) if e["kind"] == "agent"]


@pytest.fixture
def adopted(repo):
    """Adopted, with the run r1 in progress: row code.M, whose stages have every agent's role (plan, tests, test-review, build, verify, review)."""
    adopt(repo)
    begin(repo, "code.M")
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
    path = put(adopted, "plan", spec_record())
    payload = stop_payload(adopted, message=PLAN_LINE)
    let_go(run_stop(payload, adopted))
    [entry] = agents_entered(adopted)
    assert entry["kind"] == "agent" and entry["at"].endswith("Z")
    assert (entry["agent_id"], entry["agent_type"], entry["stage"], entry["record"]) == (payload["agent_id"], "plumbline:planner", "plan", ".plumbline/runs/r1/plan.json")
    assert (entry["record_type"], entry["valid"], entry["blocks"]) == ("spec", True, 0)
    assert entry["record_sha256"] == pl.file_sha256(path)  # the record as it was when the agent stopped
    assert entry["session_id"] == payload["session_id"]
    assert entry["transcript"] == payload["agent_transcript_path"]
    assert entry["session_transcript"] == payload["transcript_path"]
    assert "errors" not in entry


def test_the_ledger_holds_the_hash_of_the_record_at_the_moment_the_agent_stopped(run_stop, adopted):  # PF-FORGE, SS-HANDFIX
    path = put(adopted, "plan", spec_record())
    let_go(stop(run_stop, adopted))
    [entry] = agents_entered(adopted)
    edited = spec_record()
    edited["goal"] = "A goal written afterwards."
    path.write_text(json.dumps(edited), encoding="utf-8")
    assert pl.file_sha256(path) != entry["record_sha256"]


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
    [entry] = agents_entered(adopted)
    assert (entry["stage"], entry["record_type"], entry["valid"]) == (stage, record_type, True)
    # the record of another agent's type does not do
    other = "plumbline:builder" if name != "builder" else "plumbline:planner"
    assert blocked(stop(run_stop, adopted, message, agent_type=other, agent_id="someone-else"))


def test_the_last_line_may_dress_the_label_and_the_path(run_stop, adopted):
    put(adopted, "plan", spec_record())
    lines = ("**RECORD:** .plumbline/runs/r1/plan.json", "RECORD: `.plumbline/runs/r1/plan.json`", "RECORD:.plumbline/runs/r1/plan.json", "  RECORD:   .plumbline/runs/r1/plan.json  ")
    for n, line in enumerate(lines):
        let_go(stop(run_stop, adopted, f"Done.\n\n{line}\n\n", agent_id=f"agent-{n}"))  # one agent each: one agent's repeated stops are one entry
    assert len(agents_entered(adopted)) == 4


def test_a_closing_code_fence_after_the_record_line_does_not_hide_it(run_stop, adopted):  # C-18
    put(adopted, "plan", spec_record())
    for n, message in enumerate((
        "Done.\n```\nRECORD: .plumbline/runs/r1/plan.json\n```",
        "Done.\n\n```text\nRECORD: .plumbline/runs/r1/plan.json\n```\n\n",
        "Done.\n~~~\nRECORD: .plumbline/runs/r1/plan.json\n~~~",
    )):
        let_go(stop(run_stop, adopted, message, agent_id=f"agent-{n}"))
    assert len(agents_entered(adopted)) == 3


def test_a_relative_path_is_resolved_from_the_cwd_and_then_from_the_root(run_stop, adopted):
    put(adopted, "plan", spec_record())
    src = adopted / "src"
    payload = stop_payload(adopted, message="RECORD: .plumbline/runs/r1/plan.json")
    payload["cwd"] = str(src)  # the agent works in a subdirectory: the path is found from the root
    let_go(run_stop(payload, src))
    absolute = f"RECORD: {run_path(adopted, RUN, 'plan.json')}"
    let_go(run_stop(stop_payload(adopted, message=absolute, agent_id="another-agent"), adopted))
    assert [e["record"] for e in agents_entered(adopted)] == [".plumbline/runs/r1/plan.json"] * 2


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
    assert agents_entered(adopted) == []  # nothing is logged until the agent is let go


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
    assert agents_entered(adopted) == []


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


# --- the record belongs to the run in progress, at the path of a stage its agent serves


def test_a_record_of_another_run_blocks_and_names_the_run_in_progress(run_stop, adopted):  # SS-XRUN, C-29
    begin(adopted, "code.M", "other")
    put(adopted, "plan", spec_record(), "other")
    pl.write_active(adopted, RUN)  # r1 was started by `plan --intent`, and `other` is another run
    reason = blocked(stop(run_stop, adopted, "Done.\nRECORD: .plumbline/runs/other/plan.json"))
    assert "the record must be in the run in progress, 'r1' (you named run 'other')" in reason and "write it under .plumbline/runs/r1/" in reason
    assert "is a valid spec, but it is not where this agent's record belongs" in reason


def test_a_stop_that_names_another_runs_record_is_entered_in_the_run_in_progress_after_three_blocks(run_stop, adopted):
    begin(adopted, "code.M", "other")
    put(adopted, "plan", spec_record(), "other")
    pl.write_active(adopted, RUN)
    message = "Done.\nRECORD: .plumbline/runs/other/plan.json"
    for attempt in (1, 2, 3):
        blocked(stop(run_stop, adopted, message, active=attempt > 1))
    let_go(stop(run_stop, adopted, message, active=True))
    assert agents_entered(adopted, "other") == []  # the other run is not credited with the stop
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["stage"], entry["blocks"]) == (False, None, 3) and entry["record"] == ".plumbline/runs/other/plan.json"
    assert any("the record must be in the run in progress" in e for e in entry["errors"])


def test_the_run_in_progress_is_the_one_the_active_file_names_not_the_newest(run_stop, adopted):
    begin(adopted, "code.M", "newer")
    put(adopted, "plan", spec_record(), "newer")
    pl.write_active(adopted, RUN)  # r1 was started by `plan --intent`; the newer run is a stray
    put(adopted, "plan", spec_record())
    assert "in the run in progress, 'r1'" in blocked(stop(run_stop, adopted, "RECORD: .plumbline/runs/newer/plan.json", agent_id="a"))
    let_go(stop(run_stop, adopted, PLAN_LINE, agent_id="b"))
    assert [e["stage"] for e in agents_entered(adopted)] == ["plan"]


def test_without_an_active_file_the_newest_run_is_in_progress(run_stop, adopted):
    begin(adopted, "code.M", "newer")
    put(adopted, "plan", spec_record(), "newer")
    for path in run_path(adopted, "r1").iterdir():
        os.utime(path, (1_000_000_000, 1_000_000_000))
    os.utime(run_path(adopted, "r1"), (1_000_000_000, 1_000_000_000))
    let_go(stop(run_stop, adopted, "RECORD: .plumbline/runs/newer/plan.json"))
    assert [e["stage"] for e in agents_entered(adopted, "newer")] == ["plan"]


def test_an_agent_names_a_record_of_its_own_stage_and_no_other(run_stop, adopted):  # SS-XRUN
    put(adopted, "build", build_note_record())
    put(adopted, "plan", spec_record())
    reason = blocked(stop(run_stop, adopted, "RECORD: .plumbline/runs/r1/build.json", agent_type="plumbline:planner"))
    assert "a planner writes its record at .plumbline/runs/r1/plan.json, not at .plumbline/runs/r1/build.json" in reason
    let_go(stop(run_stop, adopted, "RECORD: .plumbline/runs/r1/build.json", agent_type="plumbline:builder", agent_id="b"))
    let_go(stop(run_stop, adopted, PLAN_LINE, agent_type="plumbline:planner", agent_id="p"))


def test_a_stage_the_run_does_not_have_is_no_place_for_a_record(run_stop, repo):
    adopt(repo)
    begin(repo, "docs")  # the docs row has no plan stage
    put(repo, "plan", spec_record())
    reason = blocked(stop(run_stop, repo))
    assert "a planner writes its record at a stage of the run that its role serves (this run has none), not at .plumbline/runs/r1/plan.json" in reason


def test_a_review_roles_record_sits_in_a_round_of_a_review_stage(run_stop, adopted):
    part = sample("findings_record")
    for path in ("review/prosecutor-correctness.json", "plan/round-1/prosecutor-correctness.json", "review/round-x/prosecutor-correctness.json", "review/round-1/deeper/prosecutor-correctness.json"):
        target = run_path(adopted, RUN, *path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(part), encoding="utf-8")
        assert "a prosecutor" in blocked(stop(run_stop, adopted, f"RECORD: .plumbline/runs/r1/{path}", agent_type="plumbline:prosecutor", agent_id=path))
    for path in ("review/round-2/prosecutor-data.json", "test-review/round-1/prosecutor-tests.json"):
        target = run_path(adopted, RUN, *path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(part), encoding="utf-8")
        let_go(stop(run_stop, adopted, f"RECORD: .plumbline/runs/r1/{path}", agent_type="plumbline:prosecutor", agent_id=path))


def test_a_review_role_cannot_write_the_record_of_an_agent_stage_nor_the_reverse(run_stop, adopted):
    put(adopted, "plan", spec_record())
    assert "a prosecutor" in blocked(stop(run_stop, adopted, PLAN_LINE, agent_type="plumbline:prosecutor", agent_id="x"))
    part = run_path(adopted, RUN, "review", "round-1", "prosecutor-correctness.json")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_text(json.dumps(sample("findings_record")), encoding="utf-8")
    assert "a planner writes its record at" in blocked(stop(run_stop, adopted, "RECORD: .plumbline/runs/r1/review/round-1/prosecutor-correctness.json", agent_type="plumbline:planner", agent_id="y"))


def test_a_run_whose_intake_record_is_unusable_lets_the_agent_go_and_marks_the_record_invalid(run_stop, repo):
    adopt(repo)
    put(repo, "plan", spec_record())  # the run has no intake record: the agent cannot mend that
    let_go(stop(run_stop, repo))
    [entry] = agents_entered(repo)
    assert entry["valid"] is False and any("the place of the record in the run was not checked" in e for e in entry["errors"])
    assert entry["record_sha256"] == pl.file_sha256(run_path(repo, RUN, "plan.json"))


def test_an_invalid_pipeline_configuration_does_not_hold_the_agent_up_either(run_stop, adopted):
    put(adopted, "plan", spec_record())
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "nope"\n')
    let_go(stop(run_stop, adopted))
    [entry] = agents_entered(adopted)
    assert entry["valid"] is False and any("the pipeline configuration is invalid" in e for e in entry["errors"])


def test_when_no_run_is_in_progress_the_agent_is_told_how_to_start_one(run_stop, repo):
    adopt(repo)
    reason = blocked(stop(run_stop, repo, "Done.\nRECORD: .plumbline/runs/ghost/plan.json"))
    assert "no run is in progress: `plumbline.py plan --intent ID` starts one" in reason


# --- three blocks, then the agent is let go and the record marked invalid


def test_after_three_blocks_the_agent_is_let_go_and_the_record_is_marked_invalid(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    for attempt in (1, 2, 3):
        reason = blocked(stop(run_stop, adopted, active=attempt > 1))
        assert f"(attempt {attempt} of 3" in reason
    let_go(stop(run_stop, adopted, active=True))  # the fourth stop
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["blocks"], entry["stage"], entry["record"]) == (False, 3, "plan", ".plumbline/runs/r1/plan.json")
    assert entry["record_type"] == "spec" and any("$.goal" in e or "missing required key" in e for e in entry["errors"])
    assert not (adopted / ".plumbline" / "blocks" / AGENT).exists()  # the count is cleared


def test_an_agent_that_never_names_a_record_is_let_go_after_three_blocks_with_an_invalid_entry_and_no_stage(run_stop, adopted):
    for attempt in (1, 2, 3):
        blocked(stop(run_stop, adopted, "no record here", active=attempt > 1))
    let_go(stop(run_stop, adopted, "no record here", active=True))
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["stage"], entry["record"], entry["blocks"]) == (False, None, None, 3)
    assert entry["errors"] == ["no RECORD line at the end of the final reply"]


def test_fixing_the_record_after_a_block_lets_the_agent_go_with_a_valid_entry(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    blocked(stop(run_stop, adopted))
    put(adopted, "plan", spec_record())
    let_go(stop(run_stop, adopted, active=True))
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["blocks"]) == (True, 1)
    assert not (adopted / ".plumbline" / "blocks" / AGENT).exists()


def test_the_count_is_kept_whatever_stop_hook_active_says(run_stop, adopted):  # SS-LOOP
    put(adopted, "plan", {"goal": ""})
    attempts = [blocked(stop(run_stop, adopted, active=False)) for _ in range(3)]  # stops each reported as a fresh one: they still add up
    for number, reason in enumerate(attempts, 1):
        assert f"(attempt {number} of 3" in reason
    let_go(stop(run_stop, adopted, active=False))  # the fourth stop is let go, though it says it is not a continuation
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["blocks"]) == (False, 3)


def test_an_agent_that_stops_six_times_without_fixing_its_record_is_let_go_once_and_not_blocked_forever(run_stop, adopted):  # SS-LOOP
    put(adopted, "plan", {"goal": ""})
    outcomes = []
    for _ in range(6):
        result = stop(run_stop, adopted, active=False)
        outcomes.append("blocked" if result.returncode == 2 else "let go")
    assert outcomes[:4] == ["blocked", "blocked", "blocked", "let go"]


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
    ["Explore", "general-purpose", "probe:echo", "someone:builder", "plumbline", "", None, 5,
     "plumbline_builder", "Plumbline:builder", "xplumbline:builder", " plumbline:builder"],  # look-alikes of a plumbline agent
)
def test_agents_that_are_not_plumbline_agents_are_ignored(run_stop, adopted, agent_type):
    put(adopted, "plan", {"goal": ""})  # even an invalid record would not matter
    payload = stop_payload(adopted, message=PLAN_LINE)
    payload["agent_type"] = agent_type
    let_go(run_stop(payload, adopted))
    assert agents_entered(adopted) == [] and not (adopted / ".plumbline" / "blocks").exists()


@pytest.mark.parametrize("agent_type", ["plumbline:wizard", "plumbline:", "plumbline:general", "plumbline:Planner"])
def test_an_unknown_plumbline_role_is_let_go_and_entered_as_invalid(run_stop, adopted, agent_type):  # SS-UNKNOWN
    let_go(stop(run_stop, adopted, "no record needed", agent_type=agent_type))
    [entry] = agents_entered(adopted)
    assert (entry["agent_type"], entry["valid"], entry["stage"], entry["record"], entry["record_sha256"]) == (agent_type, False, None, None, None)
    assert entry["errors"] == ["unknown plumbline role"]
    assert entry["agent_id"] == AGENT and entry["transcript"] and not (adopted / ".plumbline" / "blocks").exists()


def test_an_unknown_plumbline_role_where_no_run_has_begun_leaves_no_trace(run_stop, repo):
    adopt(repo)
    let_go(stop(run_stop, repo, "no record needed", agent_type="plumbline:wizard"))
    assert not (repo / ".plumbline").exists()


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
    run_path(adopted, RUN, "ledger.jsonl").unlink()
    run_path(adopted, RUN, "ledger.jsonl").mkdir()  # a directory where the ledger file should be
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
        ("```\nRECORD: a/b.json\n```", "a/b.json"),
        ("```text\nRECORD: a/b.json\n```\n", "a/b.json"),
        ("~~~\nRECORD: a/b.json\n~~~", "a/b.json"),
        ("RECORD: a/b.json\n```\n```", "a/b.json"),
        ("```", None),
        ("RECORD: a/b.json\nmore", None),
        ("RECORD: a/b.json\n```\nmore\n```", None),
        ("record: a/b.json", None),
        ("RECORD:", None),
        ("", None),
        (None, None),
    ],
)
def test_record_line(text, path):
    assert subagent_stop.record_line(text) == path


# --- one entry for each agent and record: an agent the harness asks again for its report stops again, and is still one agent


def test_four_stops_of_one_agent_with_the_same_valid_record_are_one_entry_and_one_round_and_the_plan_gate_passes(run_cli, run_stop, adopted):
    put(adopted, "plan", spec_record())
    for _ in range(4):  # the first real run: the planner stopped four times with the same plan.json
        let_go(stop(run_stop, adopted))
    [entry] = agents_entered(adopted)
    assert (entry["agent_id"], entry["valid"], entry["record"], entry["stage"]) == (AGENT, True, ".plumbline/runs/r1/plan.json", "plan")
    assert entry["record_sha256"] == pl.file_sha256(run_path(adopted, RUN, "plan.json"))
    assert pl.stage_round({"id": "plan", "role": "planner", "max_rounds": 2}, None, ledger(adopted)) == (1, 2)
    result = run_cli("gate", RUN, "plan", cwd=adopted)
    assert result.returncode == 0, result.stdout
    assert "gate spec_complete for stage 'plan': pass" in result.stdout and "round 1 of 2" in result.stdout


def test_each_agent_is_entered_for_its_own_stops_and_counts_as_a_round(run_stop, adopted):
    put(adopted, "plan", spec_record())
    for agent_id in ("first", "second", "first", "second", "first"):  # the ledger's latest entry is the other agent's: the agent's own entry is the one that counts
        let_go(stop(run_stop, adopted, agent_id=agent_id))
    assert [e["agent_id"] for e in agents_entered(adopted)] == ["first", "second"]
    assert pl.stage_round({"id": "plan", "role": "planner", "max_rounds": 2}, None, ledger(adopted)) == (2, 2)


def test_a_later_stop_that_changes_the_record_or_its_validity_is_entered_again(run_stop, adopted):
    path = put(adopted, "plan", spec_record())
    let_go(stop(run_stop, adopted))
    let_go(stop(run_stop, adopted))
    assert len(agents_entered(adopted)) == 1
    edited = spec_record()
    edited["goal"] = "A goal written after the first stop."
    path.write_text(json.dumps(edited), encoding="utf-8")
    let_go(stop(run_stop, adopted))  # the record changed: the agent left another record
    let_go(stop(run_stop, adopted))
    first, second = agents_entered(adopted)
    assert first["record_sha256"] != second["record_sha256"] == pl.file_sha256(path) and (first["valid"], second["valid"]) == (True, True)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "nope"\n')  # the record is as it was, and no longer checked: its validity changes
    let_go(stop(run_stop, adopted))
    let_go(stop(run_stop, adopted))
    *_, last = agents_entered(adopted)
    assert len(agents_entered(adopted)) == 3 and (last["valid"], last["record_sha256"]) == (False, second["record_sha256"])
    assert {e["agent_id"] for e in agents_entered(adopted)} == {AGENT}  # three entries of one agent are one round
    assert pl.stage_agents(ledger(adopted), "plan") == 1


def test_a_stop_that_names_the_same_content_at_another_path_is_entered_again(run_stop, adopted):
    part = json.dumps(sample("findings_record"))
    for name in ("prosecutor-correctness", "prosecutor-tests"):  # one agent, two valid records with the same hash: two paths
        target = run_path(adopted, RUN, "review", "round-1", f"{name}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(part, encoding="utf-8")
        let_go(stop(run_stop, adopted, f"RECORD: .plumbline/runs/r1/review/round-1/{name}.json", agent_type="plumbline:prosecutor", agent_id="pro"))
    first, second = agents_entered(adopted)
    assert first["record_sha256"] == second["record_sha256"] and (first["valid"], second["valid"]) == (True, True)
    assert [first["record"], second["record"]] == [f".plumbline/runs/r1/review/round-1/{name}.json" for name in ("prosecutor-correctness", "prosecutor-tests")]


def test_stops_that_name_no_agent_are_entered_each_time_and_count_as_a_round_each(run_stop, adopted):
    put(adopted, "plan", spec_record())
    for _ in range(2):
        let_go(stop(run_stop, adopted, agent_id=None))
    assert [e["agent_id"] for e in agents_entered(adopted)] == [None, None]
    assert pl.stage_round({"id": "plan", "role": "planner", "max_rounds": 3}, None, ledger(adopted)) == (2, 3)


def test_an_agent_let_go_with_an_invalid_record_that_stops_again_stays_one_entry(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    outcomes = ["blocked" if stop(run_stop, adopted).returncode == 2 else "let go" for _ in range(8)]
    assert outcomes == ["blocked"] * 3 + ["let go"] + ["blocked"] * 3 + ["let go"]  # the count of blocks starts again once the agent is let go
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["blocks"]) == (False, 3)


def test_an_unknown_plumbline_role_that_stops_again_is_entered_once(run_stop, adopted):
    for _ in range(3):
        let_go(stop(run_stop, adopted, "no record needed", agent_type="plumbline:wizard"))
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["errors"]) == (False, ["unknown plumbline role"])


# --- a report handed back through SubagentHandback: when the final message has no RECORD line, the last handback call has it

HANDBACK_REFUSED = "Only the auto-mode classifier can allow SubagentHandback: the session is not in auto mode"


@pytest.fixture
def stop_with(run_stop, adopted, tmp_path):
    """Stop an agent whose transcript holds `records` (a list of dicts, or of lines that are written as they are)."""

    def run(records, message="", **fields):
        payload = stop_payload(adopted, message=message, **fields)
        payload["agent_transcript_path"] = str(tmp_path / "transcripts" / f"agent-{payload['agent_id']}.jsonl")
        write_transcript(Path(payload["agent_transcript_path"]), [r for r in records if isinstance(r, dict)], [r for r in records if not isinstance(r, dict)])
        return run_stop(payload, adopted)

    return run


@pytest.mark.parametrize("final", ["", "   \n", "Done: the report went through SubagentHandback."])
def test_a_report_that_went_only_through_the_handback_names_its_record_from_the_transcript(stop_with, adopted, final):
    put(adopted, "plan", spec_record())
    let_go(stop_with([text_record("Working on it."), handback_record(PLAN_LINE)], message=final))
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["record"], entry["stage"], entry["blocks"]) == (True, ".plumbline/runs/r1/plan.json", "plan", 0)
    assert entry["record_sha256"] == pl.file_sha256(run_path(adopted, RUN, "plan.json"))


def test_a_payload_without_a_final_message_field_falls_back_to_the_handback_too(run_stop, adopted, tmp_path):
    put(adopted, "plan", spec_record())
    payload = stop_payload(adopted)
    del payload["last_assistant_message"]
    payload["agent_transcript_path"] = str(write_transcript(tmp_path / "t" / "agent.jsonl", [handback_record(PLAN_LINE)]))
    let_go(run_stop(payload, adopted))
    assert [e["valid"] for e in agents_entered(adopted)] == [True]


@pytest.mark.parametrize(
    "report",
    [
        "Planned it.\nRECORD: .plumbline/runs/r1/plan.json",
        "Planned it.\n\n**RECORD:** `.plumbline/runs/r1/plan.json`\n\n",
        "Planned it.\n```\nRECORD: .plumbline/runs/r1/plan.json\n```",
    ],
)
def test_the_record_line_of_a_handback_message_is_read_like_the_one_of_a_final_message(stop_with, adopted, report):
    put(adopted, "plan", spec_record())
    let_go(stop_with([handback_record(report)]))
    assert [e["valid"] for e in agents_entered(adopted)] == [True]


def test_a_refused_handback_followed_by_a_text_report_is_read_from_the_final_message(stop_with, adopted):
    put(adopted, "plan", spec_record())
    named_elsewhere = "Planned it.\nRECORD: .plumbline/runs/r1/build.json"  # the handback names a place no planner writes: the final message comes first
    records = [handback_record(named_elsewhere), refusal_record(text=HANDBACK_REFUSED), text_record(PLAN_LINE)]
    let_go(stop_with(records, message=PLAN_LINE))
    [entry] = agents_entered(adopted)
    assert (entry["valid"], entry["record"]) == (True, ".plumbline/runs/r1/plan.json")


def test_a_refused_handback_followed_by_the_same_report_as_text_is_let_go_once(stop_with, adopted):
    put(adopted, "plan", spec_record())
    records = [handback_record(PLAN_LINE), refusal_record(text=HANDBACK_REFUSED), text_record(PLAN_LINE)]
    for _ in range(4):
        let_go(stop_with(records, message=PLAN_LINE))
    assert len(agents_entered(adopted)) == 1


@pytest.mark.parametrize("final", [PLAN_LINE, ""], ids=["a text report ends each stop", "the handback calls are all there is"])
def test_the_real_sequence_a_refused_handback_four_stops_one_entry_round_1_and_a_plan_gate_that_passes(stop_with, run_cli, adopted, final):
    put(adopted, "plan", spec_record())
    nudge = {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "[handback-send-enforce] Your report has not been delivered."}}
    records = []
    for call in range(1, 5):  # the agent called SubagentHandback and was refused; the runtime nudged it; it stopped again
        records += [handback_record(PLAN_LINE, f"toolu_{call}"), refusal_record(f"toolu_{call}", HANDBACK_REFUSED), nudge]
        let_go(stop_with(records, message=final))
    [entry] = agents_entered(adopted)
    assert entry["valid"] is True and entry["record"] == ".plumbline/runs/r1/plan.json"
    result = run_cli("gate", RUN, "plan", cwd=adopted)
    assert result.returncode == 0 and "round 1 of 2" in result.stdout, result.stdout


def test_only_the_last_handback_call_counts(stop_with, adopted):
    put(adopted, "plan", spec_record())
    wrong = handback_record("Planned it.\nRECORD: .plumbline/runs/r1/build.json", "toolu_1")
    right = handback_record(PLAN_LINE, "toolu_2")
    assert "a planner writes its record at" in blocked(stop_with([right, wrong], agent_id="right-then-wrong"))
    let_go(stop_with([wrong, refusal_record("toolu_1"), right], agent_id="wrong-then-right"))
    assert [e["agent_id"] for e in agents_entered(adopted)] == ["wrong-then-right"]
    both = {**right, "message": {**right["message"], "content": [*wrong["message"]["content"], *right["message"]["content"]]}}  # two calls in one message
    let_go(stop_with([both], agent_id="two-in-one"))
    assert [e["agent_id"] for e in agents_entered(adopted)] == ["wrong-then-right", "two-in-one"]


def test_the_handback_is_a_fallback_only_a_line_in_the_final_message_that_is_wrong_is_not_mended_by_it(stop_with, adopted):
    put(adopted, "plan", spec_record())
    reason = blocked(stop_with([handback_record(PLAN_LINE)], message="Done.\nRECORD: .plumbline/runs/r1/missing.json"))
    assert "missing.json: no such file" in reason
    assert agents_entered(adopted) == []


def test_a_handback_without_a_record_line_blocks_and_the_message_says_where_the_line_goes(stop_with, adopted):
    put(adopted, "plan", spec_record())
    reason = blocked(stop_with([handback_record("Planned it, the spec is written.")], message="Done."))
    assert "your report must end with a line of the form `RECORD: <path>`" in reason
    assert "in the message of your SubagentHandback call" in reason and "in your final message when that call is refused" in reason
    assert "not only of a hand-back report" not in reason
    assert agents_entered(adopted) == []


def test_the_other_block_messages_ask_for_the_line_at_the_end_of_the_report(run_stop, adopted):
    put(adopted, "plan", {"goal": ""})
    assert "end your report with the line `RECORD: <path>` again" in blocked(stop(run_stop, adopted, agent_id="invalid"))
    assert "end your report with the line `RECORD: <path>` again" in blocked(stop(run_stop, adopted, "RECORD: README.md", agent_id="unusable"))
    put(adopted, "build", build_note_record())
    assert "end your report with the line `RECORD: <path>` again" in blocked(stop(run_stop, adopted, "RECORD: .plumbline/runs/r1/build.json", agent_id="misplaced"))


def test_a_handback_that_is_far_from_the_end_of_a_big_transcript_is_still_found_by_reading_from_the_end(stop_with, adopted):
    put(adopted, "plan", spec_record())
    filler = [text_record("x" * 4000) for _ in range(700)]  # about 2.8 MB, more than is read
    let_go(stop_with([*filler, handback_record(PLAN_LINE), refusal_record()], agent_id="tail"))
    assert [e["agent_id"] for e in agents_entered(adopted)] == ["tail"]


def test_a_handback_before_more_than_the_cap_is_left_unread_and_the_stop_is_blocked(stop_with, adopted):
    put(adopted, "plan", spec_record())
    filler = [text_record("x" * 4000) for _ in range(700)]
    assert "must end with a line of the form" in blocked(stop_with([handback_record(PLAN_LINE), *filler], agent_id="buried"))


# --- reading the transcript defensively, in process


def lines_of(tmp_path, *lines, name="agent.jsonl", raw=None):
    path = tmp_path / name
    path.write_bytes(raw if raw is not None else "".join(line if isinstance(line, str) else json.dumps(line) for line in lines).encode("utf-8"))
    return path


def as_lines(*records):
    return [(record if isinstance(record, str) else json.dumps(record)) + "\n" for record in records]


GOOD = "Done.\nRECORD: a/b.json"


def test_a_transcript_that_is_no_readable_file_names_nothing(tmp_path):
    (tmp_path / "a-directory").mkdir()
    for named in (None, 5, "", [], str(tmp_path / "missing.jsonl"), str(tmp_path / "a-directory"), "nul\0byte"):
        assert subagent_stop.handback_record_line(named) is None, named
        assert subagent_stop.transcript_tail(named) == [], named


def test_the_message_of_the_last_call_is_what_is_read_and_a_last_call_without_a_message_names_nothing(tmp_path):
    good = handback_record(GOOD)
    for last in ({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SubagentHandback", "input": {}}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SubagentHandback", "input": {"message": ["RECORD: a/b.json"]}}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SubagentHandback", "input": "RECORD: a/b.json"}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SubagentHandback"}]}}):
        path = lines_of(tmp_path, *as_lines(good, last))
        assert subagent_stop.handback_record_line(path) is None  # the earlier call is not the last one
    assert subagent_stop.handback_record_line(lines_of(tmp_path, *as_lines(handback_record("no line"), good))) == "a/b.json"


def test_only_a_tool_use_of_subagent_handback_counts(tmp_path):
    other_tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"message": GOOD}}]}}
    look_alike = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SubagentHandbackLater", "input": {"message": GOOD}}]}}
    as_text = {"type": "assistant", "message": {"content": [{"type": "text", "text": f"SubagentHandback message: {GOOD}"}]}}
    as_result = {"type": "user", "message": {"content": [{"type": "tool_result", "name": "SubagentHandback", "content": GOOD}]}}
    mention = {"type": "user", "message": {"role": "user", "content": f"[handback-send-enforce] call SubagentHandback: {GOOD}"}}
    for record in (other_tool, look_alike, as_text, as_result, mention):
        assert subagent_stop.handback_record_line(lines_of(tmp_path, *as_lines(record))) is None, record
    assert subagent_stop.handback_record_line(lines_of(tmp_path, *as_lines(handback_record(GOOD), other_tool, look_alike, as_text, as_result, mention))) == "a/b.json"


def test_a_call_is_found_wherever_its_record_keeps_the_content(tmp_path):
    block = {"type": "tool_use", "id": "t", "name": "SubagentHandback", "input": {"message": GOOD}}
    shapes = [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hm"}, block]}},
        {"type": "assistant", "content": [block]},  # no wrapping message
        {"message": {"content": [block]}},  # no type
    ]
    for record in shapes:
        assert subagent_stop.handback_record_line(lines_of(tmp_path, *as_lines(record))) == "a/b.json", record
    for record in ({"message": {"content": "SubagentHandback"}}, {"message": "SubagentHandback"}, {"content": [None, 5, "SubagentHandback", [block]]}, [block], "SubagentHandback"):
        assert subagent_stop.handback_record_line(lines_of(tmp_path, *as_lines(record))) is None, record


def test_torn_blank_and_undecodable_lines_are_skipped_not_fatal(tmp_path):
    deep = '{"x": ' + "[" * 60000 + '"SubagentHandback"' + "]" * 60000 + "}"  # deeper than json.loads follows: it raises RecursionError
    body = "\n".join(["{torn SubagentHandback", "", "   ", deep, json.dumps(handback_record(GOOD)), "not json SubagentHandback", '{"a": 1}', "[1, 2]", "null"]) + "\n"
    assert subagent_stop.handback_record_line(lines_of(tmp_path, raw=body.encode("utf-8"))) == "a/b.json"
    undecodable = b"\xff\xfe SubagentHandback \x80\n" + json.dumps(handback_record(GOOD)).encode("utf-8") + b"\n\xc3\n"
    assert subagent_stop.handback_record_line(lines_of(tmp_path, raw=undecodable)) == "a/b.json"
    assert subagent_stop.handback_record_line(lines_of(tmp_path, raw=b"")) is None
    assert subagent_stop.handback_record_line(lines_of(tmp_path, raw=b"\n\n")) is None


def test_only_the_last_bytes_up_to_the_cap_are_read(tmp_path, monkeypatch):
    last = json.dumps(handback_record(GOOD)) + "\n"
    early = json.dumps(handback_record("early\nRECORD: early.json")) + "\n"
    filler = json.dumps(text_record("f" * 300)) + "\n"
    size = len(last.encode("utf-8"))
    path = lines_of(tmp_path, early, filler, filler, last)
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", size)  # the last line, exactly: it begins where the window does
    assert subagent_stop.handback_record_line(path) == "a/b.json"
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", size - 1)  # one byte short: the window begins inside the last line, which is torn
    assert subagent_stop.handback_record_line(path) is None
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", size + 10)  # the window begins inside the line before it
    assert subagent_stop.handback_record_line(path) == "a/b.json"
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", 3 * size)
    assert subagent_stop.handback_record_line(path) == "a/b.json"
    only_early = lines_of(tmp_path, early, filler, filler, filler, name="early.jsonl")
    assert subagent_stop.handback_record_line(only_early) is None  # the early call is beyond the window ...
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", 1 << 20)
    assert subagent_stop.handback_record_line(only_early) == "early.json"  # ... and inside a bigger one
    assert subagent_stop.handback_record_line(lines_of(tmp_path, last.rstrip("\n"), name="no-newline.jsonl")) == "a/b.json"
    assert subagent_stop.transcript_tail(lines_of(tmp_path, "a\nb\nc\n", name="small.jsonl")) == ["a", "b", "c", ""]  # a file inside the cap is read whole


def test_a_line_the_cut_tears_is_left_out_though_its_tail_is_a_record(tmp_path, monkeypatch):
    whole = json.dumps(handback_record("torn\nRECORD: torn.json"))
    torn = "x" * 40 + whole + "\n"  # no record as it stands; its tail, from the 41st byte, is one
    after = json.dumps(text_record("after")) + "\n"
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", len(whole) + 1 + len(after))  # the window begins exactly at the tail of `torn`
    path = lines_of(tmp_path, "y\n", torn, after)
    assert [line for line in subagent_stop.transcript_tail(path) if "torn.json" in line] == []
    assert subagent_stop.handback_record_line(path) is None
    monkeypatch.setattr(subagent_stop, "MAX_TRANSCRIPT_BYTES", len(whole) + 1 + len(after) + 41)  # the whole of `torn` is in it, and is no JSON
    assert subagent_stop.handback_record_line(path) is None
    assert subagent_stop.handback_record_line(lines_of(tmp_path, whole + "\n", after, name="whole.jsonl")) == "torn.json"
