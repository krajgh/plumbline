"""Rounds of an agent stage are attempts: an agent's work up to the stage's next gate."""
import plumbline as pl

STAGE = {"id": "plan", "role": "planner", "max_rounds": 2}


def agent(agent_id, sha="a" * 64):
    return {"kind": "agent", "stage": "plan", "agent_id": agent_id, "record_sha256": sha, "valid": True}


def gate(passed):
    return {"kind": "gate", "stage": "plan", "gate": "spec_complete", "passed": passed}


def test_one_agent_that_stops_four_times_before_the_gate_is_one_round():
    assert pl.stage_round(STAGE, None, [agent("p1")] * 4) == (1, 2)


def test_the_same_agent_resumed_after_a_failed_gate_is_the_next_round():
    ledger = [agent("p1"), gate(False), agent("p1", "b" * 64)]
    assert pl.stage_round(STAGE, None, ledger) == (2, 2)
    assert pl.rounds_taken(STAGE, None, ledger) == 2


def test_a_gate_run_again_without_new_work_adds_no_round():
    assert pl.stage_round(STAGE, None, [agent("p1"), gate(False), gate(False)]) == (1, 2)


def test_a_second_agent_before_the_gate_is_a_round_of_its_own():
    assert pl.stage_round(STAGE, None, [agent("p1"), agent("p2")]) == (2, 2)


def test_counting_starts_again_after_the_gate_passes_but_the_run_total_keeps_every_attempt():
    ledger = [agent("p1"), gate(False), agent("p1", "b" * 64), gate(True), agent("p2", "c" * 64)]
    assert pl.stage_round(STAGE, None, ledger) == (1, 2)
    assert pl.rounds_taken(STAGE, None, ledger) == 3


def test_entries_of_other_stages_are_not_counted():
    other = {**agent("t1"), "stage": "tests"}
    assert pl.stage_round(STAGE, None, [other, agent("p1"), {**gate(False), "stage": "tests"}, agent("p1")]) == (1, 2)
