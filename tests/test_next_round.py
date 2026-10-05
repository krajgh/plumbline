"""A re-review gets a new round. In a real run the orchestrator re-ran the spec-review prosecutor into `round-1/` after a replan, overwriting its history.
The round the agents of a review stage write in is mechanical: the highest round directory, or the one after it when that round is over (its gate passed,
or what it reviews changed after its records were made). `plan --run` gives it as `next_round`, the hook holds the agents to it, and merge-review
refuses records that predate what the stage reviews."""
import json

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import write
from hookdata import tool_payload
from rundata import RUN, adopt, adopt_base, agent_stopped, begin, put, put_part, run_path, spec_record, write_ledger
from test_readme import section
from test_spec_review import REQUEST, file_findings, finding, panel

STAGE = "spec-review"


@pytest.fixture
def stored(repo, run_cli, tmp_path):
    """An adopted repository with a run of row code.M started with its request, and the planner's spec in place."""
    adopt_base(repo)
    request = write(tmp_path / "request.md", REQUEST)
    assert run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.M", "--request-file", str(request), cwd=repo).returncode == 0
    put(repo, "plan", spec_record())
    return repo


def stage_of(repo, stage=STAGE):
    return next(s for s in pl.load_project(repo).pipeline["stage"] if s["id"] == stage)


def current(repo, stage=STAGE):
    return pl.review_round(repo, RUN, stage_of(repo, stage))


def part(repo, round_no, name="prosecutor-requirements.json", stage=STAGE):
    return run_path(repo, RUN, stage, f"round-{round_no}", name)


def writes(repo, path, role="prosecutor"):
    """The reason the hook denies a review agent's write of `path`, or None."""
    return pre.decide(tool_payload(repo, "Write", {"file_path": str(path), "content": "{}"}, agent_type=f"plumbline:{role}"))


def file_clean_round(repo, round_no=1):
    put_part(repo, STAGE, "prosecutor-requirements", {"lens": "requirements", "findings": []}, round_no)


def merge(run_cli, repo, *extra, stage=STAGE):
    return run_cli("merge-review", RUN, stage, *extra, cwd=repo)


def gate(run_cli, repo, stage=STAGE):
    return run_cli("gate", RUN, stage, cwd=repo)


def passed_round_one(run_cli, repo):
    file_clean_round(repo)
    assert merge(run_cli, repo).returncode == 0
    assert gate(run_cli, repo).returncode == 0


def replanned(repo, goal="A different goal"):
    """The planner is resumed, and stops with another spec."""
    put(repo, "plan", {**spec_record(), "goal": goal})


def next_rounds(run_cli, repo):
    stages = json.loads(run_cli("plan", "--run", RUN, "--json", cwd=repo).stdout)["stages"]
    return {s["id"]: s["next_round"] for s in stages if "next_round" in s}


# --- the round a review stage is in


def test_a_stage_with_no_round_directory_is_in_round_1(stored):
    assert current(stored) == 1 and not part(stored, 1).parent.exists()


def test_a_round_that_is_being_written_or_merged_but_has_no_gate_yet_goes_on_in_its_directory(run_cli, stored):
    file_clean_round(stored)
    assert current(stored) == 1  # the prosecutor has written: the defenders and the detective are still to come in this round
    assert merge(run_cli, stored).returncode == 0
    assert current(stored) == 1  # merged, and not gated: the panel may still be called, and the detective written, in this round


def test_a_round_whose_gate_passed_is_over_and_the_next_review_is_the_next_round(run_cli, stored):
    passed_round_one(run_cli, stored)
    assert current(stored) == 2
    assert pre.current_round(pl, stored, RUN, STAGE) == 2  # the hook counts the same round


def test_a_round_whose_gate_failed_has_its_next_directory_open_already_and_that_is_the_round(run_cli, stored):
    file_findings(stored, [finding()])
    panel(stored, "requirements-1")
    assert merge(run_cli, stored).returncode == 0
    result = gate(run_cli, stored)
    assert result.returncode == 1 and "round 2 of 2 is open" in result.stdout
    assert part(stored, 2).parent.is_dir() and current(stored) == 2  # the directory `gate` opened: the round the replanned spec is reviewed in


def test_a_round_whose_record_is_not_the_one_its_gate_passed_on_is_not_over(run_cli, stored):
    passed_round_one(run_cli, stored)
    assert current(stored) == 2
    record = run_path(stored, RUN, f"{STAGE}.json")
    record.write_text(record.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # the record changed after its gate passed: the round is the one being worked on again
    assert current(stored) == 1


def test_a_gate_that_passed_for_another_stage_does_not_end_this_stages_round(run_cli, stored):
    file_clean_round(stored)
    assert merge(run_cli, stored).returncode == 0
    write_ledger(stored, [{"kind": "gate", "stage": "tests", "gate": "tests_fail_on_stub", "passed": True, "record_sha256": pl.file_sha256(run_path(stored, RUN, f"{STAGE}.json"))}])
    assert current(stored) == 1


# --- a review of something that changed is the next round, whether or not the round's gate ran


def test_records_made_before_the_plan_last_changed_make_the_round_stale_and_the_review_is_the_next_round(stored):
    file_clean_round(stored)
    assert current(stored) == 1
    replanned(stored)  # the planner stops with another spec: the prosecutor read the old one
    assert current(stored) == 2 and pre.current_round(pl, stored, RUN, STAGE) == 2


def test_a_plan_that_changed_before_the_records_were_made_leaves_the_round_alone(stored):
    replanned(stored)
    file_clean_round(stored)
    assert current(stored) == 1


def test_a_stop_of_the_planner_that_changes_nothing_does_not_make_what_was_reviewed_stale(stored):
    file_clean_round(stored)
    agent_stopped(stored, "plan", run_path(stored, RUN, "plan.json"), "planner", "spec")  # resumed, and it left the spec as it was
    assert current(stored) == 1
    replanned(stored)
    assert current(stored) == 2


def test_changed_at_is_where_the_record_last_became_what_it_is():
    def stop(sha, kind="agent"):
        return {"kind": kind, "stage": "plan", "record_sha256": sha}

    other = {"kind": "agent", "stage": "tests", "record_sha256": "x"}
    assert pl.changed_at([], "plan") is None and pl.changed_at([other], "plan") is None
    assert pl.changed_at([stop("a")], "plan") == 0
    assert pl.changed_at([stop("a"), stop("a")], "plan") == 0  # the second stop left it as it was
    assert pl.changed_at([stop("a"), other, stop("b"), stop("b")], "plan") == 2
    assert pl.changed_at([stop("a"), stop("b"), stop("a")], "plan") == 2  # back to what it was is a change too
    assert pl.changed_at([stop("a", "supplied"), stop("a")], "plan") == 0  # a supplied record counts as the first
    assert pl.changed_at([{"kind": "gate", "stage": "plan", "record_sha256": "z"}], "plan") is None


def test_the_review_of_the_tests_is_stale_when_the_test_writer_changed_the_tests_after_its_records(run_cli, stored):
    from rundata import write_test_file, written_tests_record

    write_test_file(stored)
    put(stored, "tests", written_tests_record())
    put_part(stored, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    unit = pl.load_project(stored).pipeline["stage"]
    test_review = next(s for s in unit if s["id"] == "test-review")
    assert pl.review_round(stored, RUN, test_review) == 1
    put(stored, "tests", {**written_tests_record(), "stub_check": {"ran": True, "all_failed_on_assertions": True, "detail": "run again"}})  # the tests changed: a new record
    assert pl.review_round(stored, RUN, test_review) == 2


def test_the_review_of_the_diff_has_no_stage_to_go_stale_against_and_is_over_when_its_gate_passed(run_cli, repo):
    adopt(repo, commands={"test": "true"})
    begin(repo, "docs")
    review = next(s for s in pl.load_project(repo).pipeline["stage"] if s["id"] == "review")
    put_part(repo, "review", "prosecutor-docs", {"lens": "docs", "findings": []})
    assert pl.review_round(repo, RUN, review) == 1
    assert run_cli("merge-review", RUN, "review", cwd=repo).returncode == 0 and pl.review_round(repo, RUN, review) == 1
    assert run_cli("gate", RUN, "review", cwd=repo).returncode == 0
    assert pl.review_round(repo, RUN, review) == 2  # the review that looks at what was fixed is round 2


# --- the hook holds the agents to that round: a round that is over keeps its records


def test_after_a_passed_round_the_hook_lets_a_review_agent_write_the_next_round_and_denies_the_one_that_is_over(run_cli, stored):
    passed_round_one(run_cli, stored)
    old = part(stored, 1).read_bytes()
    reason = writes(stored, part(stored, 1))
    assert reason and f"the prosecutor writes in the current round of {STAGE} (round-2); .plumbline/runs/r1/{STAGE}/round-1/prosecutor-requirements.json is in round-1" in reason
    assert "`next_round`" in reason and "plumbline.py gate" not in reason
    for role, name in (("prosecutor", "prosecutor-requirements.json"), ("defender", "defender-1.json"), ("defender", "screen-1.json"), ("detective", "detective.json")):
        assert writes(stored, part(stored, 2, name), role) is None, (role, name)
        assert writes(stored, part(stored, 1, name), role), (role, name)
    assert writes(stored, part(stored, 3)) is not None  # and not a round further on
    assert part(stored, 1).read_bytes() == old


def test_the_orchestrator_that_re_runs_the_review_after_a_replan_writes_the_second_round_and_the_first_stays(run_cli, stored):
    passed_round_one(run_cli, stored)
    first, record = part(stored, 1).read_bytes(), part(stored, 1)
    replanned(stored)  # the user's decision sent the run back to the plan
    assert writes(stored, record) is not None  # the old directory is shut
    file_clean_round(stored, 2)  # the new prosecutor writes where `next_round` says
    assert record.read_bytes() == first  # the first round's record is as it was: the history is on disk
    result = merge(run_cli, stored)  # no --round: the highest round
    assert result.returncode == 0 and json.loads(run_path(stored, RUN, f"{STAGE}.json").read_text(encoding="utf-8"))["round"] == 2
    assert gate(run_cli, stored).returncode == 0
    assert current(stored) == 3  # and that round is over in its turn


# --- merge-review refuses records that predate what the stage reviews


def test_merge_review_refuses_a_round_whose_records_were_made_before_the_plan_last_changed(run_cli, stored):
    file_clean_round(stored)
    replanned(stored)
    result = merge(run_cli, stored)
    assert result.returncode == 1
    assert (
        f"error: .plumbline/runs/r1/{STAGE}/round-1/prosecutor-requirements.json: its agent stopped before the plan last changed, so it reviewed an older version; "
        "run the agent again in round 2 (`plumbline.py plan --run r1` gives it as the stage's `next_round`)"
    ) in result.stdout
    assert not run_path(stored, RUN, f"{STAGE}.json").exists()


def test_the_same_round_is_merged_once_its_agents_have_run_again_after_the_change(run_cli, stored):
    file_clean_round(stored)
    replanned(stored)
    file_clean_round(stored, 2)
    assert merge(run_cli, stored).returncode == 0  # the highest round, whose records follow the new plan
    assert merge(run_cli, stored, "--round", "1").returncode == 1  # the old one is still refused


def test_a_stop_of_the_planner_that_changed_nothing_does_not_stop_a_merge(run_cli, stored):
    file_clean_round(stored)
    agent_stopped(stored, "plan", run_path(stored, RUN, "plan.json"), "planner", "spec")
    assert merge(run_cli, stored).returncode == 0


def test_merge_review_of_the_diff_has_nothing_to_compare_with_and_merges_as_it_did(run_cli, repo):
    adopt(repo, commands={"test": "true"})
    begin(repo, "docs")
    put_part(repo, "review", "prosecutor-docs", {"lens": "docs", "findings": []})
    assert run_cli("merge-review", RUN, "review", cwd=repo).returncode == 0


# --- the plan says it


def test_plan_run_gives_each_review_stage_its_next_round_and_no_other_stage_has_one(run_cli, stored):
    stages = json.loads(run_cli("plan", "--run", RUN, "--json", cwd=stored).stdout)["stages"]
    assert [(s["id"], s["next_round"]) for s in stages if "next_round" in s] == [("spec-review", 1), ("test-review", 1), ("review", 1)]
    assert {s["id"] for s in stages if s["kind"] == "review"} == {s["id"] for s in stages if "next_round" in s}
    assert next_rounds(run_cli, stored) == {"spec-review": 1, "test-review": 1, "review": 1}


def test_plan_run_follows_the_run_as_its_rounds_pass(run_cli, stored):
    passed_round_one(run_cli, stored)
    assert next_rounds(run_cli, stored) == {"spec-review": 2, "test-review": 1, "review": 1}
    replanned(stored)
    file_clean_round(stored, 2)
    assert next_rounds(run_cli, stored)["spec-review"] == 2  # round 2 is being written
    assert merge(run_cli, stored).returncode == 0 and gate(run_cli, stored).returncode == 0
    assert next_rounds(run_cli, stored)["spec-review"] == 3


def test_the_plan_that_starts_a_run_and_the_plan_of_the_run_that_has_begun_agree(run_cli, repo, tmp_path):
    adopt_base(repo)
    request = write(tmp_path / "request.md", REQUEST)
    started = json.loads(run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.M", "--request-file", str(request), cwd=repo).stdout)
    assert [s["next_round"] for s in started["stages"] if s["kind"] == "review"] == [1, 1, 1]
    assert json.loads(run_cli("plan", "--run", RUN, cwd=repo).stdout) == started


def test_plan_run_still_writes_nothing_for_it(run_cli, stored):
    passed_round_one(run_cli, stored)
    before = sorted((p.relative_to(stored).as_posix(), p.read_bytes()) for p in (stored / ".plumbline").rglob("*") if p.is_file())
    assert run_cli("plan", "--run", RUN, "--json", cwd=stored).returncode == 0
    assert before == sorted((p.relative_to(stored).as_posix(), p.read_bytes()) for p in (stored / ".plumbline").rglob("*") if p.is_file())


# --- the README and the prompt say so


def test_the_readme_describes_the_current_round_the_next_round_and_the_merge_refusal():
    text = section("Runs, gates and the pass record")
    paragraph = text.split("**Review rounds beyond the first.**", 1)[1].split("\n\n", 1)[0]
    for needed in (
        "The review agents write only in their stage's current round", "so the hook, `merge-review` and `gate` count one round",
        "or the one after it when that round is over, and a round is over when its gate has passed on the record `merge-review` built from it, or when what the stage reviews",
        "has changed since: its merged record holds the hash it read (`target_sha256`), and records not yet merged were made before the change",
        "A stop of the planner or the test-writer that leaves its record as it was changes nothing",
        "it never writes over the history of a round that is over", "`plan --run RUN` gives the number as each review stage's `next_round`",
        "`merge-review` refuses a round whose records predate the stage's upstream record", "A prosecutors' round that filed no findings needs no defender",
    ):
        assert needed in paragraph, needed
    assert "and the one after it when that round is over (see Review rounds beyond the first)" in section("Hooks")
    from test_readme import README

    assert "a review stage also gives its `next_round`, the round its agents write in" in README and "made before the stage it reviews last changed" in README


def test_the_orchestrator_uses_the_next_round_of_the_plan_for_every_round_of_a_review():
    from test_manifests import between, orchestrator_body

    body = orchestrator_body()
    assert "and, for a review stage, the `next_round` its agents write in" in between(body, "## Where the run stands", "Take the stages in order")
    units = between(body, "## Review units", "Start each round with")
    assert "the round its agents write in is the stage's `next_round` in the plan (`PLUMBLINE plan --run <run id> --json` gives it afresh)" in units
    assert "A review that runs again because what it reviews changed (a replan, a rebuild, revised tests), or to look at what was fixed after its gate passed, is the next round" in units
    assert "Launch each round's agents with its `next_round` in the paths of their briefs" in units
    assert "with nothing to defend, no defender runs, in a re-review as in any round" in between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    failing = between(body, "## When a gate fails", "## A decision comes back")
    assert failing.count("the one whose directory `gate` opened (its `next_round`)") == 2
