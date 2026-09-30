"""tokens: usage per model from the agent transcripts the ledger points to, the maximum per message id."""
import json

import pytest

import plumbline as pl
from rundata import HAIKU, RUN, SONNET, adopt, agent_row, assistant_record, put, run_path, write_ledger, write_transcript

@pytest.fixture
def transcripts(tmp_path):
    """Two agents' transcripts. msg_1 is streamed as three records with growing output; msg_2 is one record;
    the other lines are the kinds of thing a real transcript holds and that must not count."""
    first = write_transcript(
        tmp_path / "t" / "agent-a1.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "do it"}},
            assistant_record("msg_1", SONNET, output=1, inp=10, cache_write=100, cache_read=1000, block="text"),
            assistant_record("msg_1", SONNET, output=25, inp=10, cache_write=100, cache_read=1000, block="tool_use"),
            assistant_record("msg_1", SONNET, output=40, inp=10, cache_write=100, cache_read=1000, block="tool_use"),
            {"type": "attachment", "attachment": {"type": "hook_success"}},
            assistant_record("msg_2", SONNET, output=60, inp=5, cache_write=0, cache_read=2000),
            assistant_record("msg_synthetic", "<synthetic>", output=0),
            {"type": "assistant", "message": {"role": "assistant", "model": SONNET, "usage": {"output_tokens": 999}}},  # no id
        ],
        extra_lines=["{not json", ""],
    )
    second = write_transcript(
        tmp_path / "t" / "agent-b2.jsonl",
        [
            assistant_record("msg_3", HAIKU, output=5, inp=7, cache_write=3, cache_read=50),
            assistant_record("msg_3", HAIKU, output=9, inp=7, cache_write=3, cache_read=50),
            assistant_record("msg_1", SONNET, output=40, inp=10, cache_write=100, cache_read=1000),  # the same message again: not counted twice
        ],
    )
    return first, second


EXPECTED = {
    SONNET: {"output": 100, "fresh_input": 115, "cache_read": 3000},  # msg_1: 40, 10+100, 1000; msg_2: 60, 5, 2000
    HAIKU: {"output": 9, "fresh_input": 10, "cache_read": 50},  # msg_3: 9, 7+3, 50
}


def test_the_maximum_usage_per_message_id_is_summed_per_model(transcripts):
    assert pl.usage_by_model(list(transcripts)) == EXPECTED


def test_a_message_streamed_in_several_records_counts_once(tmp_path):
    path = write_transcript(tmp_path / "a.jsonl", [assistant_record("m", SONNET, output=n, inp=4, cache_write=6, cache_read=8) for n in (1, 2, 3, 30)])
    assert pl.usage_by_model([path]) == {SONNET: {"output": 30, "fresh_input": 10, "cache_read": 8}}


def test_each_counter_takes_its_own_maximum(tmp_path):
    # a later record does not always have the larger of every counter
    path = write_transcript(
        tmp_path / "a.jsonl",
        [assistant_record("m", SONNET, output=50, inp=1, cache_write=0, cache_read=900), assistant_record("m", SONNET, output=2, inp=9, cache_write=5, cache_read=100)],
    )
    assert pl.usage_by_model([path]) == {SONNET: {"output": 50, "fresh_input": 14, "cache_read": 900}}


def test_fresh_input_is_input_plus_cache_writes_and_cache_reads_stand_alone(tmp_path):
    path = write_transcript(tmp_path / "a.jsonl", [assistant_record("m", HAIKU, output=1, inp=7, cache_write=30, cache_read=500)])
    assert pl.usage_by_model([path]) == {HAIKU: {"output": 1, "fresh_input": 37, "cache_read": 500}}


def test_models_are_reported_separately_and_sorted(tmp_path):
    path = write_transcript(tmp_path / "a.jsonl", [assistant_record("m1", SONNET, output=1), assistant_record("m2", HAIKU, output=2)])
    assert list(pl.usage_by_model([path])) == [HAIKU, SONNET]


def test_records_without_usage_or_id_and_synthetic_messages_do_not_count(tmp_path):
    path = write_transcript(
        tmp_path / "a.jsonl",
        [{"type": "user", "message": {"content": "x"}}, assistant_record("s", "<synthetic>", output=5), {"type": "attachment"}],
        extra_lines=["[]", "null", "{not json"],
    )
    assert pl.usage_by_model([path]) == {}


def test_a_transcript_that_cannot_be_read_is_skipped(tmp_path):
    assert pl.usage_by_model([tmp_path / "missing.jsonl"]) == {}


# --- the command, reading the transcripts the ledger points to


@pytest.fixture
def adopted(repo):
    adopt(repo)
    put(repo, "verify", {}, agent=False)  # the run directory
    return repo


def test_tokens_reads_the_transcripts_named_in_the_ledger(run_cli, adopted, transcripts):
    first, second = transcripts
    write_ledger(adopted, [agent_row("a1", transcript=str(first)), agent_row("b2", transcript=str(second))])
    result = run_cli("tokens", RUN, cwd=adopted)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"by_model": EXPECTED}
    assert result.stderr == ""


def test_the_object_it_prints_is_what_the_pass_record_holds(run_cli, adopted, transcripts):
    first, second = transcripts
    write_ledger(adopted, [agent_row("a1", transcript=str(first)), agent_row("b2", transcript=str(second))])
    tokens = json.loads(run_cli("tokens", RUN, cwd=adopted).stdout)
    record = {"commit": "0" * 40, "run_id": RUN, "row": "docs", "stages": [], "tokens": tokens, "verdict": "pass", "notes": []}
    assert pl.check_record("pass_record", record) == []


def test_two_ledger_rows_for_one_agent_read_its_transcript_once(run_cli, adopted, transcripts):
    first, _ = transcripts
    write_ledger(adopted, [agent_row("a1", transcript=str(first)), agent_row("a1", transcript=str(first))])
    assert json.loads(run_cli("tokens", RUN, cwd=adopted).stdout)["by_model"][SONNET]["output"] == 100


def test_without_a_reported_transcript_it_is_derived_from_the_session_transcript(run_cli, adopted, tmp_path):
    sessions = tmp_path / "projects" / "slug"
    write_transcript(sessions / "S1" / "subagents" / "agent-xyz.jsonl", [assistant_record("m", HAIKU, output=8, inp=2)])
    row = agent_row("xyz", session_id="S1", session_transcript=str(sessions / "S1.jsonl"))
    write_ledger(adopted, [row])
    result = run_cli("tokens", RUN, cwd=adopted)
    assert json.loads(result.stdout) == {"by_model": {HAIKU: {"output": 8, "fresh_input": 2, "cache_read": 0}}}


def test_the_reported_transcript_wins_over_the_derived_one(run_cli, adopted, tmp_path):
    sessions = tmp_path / "projects" / "slug"
    write_transcript(sessions / "S1" / "subagents" / "agent-xyz.jsonl", [assistant_record("derived", HAIKU, output=1)])
    reported = write_transcript(tmp_path / "reported.jsonl", [assistant_record("reported", HAIKU, output=7)])
    write_ledger(adopted, [agent_row("xyz", transcript=str(reported), session_id="S1", session_transcript=str(sessions / "S1.jsonl"))])
    assert json.loads(run_cli("tokens", RUN, cwd=adopted).stdout)["by_model"][HAIKU]["output"] == 7


def test_a_transcript_that_cannot_be_found_is_a_note_not_a_failure(run_cli, adopted, transcripts):
    first, _ = transcripts
    write_ledger(adopted, [agent_row("a1", transcript=str(first)), agent_row("gone", transcript="/nowhere/agent-gone.jsonl")])
    result = run_cli("tokens", RUN, cwd=adopted)
    assert result.returncode == 0
    assert json.loads(result.stdout)["by_model"][SONNET]["output"] == 100
    assert "note: no transcript found for agent gone (plumbline:builder, stage build)" in result.stderr


def test_a_missing_transcript_is_noted_once_for_an_agent_that_has_several_ledger_rows(run_cli, adopted):
    rows = [agent_row("gone", transcript="/nowhere/agent-gone.jsonl")] * 4 + [agent_row("gone-too", transcript="/nowhere/agent-gone-too.jsonl")]
    write_ledger(adopted, rows)
    result = run_cli("tokens", RUN, cwd=adopted)
    assert result.returncode == 0
    assert result.stderr.count("no transcript found for agent gone (plumbline:builder, stage build)") == 1
    assert result.stderr.count("no transcript found for agent gone-too (") == 1


def test_gate_rows_and_other_entries_of_the_ledger_are_not_agents(run_cli, adopted, transcripts):
    first, _ = transcripts
    write_ledger(adopted, [{"kind": "gate", "stage": "verify", "gate": "verify_green", "passed": True}, agent_row("a1", transcript=str(first))])
    result = run_cli("tokens", RUN, cwd=adopted)
    assert result.stderr == "" and json.loads(result.stdout)["by_model"][SONNET]["output"] == 100


def test_a_run_without_agents_has_no_usage(run_cli, adopted):
    assert json.loads(run_cli("tokens", RUN, cwd=adopted).stdout) == {"by_model": {}}


def test_a_ledger_with_a_damaged_line_is_still_read(run_cli, adopted, transcripts):
    first, _ = transcripts
    write_ledger(adopted, [agent_row("a1", transcript=str(first))])
    with open(run_path(adopted, RUN, "ledger.jsonl"), "a", encoding="utf-8") as handle:
        handle.write("{torn line\n")
    assert json.loads(run_cli("tokens", RUN, cwd=adopted).stdout)["by_model"][SONNET]["output"] == 100


def test_an_unknown_run_could_not_be_read(run_cli, adopted):
    result = run_cli("tokens", "nope", cwd=adopted)
    assert result.returncode == 2 and "there is no run 'nope'" in result.stderr
