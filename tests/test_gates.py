"""gate: each gate is evaluated mechanically from a stage's record and the ledger, and every evaluation is in the ledger."""
import pytest

import plumbline as pl
from helpers import DEFAULT_TOML, commit_all, write
from rundata import (
    CONTROLLED, RUN, adopt, begin, build_note_record, change_of, gate_stages, intake_record, ledger, now_hash, put, review_record, run_path,
    set_exit_code, spec_record, verify_now, verify_record, write_code_s_run, write_docs_run, write_test_file, written_tests_record,
)


@pytest.fixture
def adopted(repo):
    """Adopted, with a test command whose exit status the test sets (see set_exit_code): 0 unless it says otherwise."""
    adopt(repo, commands={"test": CONTROLLED})
    return repo


def gate(run_cli, repo, stage, run_id=RUN):
    return run_cli("gate", run_id, stage, cwd=repo)


def failed_with(result, text, code=1):
    assert result.returncode == code, result.stdout + result.stderr
    assert "FAIL" in result.stdout and text in result.stdout, result.stdout


# --- spec_complete


def test_spec_complete_passes_when_every_ac_has_a_test_plan_entry(run_cli, adopted):
    put(adopted, "plan", spec_record())
    result = gate(run_cli, adopted, "plan")
    assert result.returncode == 0, result.stdout
    assert "gate spec_complete for stage 'plan': pass" in result.stdout


def test_spec_complete_fails_when_an_ac_has_no_test_plan_entry(run_cli, adopted):
    put(adopted, "plan", spec_record(planned=("AC-1",)))
    failed_with(gate(run_cli, adopted, "plan"), "AC-2 has no test_plan entry")


def test_spec_complete_fails_without_any_acceptance_criterion(run_cli, adopted):
    record = spec_record()
    record["acceptance_criteria"], record["test_plan"] = [], []
    put(adopted, "plan", record)
    failed_with(gate(run_cli, adopted, "plan"), "the spec has no acceptance criteria")


def test_spec_complete_fails_on_a_duplicate_acceptance_criterion_id(run_cli, adopted):  # C-15
    record = spec_record()
    record["acceptance_criteria"].append(dict(record["acceptance_criteria"][0]))  # AC-1 twice
    put(adopted, "plan", record)
    failed_with(gate(run_cli, adopted, "plan"), "AC-1 is defined more than once")


def test_spec_complete_fails_on_a_test_plan_entry_for_an_unknown_criterion(run_cli, adopted):  # C-15
    record = spec_record()
    record["test_plan"].append({"ac": "AC-9", "scenario": "AC-9 does not exist"})
    put(adopted, "plan", record)
    failed_with(gate(run_cli, adopted, "plan"), "the test_plan has an entry for AC-9, which the spec does not define")


def test_a_gate_fails_on_a_record_that_does_not_validate_naming_the_error(run_cli, adopted):
    record = spec_record()
    del record["goal"]
    put(adopted, "plan", record)
    failed_with(gate(run_cli, adopted, "plan"), "$.goal: missing required key")


def test_a_gate_fails_on_a_record_that_is_not_json(run_cli, adopted):
    put(adopted, "plan", "{not json")
    failed_with(gate(run_cli, adopted, "plan"), "not valid JSON")


def test_a_gate_fails_on_a_missing_record(run_cli, adopted):
    put(adopted, "intake", intake_record("code.S"))  # the run exists, its plan record does not
    failed_with(gate(run_cli, adopted, "plan"), ".plumbline/runs/r1/plan.json: no such file")


# --- acs_covered: the gate of a repository's own pipeline whose tests stage has it

COVERED_PIPELINE = DEFAULT_TOML.read_text().replace('gate = "tests_fail_on_stub"', 'gate = "acs_covered"')


@pytest.fixture
def covered(adopted):
    write(adopted / "pipelines" / "covered.toml", COVERED_PIPELINE)
    write(adopted / "plumbline.toml", (adopted / "plumbline.toml").read_text().replace('pipeline = "default"', 'pipeline = "pipelines/covered.toml"'))
    commit_all(adopted, "own pipeline")
    write_test_file(adopted)  # tests/test_app.py holds test_ac_1 and test_ac_2
    return adopted


def test_acs_covered_passes_when_every_ac_is_in_some_tests_ac_ids(run_cli, covered):
    put(covered, "plan", spec_record())
    put(covered, "tests", written_tests_record(covering=("AC-2", "AC-1")))
    result = gate(run_cli, covered, "tests")
    assert result.returncode == 0, result.stdout
    assert "gate acs_covered for stage 'tests': pass" in result.stdout


def test_acs_covered_fails_when_an_ac_is_covered_by_no_test(run_cli, covered):
    put(covered, "plan", spec_record())
    put(covered, "tests", written_tests_record(covering=("AC-1",)))
    failed_with(gate(run_cli, covered, "tests"), "AC-2 is covered by no test")


def test_acs_covered_needs_the_plan_record(run_cli, covered):
    put(covered, "tests", written_tests_record())
    failed_with(gate(run_cli, covered, "tests"), "plan.json: no such file")


def test_acs_covered_counts_a_test_covering_several_acs(run_cli, covered):
    put(covered, "plan", spec_record())
    record = written_tests_record(covering=("AC-1",))
    record["tests"][0]["ac_ids"] = ["AC-1", "AC-2"]
    put(covered, "tests", record)
    assert gate(run_cli, covered, "tests").returncode == 0


def test_acs_covered_fails_for_a_test_file_that_does_not_exist(run_cli, covered):  # C-05
    put(covered, "plan", spec_record())
    record = written_tests_record()
    record["tests"][0]["file"] = "tests/test_ghost.py"
    put(covered, "tests", record)
    result = gate(run_cli, covered, "tests")
    failed_with(result, "test T-1: the file tests/test_ghost.py does not exist")
    assert "AC-1 is covered by no test" in result.stdout  # a test that is not there covers nothing


def test_acs_covered_fails_for_a_test_whose_file_is_a_source_file(run_cli, covered):  # C-05
    put(covered, "plan", spec_record())
    record = written_tests_record()
    record["tests"][0]["file"] = "src/app.py"
    put(covered, "tests", record)
    failed_with(gate(run_cli, covered, "tests"), "test T-1: the file src/app.py is a code file, not a tests file")


def test_acs_covered_fails_for_a_test_name_the_file_does_not_hold(run_cli, covered):  # C-05
    put(covered, "plan", spec_record())
    record = written_tests_record()
    record["tests"][1]["name"] = "test_nothing_of_the_kind"
    put(covered, "tests", record)
    failed_with(gate(run_cli, covered, "tests"), "test T-2: the file tests/test_app.py has no test named 'test_nothing_of_the_kind'")


def test_acs_covered_accepts_a_node_id_and_a_parametrised_name(run_cli, covered):
    put(covered, "plan", spec_record())
    record = written_tests_record()
    record["tests"][0]["name"] = "tests/test_app.py::test_ac_1"
    record["tests"][1]["name"] = "test_ac_2[3-4]"
    put(covered, "tests", record)
    assert gate(run_cli, covered, "tests").returncode == 0


def test_acs_covered_fails_for_a_criterion_the_spec_does_not_have(run_cli, covered):  # C-05
    put(covered, "plan", spec_record())
    record = written_tests_record()
    record["tests"][0]["ac_ids"] = ["AC-1", "AC-7"]
    put(covered, "tests", record)
    failed_with(gate(run_cli, covered, "tests"), "test T-1 claims AC-7, which the spec does not have")


@pytest.mark.parametrize("path", ["../outside.py", "/etc/hostname", "tests/../../outside.py"])
def test_acs_covered_refuses_a_test_file_outside_the_repository(run_cli, covered, path):
    put(covered, "plan", spec_record())
    record = written_tests_record()
    record["tests"][0]["file"] = path
    put(covered, "tests", record)
    failed_with(gate(run_cli, covered, "tests"), f"the file {path} is outside the repository")


def test_acs_covered_reads_the_tests_type_from_the_repositorys_own_pipeline_types(run_cli, covered):
    write(covered / "checks" / "cases.py", "def test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n")
    put(covered, "plan", spec_record())
    record = written_tests_record()
    for test in record["tests"]:
        test["file"] = "checks/cases.py"
    put(covered, "tests", record)
    failed_with(gate(run_cli, covered, "tests"), "the file checks/cases.py is a code file, not a tests file")


# --- tests_fail_on_stub: the default gate of the tests stage, measured


@pytest.fixture
def tested(adopted):
    """The default pipeline's tests stage: a plan, the tests file the record names, and the test command failing (exit 1)."""
    write_test_file(adopted)
    put(adopted, "plan", spec_record())
    set_exit_code(adopted, 1)
    return adopted


def test_tests_fail_on_stub_passes_when_the_tests_cover_the_spec_and_the_test_command_fails(run_cli, tested):
    put(tested, "tests", written_tests_record())
    result = gate(run_cli, tested, "tests")
    assert result.returncode == 0, result.stdout
    assert "gate tests_fail_on_stub for stage 'tests': pass" in result.stdout


def test_tests_fail_on_stub_fails_when_the_test_command_passes(run_cli, tested):
    set_exit_code(tested, 0)
    put(tested, "tests", written_tests_record())
    failed_with(gate(run_cli, tested, "tests"), "the test command exited 0: every test passed, so none of them fails without the change it tests")


def test_tests_fail_on_stub_fails_when_the_check_did_not_run(run_cli, tested):
    put(tested, "tests", written_tests_record(ran=False, all_failed=False))
    failed_with(gate(run_cli, tested, "tests"), "the stub check did not run")


def test_tests_fail_on_stub_fails_when_a_test_failed_for_another_reason(run_cli, tested):
    put(tested, "tests", written_tests_record(ran=True, all_failed=False))
    failed_with(gate(run_cli, tested, "tests"), "not every test failed on an assertion against the stubs")


def test_tests_fail_on_stub_needs_at_least_one_test(run_cli, tested):  # C-27
    record = written_tests_record()
    record["tests"] = []
    put(tested, "tests", record)
    result = gate(run_cli, tested, "tests")
    failed_with(result, "there are no tests")
    assert "AC-1 is covered by no test" in result.stdout


def test_the_default_pipelines_tests_stage_has_the_measured_gate_and_a_retry():  # C-05, C-08
    tests = next(s for s in pl.load_toml(DEFAULT_TOML)["stage"] if s["id"] == "tests")
    assert tests["gate"] == "tests_fail_on_stub"
    assert (tests["on_fail"], tests["max_rounds"]) == ("tests", 3)  # the test-writer runs again


# --- verify_green


def test_verify_green_passes_on_a_green_record_the_commands_agree_with(run_cli, adopted):
    begin(adopted)
    verify_now(adopted)
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 0, result.stdout
    assert "gate verify_green for stage 'verify': pass" in result.stdout


def test_verify_green_fails_on_a_record_that_is_not_green_naming_the_failing_acs(run_cli, adopted):
    begin(adopted)
    verify_now(adopted, green=False)
    failed_with(gate(run_cli, adopted, "verify"), "the verify record is not green (failing: AC-1)")


def test_a_verify_failure_lists_the_failing_commands_and_false_checks(run_cli, adopted):  # C-17
    begin(adopted)
    record = verify_record(green=False, diff=now_hash(adopted))
    record["failing_acs"] = []
    record["commands"] = [{"name": "lint", "command": "ruff check", "exit_code": 2, "summary": "3 errors"}]
    record["checks"] = {"lint": False, "secrets": True, "symlinks": True, "abs_paths": False, "graft_fresh": None}
    put(adopted, "verify", record)
    out = gate(run_cli, adopted, "verify").stdout
    assert "the verify record is not green" in out
    assert "the record's lint command exited 2" in out
    assert "the record's check lint is false" in out and "the record's check abs_paths is false" in out
    assert "the record's check secrets is false" not in out


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda r: r.update(failing_acs=[{"ac": "AC-1", "error_type": "AssertionError"}]), "failing_acs"),
        (lambda r: r["tests"].update(failed=7), "tests.failed is 7"),
        (lambda r: r["commands"][0].update(exit_code=1), "the tests command's exit_code is 1"),
        (lambda r: r["checks"].update(secrets=False), "checks.secrets is false"),
        (lambda r: r.update(commands=[]), "lists no commands"),
    ],
)
def test_verify_green_fails_when_the_record_contradicts_itself_naming_the_field(run_cli, adopted, mutate, field):  # C-04
    begin(adopted)
    record = verify_record(green=True, diff=now_hash(adopted))
    mutate(record)
    put(adopted, "verify", record)
    failed_with(gate(run_cli, adopted, "verify"), field)


# --- no_surviving_blockers, on both review stages


def test_no_surviving_blockers_passes_at_zero(run_cli, adopted):
    begin(adopted, "code.M")
    put(adopted, "review", review_record(blockers=0, diff=now_hash(adopted)))
    assert gate(run_cli, adopted, "review").returncode == 0
    put(adopted, "test-review", review_record(blockers=0, target="tests"))
    assert gate(run_cli, adopted, "test-review").returncode == 0


def test_no_surviving_blockers_fails_above_zero_naming_the_blockers(run_cli, adopted):
    begin(adopted)
    put(adopted, "review", review_record(blockers=1, diff=now_hash(adopted)))
    failed_with(gate(run_cli, adopted, "review"), "1 blocker(s) survive: F-1")


def test_no_surviving_blockers_recomputes_the_count_from_the_findings_and_the_survivors(run_cli, adopted):  # C-27
    begin(adopted)
    record = review_record(blockers=1, diff=now_hash(adopted))
    record["blockers_surviving"] = 0  # hand-set: F-1 is BLOCKING and survives
    put(adopted, "review", record)
    result = gate(run_cli, adopted, "review")
    failed_with(result, "blockers_surviving says 0, but the findings and survivors give 1")
    assert "1 blocker(s) survive: F-1" in result.stdout


def test_no_surviving_blockers_fails_for_a_survivor_that_is_no_finding(run_cli, adopted):  # C-27
    begin(adopted)
    record = review_record(blockers=1, diff=now_hash(adopted))
    record["survivors"] = ["F-1", "NOPE-9"]
    put(adopted, "review", record)
    failed_with(gate(run_cli, adopted, "review"), "survivors names 'NOPE-9', which is no finding of this round")


def test_no_surviving_blockers_ignores_a_surviving_finding_that_is_not_blocking(run_cli, adopted):
    begin(adopted)
    record = review_record(blockers=1, diff=now_hash(adopted))
    record["findings"][0]["severity"] = "MAJOR"
    record["blockers_surviving"] = 0
    put(adopted, "review", record)
    assert gate(run_cli, adopted, "review").returncode == 0


def test_the_gate_of_a_review_compares_the_records_hash_with_the_change_now(run_cli, adopted):  # C-10
    begin(adopted)
    put(adopted, "review", review_record(blockers=0, diff="f" * 64))  # a made-up hash
    failed_with(gate(run_cli, adopted, "review"), "the record covers the change ffffffffffff, but the change hashes to")


def test_the_gate_of_the_test_review_does_not_compare_hashes(run_cli, adopted):
    begin(adopted, "code.M")
    put(adopted, "test-review", review_record(blockers=0, target="tests", diff="f" * 64))  # made before the build: the change differs by design
    assert gate(run_cli, adopted, "test-review").returncode == 0


def test_the_gate_of_verify_compares_the_records_hash_with_the_change_now(run_cli, adopted):  # C-10
    begin(adopted)
    put(adopted, "verify", verify_record(green=True, diff="f" * 64))
    failed_with(gate(run_cli, adopted, "verify"), "the record covers the change ffffffffffff, but the commands ran on the change")


# --- all_gates_passed, from the ledger


def test_all_gates_passed_passes_when_every_other_gate_passed_in_the_ledger(run_cli, adopted):
    write_docs_run(adopted)
    gate_stages(run_cli, adopted, ["verify", "review"])
    result = gate(run_cli, adopted, "reduce")
    assert result.returncode == 0, result.stdout
    assert "gate all_gates_passed for stage 'reduce': pass" in result.stdout


def test_all_gates_passed_fails_for_a_gate_that_was_never_evaluated(run_cli, adopted):
    write_docs_run(adopted)
    gate_stages(run_cli, adopted, ["verify"])
    failed_with(gate(run_cli, adopted, "reduce"), "stage 'review': its gate no_surviving_blockers has not been evaluated")


def test_all_gates_passed_fails_when_the_last_evaluation_of_a_gate_failed(run_cli, adopted):
    write_docs_run(adopted)
    verify_now(adopted, green=False)
    assert gate(run_cli, adopted, "verify").returncode == 1
    gate_stages(run_cli, adopted, ["review"])
    failed_with(gate(run_cli, adopted, "reduce"), "stage 'verify': its gate verify_green failed at its last evaluation")


def test_all_gates_passed_uses_the_latest_evaluation_of_a_gate(run_cli, adopted):
    write_docs_run(adopted)
    verify_now(adopted, green=False)
    assert gate(run_cli, adopted, "verify").returncode == 1
    verify_now(adopted, green=True)  # the builder fixed it and verify ran again
    gate_stages(run_cli, adopted, ["verify", "review"])
    assert gate(run_cli, adopted, "reduce").returncode == 0


def test_all_gates_passed_fails_when_a_record_changed_after_its_gate_passed(run_cli, adopted):
    write_docs_run(adopted)
    gate_stages(run_cli, adopted, ["verify", "review"])
    record = verify_record(green=True, diff=change_of(adopted))
    record["commands"][0]["summary"] = "13 passed, again"
    put(adopted, "verify", record)  # a verifier that ran again: traced, but its gate was not evaluated on this record
    failed_with(gate(run_cli, adopted, "reduce"), "stage 'verify': its record changed after its gate verify_green passed")


def test_all_gates_passed_fails_for_a_stage_without_a_valid_record_even_without_a_gate(run_cli, adopted):
    write_code_s_run(adopted)
    gate_stages(run_cli, adopted, ["plan", "verify", "review"])
    set_exit_code(adopted, 1)
    assert gate(run_cli, adopted, "tests").returncode == 0  # the tests fail, as they must
    set_exit_code(adopted, 0)
    assert gate(run_cli, adopted, "reduce").returncode == 0
    run_path(adopted, RUN, "build.json").unlink()  # the builder never wrote its note
    failed_with(gate(run_cli, adopted, "reduce"), "stage 'build': .plumbline/runs/r1/build.json: no such file")


def test_all_gates_passed_needs_a_usable_intake_record(run_cli, adopted):
    put(adopted, "verify", verify_record())
    result = gate(run_cli, adopted, "reduce")
    assert result.returncode == 2
    assert "intake record is not usable" in result.stderr


def test_all_gates_passed_ignores_stages_the_rows_leaves_out(run_cli, adopted):
    # the docs row has no plan or tests: nothing of theirs is required
    write_docs_run(adopted)
    put(adopted, "plan", spec_record(planned=()))  # a stray, failing record of a stage outside the row
    gate_stages(run_cli, adopted, ["verify", "review"])
    assert gate(run_cli, adopted, "reduce").returncode == 0


# --- the command around the gates


def test_every_evaluation_is_entered_in_the_ledger_with_the_records_hash(run_cli, adopted):
    begin(adopted)
    path = verify_now(adopted)
    gate(run_cli, adopted, "verify")
    verify_now(adopted, green=False)
    gate(run_cli, adopted, "verify")
    first, second = [e for e in ledger(adopted) if e["kind"] == "gate"]
    assert (first["stage"], first["gate"], first["passed"]) == ("verify", "verify_green", True)
    assert second["passed"] is False and second["problems"]
    assert first["record_sha256"] != second["record_sha256"] == pl.file_sha256(path)
    assert first["at"].endswith("Z")


def test_a_stage_without_a_gate_has_nothing_to_evaluate(run_cli, adopted):
    put(adopted, "build", build_note_record())
    result = gate(run_cli, adopted, "build")
    assert result.returncode == 0
    assert "stage 'build' has no gate" in result.stdout
    assert [e for e in ledger(adopted) if e["kind"] == "gate"] == []


def test_an_unknown_stage_or_run_could_not_be_evaluated(run_cli, adopted):
    begin(adopted)
    verify_now(adopted)
    unknown_stage = gate(run_cli, adopted, "ghost")
    assert unknown_stage.returncode == 2 and "no stage 'ghost'" in unknown_stage.stderr
    unknown_run = gate(run_cli, adopted, "verify", run_id="nope")
    assert unknown_run.returncode == 2 and "there is no run 'nope'" in unknown_run.stderr
    unsafe = gate(run_cli, adopted, "verify", run_id="../escape")
    assert unsafe.returncode == 2 and "run id" in unsafe.stderr


def test_the_gate_of_each_stage_of_the_default_pipeline_is_implemented():
    gates = {s["gate"] for s in pl.load_toml(DEFAULT_TOML)["stage"] if "gate" in s}
    assert gates <= set(pl.GATE_CHECKS) | {"all_gates_passed"}
    assert set(pl.KNOWN_GATES) == set(pl.GATE_CHECKS) | {"all_gates_passed"}


def test_the_test_builders_produce_valid_records():
    for name, record in {
        "change_class": intake_record("code.S"), "spec": spec_record(), "tests_record": written_tests_record(),
        "build_note": build_note_record(), "verify_record": verify_record(), "review_record": review_record(),
    }.items():
        assert pl.check_record(name, record) == [], name
    assert pl.check_record("review_record", review_record(blockers=1)) == []


def test_gate_outcomes_never_write_the_records_they_read(run_cli, adopted):
    begin(adopted)
    path = verify_now(adopted)
    before = path.read_bytes()
    gate(run_cli, adopted, "verify")
    assert path.read_bytes() == before


# --- rounds: `gate` says which round of its stage's max_rounds it is, and stops a stage that has used them


def test_gate_says_which_round_of_the_stages_rounds_it_is(run_cli, adopted):  # C-08
    begin(adopted)
    verify_now(adopted, green=False)
    first = gate(run_cli, adopted, "verify")
    assert first.returncode == 1 and "round 1 of 3" in first.stdout
    verify_now(adopted, green=False)
    second = gate(run_cli, adopted, "verify")
    assert second.returncode == 1 and "round 2 of 3" in second.stdout and "used its rounds" not in second.stdout


def test_a_gate_that_fails_in_the_last_round_exits_3_and_says_to_stop(run_cli, adopted):  # C-08
    begin(adopted)
    for _ in range(3):
        verify_now(adopted, green=False)
    third = gate(run_cli, adopted, "verify")
    failed_with(third, "round 3 of 3", code=3)
    assert "stage 'verify' has used its rounds: stop, and bring these problems and the surviving findings to the builder" in third.stdout


def test_a_gate_that_passes_in_the_last_round_passes(run_cli, adopted):
    begin(adopted)
    for green in (False, False, True):
        verify_now(adopted, green=green)
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 0 and "round 3 of 3" in result.stdout


def test_a_stage_run_past_its_rounds_is_refused_even_with_a_good_record(run_cli, adopted):  # C-08
    begin(adopted)
    for green in (False, False, False, True):
        verify_now(adopted, green=green)
    result = gate(run_cli, adopted, "verify")
    failed_with(result, "round 4 is past the 3 rounds stage 'verify' has", code=3)


def test_a_stage_that_passed_and_runs_again_starts_counting_again(run_cli, adopted):
    begin(adopted)
    verify_now(adopted, green=False)
    verify_now(adopted, green=True)
    assert "round 2 of 3" in gate(run_cli, adopted, "verify").stdout
    verify_now(adopted, green=True)  # the review sent the run back to build: verify runs again
    assert "round 1 of 3" in gate(run_cli, adopted, "verify").stdout


def test_a_review_is_in_the_round_its_record_says(run_cli, adopted):
    begin(adopted)
    put(adopted, "review", review_record(blockers=0, round_no=2, diff=now_hash(adopted)))
    assert "round 2 of 3" in gate(run_cli, adopted, "review").stdout


def test_the_plan_stage_has_two_rounds(run_cli, adopted):  # C-08
    bad = spec_record(planned=("AC-1",))
    put(adopted, "plan", bad)
    assert "round 1 of 2" in gate(run_cli, adopted, "plan").stdout
    put(adopted, "plan", bad)
    failed_with(gate(run_cli, adopted, "plan"), "round 2 of 2", code=3)


def test_a_stage_without_max_rounds_prints_no_round(run_cli, adopted):
    write_docs_run(adopted)
    gate_stages(run_cli, adopted, ["verify", "review"])
    assert "round" not in gate(run_cli, adopted, "reduce").stdout
