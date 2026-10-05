"""A stale review fails its gate. In a real run the tests changed after test-review had merged, the leg skipped test-review, and `pass` accepted the old
record, so the pass record listed an addressed MAJOR finding as open. A review of the plan or of the tests now carries the hash of what it read
(`target_sha256`, set by merge-review), and its gate, and `pass`, fail when that record has changed since."""
import json

import pytest

import plumbline as pl
from helpers import commit_all, write
from rundata import (
    RUN, adopt, adopt_base, agent_stopped, begin, build_note_record, change_of, intake_record, put, put_part, review_record, run_entry, run_path, spec_record,
    store_request, verify_record, write_test_file, written_tests_record,
)
from test_readme import section
from test_spec_review import REQUEST


@pytest.fixture
def stored(repo, run_cli, tmp_path):
    """A code.M run with its request stored and the planner's spec in place."""
    adopt_base(repo)
    request = write(tmp_path / "request.md", REQUEST)
    assert run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.M", "--request-file", str(request), cwd=repo).returncode == 0
    put(repo, "plan", spec_record())
    return repo


def sha(repo, name):
    return pl.file_sha256(run_path(repo, RUN, f"{name}.json"))


def record(repo, stage):
    return json.loads(run_path(repo, RUN, f"{stage}.json").read_text(encoding="utf-8"))


def clean_round(run_cli, repo, stage="spec-review", lens="requirements", round_no=1):
    put_part(repo, stage, f"prosecutor-{lens}", {"lens": lens, "findings": []}, round_no)
    assert run_cli("merge-review", RUN, stage, "--round", str(round_no), cwd=repo).returncode == 0


def review_the_tests(run_cli, repo):
    """The tests record and its file in place, and test-review merged over them."""
    write_test_file(repo)
    put(repo, "tests", written_tests_record())
    clean_round(run_cli, repo, "test-review", "tests")


def revised_tests(repo, detail="run again after the build"):
    put(repo, "tests", {**written_tests_record(), "stub_check": {"ran": True, "all_failed_on_assertions": True, "detail": detail}})


def gate(run_cli, repo, stage):
    return run_cli("gate", RUN, stage, cwd=repo)


# --- merge-review stamps the hash of what the parts read


def test_merge_review_stamps_the_hash_of_the_plan_on_the_spec_review_and_of_the_tests_record_on_the_test_review(run_cli, stored):
    clean_round(run_cli, stored)
    assert record(stored, "spec-review")["target_sha256"] == sha(stored, "plan")
    review_the_tests(run_cli, stored)
    assert record(stored, "test-review")["target_sha256"] == sha(stored, "tests")
    assert pl.check_record("review_record", record(stored, "test-review")) == []


def test_the_review_of_the_diff_carries_no_target_hash_because_its_own_hash_covers_the_change(run_cli, repo):
    adopt(repo, commands={"test": "true"})
    begin(repo, "docs")
    clean_round(run_cli, repo, "review", "docs")
    assert "target_sha256" not in record(repo, "review") and "diff_sha256" in record(repo, "review")
    assert gate(run_cli, repo, "review").returncode == 0  # nothing to go stale against: as it was


def test_the_field_is_an_optional_hash_of_the_review_record_schema():
    schema = pl.load_schema("review_record")
    assert "target_sha256" in schema["properties"] and "target_sha256" not in schema["required"]
    assert schema["properties"]["target_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert "Absent for a review of the diff, which `diff_sha256` covers, and in a record merged before 0.5.1" in schema["properties"]["target_sha256"]["description"]


# --- the gate fails when the target changed since


def test_the_spec_review_gate_passes_while_the_plan_is_as_the_prosecutor_read_it(run_cli, stored):
    clean_round(run_cli, stored)
    assert gate(run_cli, stored, "spec-review").returncode == 0
    agent_stopped(stored, "plan", run_path(stored, RUN, "plan.json"), "planner", "spec")  # a stop that leaves the record as it was changes nothing
    assert gate(run_cli, stored, "spec-review").returncode == 0


def test_the_spec_review_gate_fails_when_the_plan_changed_after_it_and_opens_the_next_round(run_cli, stored):
    clean_round(run_cli, stored)
    put(stored, "plan", {**spec_record(), "goal": "A goal the planner wrote after the review"})
    result = gate(run_cli, stored, "spec-review")
    assert result.returncode == 1
    assert "the plan changed after spec-review read it; run it again as round 2" in result.stdout
    assert "round 2 of 2 is open" in result.stdout and run_path(stored, RUN, "spec-review", "round-2").is_dir()
    assert pl.review_round(stored, RUN, next(s for s in pl.load_project(stored).pipeline["stage"] if s["id"] == "spec-review")) == 2


def test_the_last_round_of_a_review_that_went_stale_has_used_its_rounds(run_cli, stored):
    clean_round(run_cli, stored)
    assert gate(run_cli, stored, "spec-review").returncode == 0
    put(stored, "plan", {**spec_record(), "goal": "Second goal"})
    clean_round(run_cli, stored, round_no=2)
    assert gate(run_cli, stored, "spec-review").returncode == 0  # round 2 of 2, over the new plan
    put(stored, "plan", {**spec_record(), "goal": "Third goal"})
    result = gate(run_cli, stored, "spec-review")
    assert result.returncode == 3 and "run it again as round 3" in result.stdout
    assert "stage 'spec-review' has used its rounds: stop, and bring these problems and the surviving findings to the builder" in result.stdout
    assert not run_path(stored, RUN, "spec-review", "round-3").exists()


def test_the_test_review_gate_fails_when_the_tests_changed_after_it(run_cli, stored):
    review_the_tests(run_cli, stored)
    assert gate(run_cli, stored, "test-review").returncode == 0
    revised_tests(stored)
    result = gate(run_cli, stored, "test-review")
    assert result.returncode == 1 and "the tests changed after test-review read them; run it again as round 2" in result.stdout
    assert "round 2 of 2 is open" in result.stdout


def test_a_review_run_again_as_the_next_round_passes_over_the_changed_record(run_cli, stored):
    review_the_tests(run_cli, stored)
    revised_tests(stored)
    assert gate(run_cli, stored, "test-review").returncode == 1
    clean_round(run_cli, stored, "test-review", "tests", 2)
    assert record(stored, "test-review")["round"] == 2 and record(stored, "test-review")["target_sha256"] == sha(stored, "tests")
    assert gate(run_cli, stored, "test-review").returncode == 0


def test_status_shows_the_stale_review_as_failed_with_the_problem(run_cli, stored):
    review_the_tests(run_cli, stored)
    assert gate(run_cli, stored, "test-review").returncode == 0
    revised_tests(stored)
    out = run_cli("status", "--run", RUN, cwd=stored).stdout
    line = next(i for i, text in enumerate(out.splitlines()) if text.startswith("  test-review"))
    assert "FAIL" in out.splitlines()[line] and "the tests changed after test-review read them; run it again as round 2" in out.splitlines()[line + 1]


def test_a_review_merged_before_the_hash_existed_fails_until_merge_review_is_run_again_for_its_round(run_cli, stored):
    review_the_tests(run_cli, stored)
    old = record(stored, "test-review")
    del old["target_sha256"]
    put(stored, "test-review", old)  # as a record of 0.5.0 is: `put` stamps the hash, so write it without
    path = run_path(stored, RUN, "test-review.json")
    path.write_text(json.dumps(old, indent=2), encoding="utf-8")
    from rundata import merged

    merged(stored, "test-review", path, 1)
    result = gate(run_cli, stored, "test-review")
    assert result.returncode == 1 and "the record does not say which version of the tests test-review read (it was merged before plumbline 0.5.1): run `plumbline.py merge-review r1 test-review --round 1` again" in result.stdout
    assert not run_path(stored, RUN, "test-review", "round-2").exists()  # nothing changed: no round is opened for it
    assert run_cli("merge-review", RUN, "test-review", "--round", "1", cwd=stored).returncode == 0
    assert gate(run_cli, stored, "test-review").returncode == 0


# --- pass, and all_gates_passed


@pytest.fixture
def finished(repo, tmp_path):
    """A finished run of row code.M awaiting its reduce, every record traced as the hook and the commands would have: the real run's shape."""
    adopt_base(repo, commands={"test": "true"})
    write_test_file(repo)
    commit_all(repo, "the tests")
    diff = change_of(repo)
    store_request(repo)
    put(repo, "intake", intake_record("code.M", repo))
    put(repo, "plan", spec_record())
    put(repo, "spec-review", review_record(target="plan", diff=diff))
    put(repo, "tests", written_tests_record())
    run_entry(repo, "tests", None, exit_code=1)
    put(repo, "test-review", review_record(target="tests", diff=diff))
    put(repo, "build", build_note_record())
    put(repo, "verify", verify_record(diff=diff))
    run_entry(repo, "verify", diff)
    put(repo, "review", review_record(diff=diff))
    return repo


def test_pass_accepts_the_run_while_every_review_read_what_is_there(finished):
    record_, problems, _copy = pl.make_pass_record(pl.load_project(finished), RUN)
    assert record_ is not None, problems


def test_pass_refuses_a_run_whose_tests_changed_after_test_review_and_names_the_stage(finished):
    revised_tests(finished)
    record_, problems, _copy = pl.make_pass_record(pl.load_project(finished), RUN)
    assert record_ is None
    assert any("stage 'test-review' (gate no_surviving_blockers): the tests changed after test-review read them; run it again as round 2" in p for p in problems), problems
    assert not (finished / ".plumbline" / "pass").exists()


def test_pass_refuses_a_run_whose_plan_changed_after_spec_review(finished):
    put(finished, "plan", {**spec_record(), "goal": "Another goal"})
    record_, problems, _copy = pl.make_pass_record(pl.load_project(finished), RUN)
    assert record_ is None and any("the plan changed after spec-review read it; run it again as round 2" in p for p in problems), problems


def test_pass_through_the_cli_prints_the_problem_and_writes_nothing(run_cli, finished):
    revised_tests(finished)
    result = run_cli("pass", RUN, cwd=finished)
    assert result.returncode == 1 and "the tests changed after test-review read them; run it again as round 2" in result.stdout
    assert "nothing was written" in result.stdout and not (finished / ".plumbline" / "pass").exists()


def test_all_gates_passed_names_the_stale_review_though_the_reviews_own_record_is_unchanged(run_cli, finished):
    for stage in ("spec-review", "test-review", "review"):
        assert gate(run_cli, finished, stage).returncode == 0
    revised_tests(finished)
    project = pl.load_project(finished)
    reduce_stage = next(s for s in project.pipeline["stage"] if s["id"] == "reduce")
    problems = pl.check_all_gates_passed(project, RUN, reduce_stage)
    assert any(p.startswith("stage 'test-review': the tests changed after test-review read them") for p in problems), problems
    assert not any("stage 'spec-review'" in p for p in problems)  # the plan is as it was read


def test_the_pass_of_a_run_whose_stale_review_was_run_again_is_accepted(run_cli, finished):
    revised_tests(finished)
    put_part(finished, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []}, 2)
    assert run_cli("merge-review", RUN, "test-review", cwd=finished).returncode == 0  # the highest round: round 2
    assert gate(run_cli, finished, "test-review").returncode == 0
    record_, problems, _copy = pl.make_pass_record(pl.load_project(finished), RUN)
    assert record_ is not None, problems
    assert [e["rounds"] for e in record_["stages"] if e["id"] == "test-review"] == [2]  # the pass record says the review took two rounds


# --- the README says it


def test_the_readme_the_prompt_and_the_gate_agree_on_what_a_stale_review_is():
    from test_manifests import between, orchestrator_body
    from test_readme import README, rows

    gates = section("Runs, gates and the pass record")
    assert "A review of the spec or of the tests must have read the record as it is now (`target_sha256`, the hash of the plan or of the tests record that `merge-review` read" in gates
    assert "the tests changed after test-review read them; run it again as round N+1" in gates and "with rounds left, `gate` opens that round's directory" in gates
    assert "It evaluates every gate afresh" in gates and "no review of the plan or of the tests has gone stale" in gates  # what `pass` does, and what the push gate re-evaluates
    assert "A review record merged before 0.5.1 has no such hash, and its gate fails until `merge-review` is run again for its round." in gates
    records = {r[0].strip("`"): r for r in rows(section("Records"))}
    assert "`target_sha256`" in records["review_record"][2]
    failing = between(orchestrator_body(), "## When a gate fails", "## A decision comes back")
    assert "A review that fails with \"the tests changed after test-review read them\" (or the plan, after spec-review) goes back to no agent stage" in failing
    assert "run its review again as the round `gate` opened (`next_round`)" in failing
    assert "merged before 0.5.1" in README
