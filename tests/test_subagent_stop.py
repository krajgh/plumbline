"""The SubagentStop hook, fed the payloads Claude Code sends: it lets an agent go when its record validates and sits where that
agent's record belongs, blocks it (exit 2, the errors on stderr, the decision as JSON) until then, and gives up after 3 blocks."""
import json
import os

import pytest

import plumbline as pl
import subagent_stop
from helpers import git, write
from hookdata import AGENT, stop_payload
from rundata import (
    RUN, adopt, begin, build_note_record, ledger, put as put_file, run_path, spec_record, verify_record, written_tests_record,
)
from samples import sample

PLAN_LINE = "Planned it.\nRECORD: .plumbline/runs/r1/plan.json"

# the record each plumbline agent must end with, written out here as the oracle
RECORD_OF = {
    "planner": "spec", "test-writer": "tests_record", "builder": "build_note", "verifier": "verify_record",
    "prosecutor": "findings_record", "defender": "defense_record", "detective": "gaps_record",
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
    for line in ("**RECORD:** .plumbline/runs/r1/plan.json", "RECORD: `.plumbline/runs/r1/plan.json`", "RECORD:.plumbline/runs/r1/plan.json", "  RECORD:   .plumbline/runs/r1/plan.json  "):
        let_go(stop(run_stop, adopted, f"Done.\n\n{line}\n\n"))
    assert len(agents_entered(adopted)) == 4


def test_a_closing_code_fence_after_the_record_line_does_not_hide_it(run_stop, adopted):  # C-18
    put(adopted, "plan", spec_record())
    for message in (
        "Done.\n```\nRECORD: .plumbline/runs/r1/plan.json\n```",
        "Done.\n\n```text\nRECORD: .plumbline/runs/r1/plan.json\n```\n\n",
        "Done.\n~~~\nRECORD: .plumbline/runs/r1/plan.json\n~~~",
    ):
        let_go(stop(run_stop, adopted, message))
    assert len(agents_entered(adopted)) == 3


def test_a_relative_path_is_resolved_from_the_cwd_and_then_from_the_root(run_stop, adopted):
    put(adopted, "plan", spec_record())
    src = adopted / "src"
    payload = stop_payload(adopted, message="RECORD: .plumbline/runs/r1/plan.json")
    payload["cwd"] = str(src)  # the agent works in a subdirectory: the path is found from the root
    let_go(run_stop(payload, src))
    absolute = f"RECORD: {run_path(adopted, RUN, 'plan.json')}"
    let_go(run_stop(stop_payload(adopted, message=absolute), adopted))
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
