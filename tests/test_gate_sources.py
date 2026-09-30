"""The gates of the tests and verify stages have two parts: what the agent's record says (typed), and what `gate` saw when it ran the
repository's commands (measured). Each problem they report starts by saying which part it comes from, so that a failure can be read
without the record and the ledger side by side. In the first real run the tests gate failed with "not every test failed on an
assertion against the stubs" while the measured run was fine, and nobody could tell which part had failed."""
import pytest

import plumbline as pl
from helpers import commit_all, write
from rundata import (
    CONTROLLED, RUN, adopt, begin, intake_record, now_hash, put, review_record, set_exit_code, spec_record, verify_now, verify_record, write_test_file,
    written_tests_record,
)

TYPED = "the agent's record: "
MEASURED = "the measured run: "


@pytest.fixture
def adopted(repo):
    adopt(repo, commands={"test": CONTROLLED})
    return repo


@pytest.fixture
def tested(adopted):
    """A code.S run at its tests stage: a plan, the test file the record names, and a test command that fails (exit 1)."""
    begin(adopted, "code.S")
    write_test_file(adopted)
    put(adopted, "plan", spec_record())
    set_exit_code(adopted, 1)
    return adopted


def gate(run_cli, repo, stage):
    return run_cli("gate", RUN, stage, cwd=repo)


def problems(result):
    """What a gate printed as problems: its lines that start with `  - `, without the dash."""
    return [line[4:] for line in result.stdout.splitlines() if line.startswith("  - ")]


# ------------------------------------------------------------------------------------------------ the tests gate


def test_a_failure_of_the_typed_part_alone_says_the_agents_record_and_not_the_measured_run(run_cli, tested):
    put(tested, "tests", written_tests_record(ran=True, all_failed=False))  # the record says a test passed; the test command fails, as it should
    result = gate(run_cli, tested, "tests")
    assert result.returncode == 1
    assert problems(result) == [TYPED + "stub_check says not every test failed when it ran (a test that passed, or that could not be collected, shows nothing)"]


def test_a_failure_of_the_measured_part_alone_says_the_measured_run(run_cli, tested):
    set_exit_code(tested, 0)
    put(tested, "tests", written_tests_record())  # the record is fine; the test command passes, so no test fails without the change
    result = gate(run_cli, tested, "tests")
    assert result.returncode == 1
    assert problems(result) == [MEASURED + "the test command exited 0: every test passed, so none of them fails without the change it tests"]


def test_when_both_parts_fail_each_problem_is_named_the_record_first(run_cli, tested):
    set_exit_code(tested, 0)
    put(tested, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=False))
    lines = problems(gate(run_cli, tested, "tests"))
    assert [line.split(": ", 1)[0] + ": " for line in lines] == [TYPED, TYPED, MEASURED]
    assert lines[0] == TYPED + "AC-2 is covered by no test" and lines[1].startswith(TYPED + "stub_check says not every test failed when it ran")
    assert lines[2].startswith(MEASURED + "the test command exited 0")


def test_the_gates_own_reading_of_the_record_against_the_spec_and_the_files_is_the_records(run_cli, tested):
    record = written_tests_record()
    record["tests"][0]["file"] = "tests/test_ghost.py"
    record["stub_check"]["ran"] = False
    put(tested, "tests", record)
    lines = problems(gate(run_cli, tested, "tests"))
    assert lines[:2] == [TYPED + "test T-1: the file tests/test_ghost.py does not exist", TYPED + "AC-1 is covered by no test"]
    assert TYPED + "the stub check did not run" in lines
    empty = written_tests_record(covering=())
    put(tested, "tests", empty)
    assert TYPED + "there are no tests" in problems(gate(run_cli, tested, "tests"))


def test_a_test_run_that_changed_a_guarded_file_is_the_measured_runs(run_cli, tested):
    put(tested, "tests", written_tests_record())
    write(tested / "plumbline.toml", (tested / "plumbline.toml").read_text().replace(CONTROLLED, "sh -c 'printf x >> .git/config; exit 1'"))
    commit_all(tested, "a test command that edits git's config")
    assert problems(gate(run_cli, tested, "tests")) == [MEASURED + "the test run changed .git/config"]


def test_a_test_command_that_is_not_there_is_the_measured_part_too(run_cli, repo):
    adopt(repo)  # no [commands]
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    [line] = problems(gate(run_cli, repo, "tests"))
    assert line == MEASURED + 'no test command is declared: add `test = "..."` under [commands] in plumbline.toml and commit it, then evaluate the gate again'


def test_a_pytest_exit_2_says_the_measured_run_and_how_to_avoid_it(run_cli, repo):
    adopt(repo, commands={"test": "sh -c 'exit 2' # pytest"})
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    [line] = problems(gate(run_cli, repo, "tests"))
    assert line.startswith(MEASURED + "the test command exited 2: pytest could not collect the tests") and "inside the test function" in line


def test_the_fix_gate_names_its_parts_too(run_cli, adopted):
    from test_intents import fix_spec, spec_file, started

    started(run_cli, adopted, "--intent", "fix", "--spec", spec_file(adopted, fix_spec()), "--row", "code.S")
    write_test_file(adopted, covering=("AC-1",))
    set_exit_code(adopted, 0)
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=False))
    result = gate(run_cli, adopted, "tests")
    assert "gate reproduces_on_head for stage 'tests': FAIL" in result.stdout
    assert problems(result) == [
        TYPED + "stub_check says not every new test fails on an assertion against today's code (one that passes there, or fails on an import or syntax error, reproduces nothing)",
        MEASURED + "the test command exited 0: every test passed, so none of them fails without the change it tests",
    ]


# ----------------------------------------------------------------------------------------------- the verify gate


def test_a_verify_record_that_is_not_green_is_the_typed_part_and_the_commands_agree(run_cli, adopted):
    begin(adopted)
    verify_now(adopted, green=False)
    lines = problems(gate(run_cli, adopted, "verify"))
    assert lines[0] == TYPED + "the verify record is not green (failing: AC-1)"
    assert lines and all(line.startswith(TYPED) for line in lines)  # the test command exits 0 here: nothing measured is wrong


def test_a_test_command_that_fails_under_a_green_record_is_the_measured_part(run_cli, adopted):
    begin(adopted)
    set_exit_code(adopted, 1)
    verify_now(adopted)  # the record says green
    [line] = problems(gate(run_cli, adopted, "verify"))
    assert line.startswith(MEASURED + "the test command exited 1 (") and CONTROLLED in line


def test_both_parts_of_the_verify_gate_fail_and_say_so(run_cli, adopted):
    begin(adopted)
    set_exit_code(adopted, 1)
    verify_now(adopted, green=False)
    lines = problems(gate(run_cli, adopted, "verify"))
    sources = [line.split(": ", 1)[0] + ": " for line in lines]
    assert set(sources) == {TYPED, MEASURED} and sources == sorted(sources, key=[TYPED, MEASURED].index)  # the record's problems first, then the run's


def test_a_record_that_covers_another_change_than_the_one_the_commands_ran_on_is_the_measured_runs(run_cli, adopted):
    begin(adopted)
    put(adopted, "verify", verify_record(green=True, diff="f" * 64))
    [line] = problems(gate(run_cli, adopted, "verify"))
    assert line.startswith(MEASURED + "the record covers the change ffffffffffff, but the commands ran on the change ")


@pytest.mark.parametrize(
    "mutate,text",
    [
        (lambda r: r.update(commands=[]), "the verify record lists no commands, so nothing shows it verified anything"),
        (lambda r: r.update(failing_acs=[{"ac": "AC-1", "error_type": "AssertionError"}]), "green is true, but failing_acs lists AC-1"),
        (lambda r: r["tests"].update(failed=7), "green is true, but tests.failed is 7"),
        (lambda r: r["checks"].update(secrets=False), "green is true, but checks.secrets is false"),
    ],
)
def test_a_record_that_contradicts_itself_is_the_typed_part(run_cli, adopted, mutate, text):
    begin(adopted)
    record = verify_record(green=True, diff=now_hash(adopted))
    mutate(record)
    put(adopted, "verify", record)
    assert TYPED + text in problems(gate(run_cli, adopted, "verify"))


def test_status_shows_the_source_of_the_problem_of_a_verify_stage_the_commands_have_not_been_run_for(run_cli, adopted):
    begin(adopted)
    verify_now(adopted)
    out = run_cli("status", cwd=adopted).stdout
    assert f"      {MEASURED}the repository's commands have not been run: `plumbline.py gate r1 verify` runs them itself" in out


def test_a_refactor_that_touches_a_test_file_is_the_measured_runs_problem(run_cli, adopted):
    put(adopted, "intake", intake_record("code.S", adopted, intent="refactor"))
    write(adopted / "tests" / "test_new.py", "def test_x():\n    assert True\n")
    verify_now(adopted)
    [line] = problems(gate(run_cli, adopted, "verify"))
    assert line == MEASURED + "the intent refactor leaves the tests as they are, but the change touches tests/test_new.py"


def test_pass_names_the_source_of_the_first_problems_of_a_gate_it_refuses(run_cli, adopted):
    begin(adopted)
    write(adopted / "docs" / "guide.md", "# guide\n")
    commit_all(adopted, "a change")
    verify_now(adopted, green=False)
    out = run_cli("pass", RUN, cwd=adopted).stdout
    assert f"error: stage 'verify' (gate verify_green): {TYPED}the verify record is not green (failing: AC-1); {TYPED}" in out


# ------------------------------------------------------ the other gates and the ledger's problems say nothing of the two parts


def test_the_gates_that_have_one_part_and_the_ledgers_problems_carry_no_source(run_cli, adopted):
    begin(adopted, "code.M")
    put(adopted, "plan", {**spec_record(planned=("AC-1",))})  # AC-2 has no test plan entry
    plan_gate = gate(run_cli, adopted, "plan")
    assert "  - AC-2 has no test_plan entry" in plan_gate.stdout
    put(adopted, "review", review_record(blockers=1, diff=now_hash(adopted)))
    review_gate = gate(run_cli, adopted, "review")
    assert "  - 1 blocker(s) survive: F-1" in review_gate.stdout
    put(adopted, "verify", verify_record(green=True, diff=now_hash(adopted)), agent=False)  # written by hand: no agent stopped with it
    untraced = gate(run_cli, adopted, "verify")
    assert "  - its record has no entry from plumbline:verifier; run the verifier" in untraced.stdout
    for result in (plan_gate, review_gate, untraced):
        assert TYPED not in result.stdout and MEASURED not in result.stdout


def test_every_problem_of_a_failing_tests_or_verify_gate_has_a_source(run_cli, tested):
    scenarios = [
        ("tests", lambda: put(tested, "tests", written_tests_record(ran=False, all_failed=False))),
        ("tests", lambda: (set_exit_code(tested, 0), put(tested, "tests", written_tests_record(covering=(), ran=True, all_failed=False)))),
        ("verify", lambda: (set_exit_code(tested, 1), put(tested, "verify", verify_record(green=False, diff=now_hash(tested))))),
    ]
    for stage, arrange in scenarios:
        arrange()
        result = gate(run_cli, tested, stage)
        lines = problems(result)
        assert result.returncode in (1, 3) and lines, (stage, result.stdout)
        assert all(line.startswith((TYPED, MEASURED)) for line in lines), (stage, lines)
    ledger = [e for e in pl.read_ledger(tested, RUN) if e["kind"] == "gate"]
    assert ledger and all(p.startswith((TYPED, MEASURED)) for e in ledger for p in e["problems"])  # the ledger keeps them as they were printed
