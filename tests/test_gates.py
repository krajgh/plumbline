"""gate: each gate is evaluated mechanically from a stage's record, and every evaluation is in the ledger."""
import pytest

import plumbline as pl
from helpers import DEFAULT_TOML, commit_all, write
from rundata import (
    RUN, adopt, build_note_record, gate_stages, intake_record, ledger, put, review_record, run_path,
    spec_record, verify_record, written_tests_record, write_code_s_run, write_docs_run,
)


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def gate(run_cli, repo, stage, run_id=RUN):
    return run_cli("gate", run_id, stage, cwd=repo)


def failed_with(result, text):
    assert result.returncode == 1, result.stdout + result.stderr
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


# --- acs_covered


def test_acs_covered_passes_when_every_ac_is_in_some_tests_ac_ids(run_cli, adopted):
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record(covering=("AC-2", "AC-1")))
    assert gate(run_cli, adopted, "tests").returncode == 0


def test_acs_covered_fails_when_an_ac_is_covered_by_no_test(run_cli, adopted):
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record(covering=("AC-1",)))
    failed_with(gate(run_cli, adopted, "tests"), "AC-2 is covered by no test")


def test_acs_covered_needs_the_plan_record(run_cli, adopted):
    put(adopted, "tests", written_tests_record())
    failed_with(gate(run_cli, adopted, "tests"), "plan.json: no such file")


def test_acs_covered_counts_a_test_covering_several_acs(run_cli, adopted):
    put(adopted, "plan", spec_record())
    record = written_tests_record(covering=("AC-1",))
    record["tests"][0]["ac_ids"] = ["AC-1", "AC-2"]
    put(adopted, "tests", record)
    assert gate(run_cli, adopted, "tests").returncode == 0


# --- tests_fail_on_stub: no stage of the default pipeline has it, so a repository's own pipeline does

STUB_PIPELINE = DEFAULT_TOML.read_text().replace('gate = "acs_covered"', 'gate = "tests_fail_on_stub"')


@pytest.fixture
def stub_repo(adopted):
    write(adopted / "pipelines" / "stubs.toml", STUB_PIPELINE)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/stubs.toml"\n')
    commit_all(adopted, "own pipeline")
    return adopted


def test_tests_fail_on_stub_passes_when_the_check_ran_and_every_test_failed_on_an_assertion(run_cli, stub_repo):
    put(stub_repo, "tests", written_tests_record(ran=True, all_failed=True))
    result = gate(run_cli, stub_repo, "tests")
    assert result.returncode == 0, result.stdout
    assert "gate tests_fail_on_stub for stage 'tests': pass" in result.stdout


def test_tests_fail_on_stub_fails_when_the_check_did_not_run(run_cli, stub_repo):
    put(stub_repo, "tests", written_tests_record(ran=False, all_failed=False))
    failed_with(gate(run_cli, stub_repo, "tests"), "the stub check did not run")


def test_tests_fail_on_stub_fails_when_a_test_failed_for_another_reason(run_cli, stub_repo):
    put(stub_repo, "tests", written_tests_record(ran=True, all_failed=False))
    failed_with(gate(run_cli, stub_repo, "tests"), "not every test failed on an assertion")


# --- verify_green


def test_verify_green_passes_on_a_green_record(run_cli, adopted):
    put(adopted, "verify", verify_record(green=True))
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 0, result.stdout
    assert "gate verify_green for stage 'verify': pass" in result.stdout


def test_verify_green_fails_on_a_record_that_is_not_green_naming_the_failing_acs(run_cli, adopted):
    put(adopted, "verify", verify_record(green=False))
    failed_with(gate(run_cli, adopted, "verify"), "the verify record is not green (failing: AC-1)")


# --- no_surviving_blockers, on both review stages


def test_no_surviving_blockers_passes_at_zero(run_cli, adopted):
    put(adopted, "review", review_record(blockers=0))
    assert gate(run_cli, adopted, "review").returncode == 0
    put(adopted, "test-review", review_record(blockers=0, target="tests"))
    assert gate(run_cli, adopted, "test-review").returncode == 0


def test_no_surviving_blockers_fails_above_zero_naming_the_blockers(run_cli, adopted):
    put(adopted, "review", review_record(blockers=1))
    failed_with(gate(run_cli, adopted, "review"), "1 blocker(s) survive: F-1")


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
    put(adopted, "verify", verify_record(green=False))
    assert gate(run_cli, adopted, "verify").returncode == 1
    gate_stages(run_cli, adopted, ["review"])
    failed_with(gate(run_cli, adopted, "reduce"), "stage 'verify': its gate verify_green failed at its last evaluation")


def test_all_gates_passed_uses_the_latest_evaluation_of_a_gate(run_cli, adopted):
    write_docs_run(adopted)
    put(adopted, "verify", verify_record(green=False))
    assert gate(run_cli, adopted, "verify").returncode == 1
    put(adopted, "verify", verify_record(green=True))  # the builder fixed it and verify ran again
    gate_stages(run_cli, adopted, ["verify", "review"])
    assert gate(run_cli, adopted, "reduce").returncode == 0


def test_all_gates_passed_fails_when_a_record_changed_after_its_gate_passed(run_cli, adopted):
    write_docs_run(adopted)
    gate_stages(run_cli, adopted, ["verify", "review"])
    record = verify_record(green=True)
    record["commands"][0]["summary"] = "13 passed, again"
    put(adopted, "verify", record)
    failed_with(gate(run_cli, adopted, "reduce"), "stage 'verify': its record changed after its gate verify_green passed")


def test_all_gates_passed_fails_for_a_stage_without_a_valid_record_even_without_a_gate(run_cli, adopted):
    write_code_s_run(adopted)
    gate_stages(run_cli, adopted, ["plan", "tests", "verify", "review"])
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
    path = put(adopted, "verify", verify_record())
    gate(run_cli, adopted, "verify")
    put(adopted, "verify", verify_record(green=False))
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
    assert ledger(adopted) == []


def test_an_unknown_stage_or_run_could_not_be_evaluated(run_cli, adopted):
    put(adopted, "verify", verify_record())
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
    path = put(adopted, "verify", verify_record())
    before = path.read_bytes()
    gate(run_cli, adopted, "verify")
    assert path.read_bytes() == before
