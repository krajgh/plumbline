"""Measured gates: `gate` runs the repository's own [commands] for a verify stage (test, lint, typecheck, build) and for a tests stage (the
test command, which must fail), enters what happened in the ledger, and compares it with what the agent typed. A test run may not change
the files that steer git and the agents (TC-PERSIST), and a pytest run keeps the inventory of tests from shrinking."""
import shlex
import sys
import time
import tomllib

import pytest

import plumbline as pl
from helpers import commit_all, git, process_gone, write
from rundata import (
    CONTROLLED, RUN, adopt, begin, build_note_record, change_of, exits, ledger, now_hash, put,
    run_entry, run_path, set_exit_code, spec_record, verify_now, verify_record, write_docs_run, write_test_file, written_tests_record,
)


def repo_with(repo, **commands):
    """An adopted repository whose [commands] are `commands`, with a docs run begun."""
    adopt(repo, commands=commands)
    begin(repo, "docs")
    return repo


def gate(run_cli, repo, stage="verify", cwd=None):
    return run_cli("gate", RUN, stage, cwd=cwd or repo)


def run_entries(repo, stage="verify"):
    return [e for e in ledger(repo) if e["kind"] == "run" and e["stage"] == stage]


def failed_with(result, text, code=1):
    assert result.returncode == code, result.stdout + result.stderr
    assert "FAIL" in result.stdout and text in result.stdout, result.stdout


# --- the verify stage: the gate runs the repository's commands itself


def test_gate_runs_the_declared_test_command_itself_and_enters_the_run_in_the_ledger(run_cli, repo):  # C-04
    repo_with(repo, test="sh -c 'echo ran >> .plumbline/ran.txt'")
    verify_now(repo)
    result = gate(run_cli, repo)
    assert result.returncode == 0, result.stdout
    assert (repo / ".plumbline" / "ran.txt").read_text() == "ran\n"  # the command ran, once
    [entry] = run_entries(repo)
    assert entry["kind"] == "run" and entry["stage"] == "verify" and entry["gate"] == "verify_green"
    [command] = entry["commands"]
    assert (command["name"], command["cmd"], command["exit_code"]) == ("test", "sh -c 'echo ran >> .plumbline/ran.txt'", 0)
    assert isinstance(command["seconds"], float) and command["seconds"] >= 0
    assert entry["diff_sha256"] == now_hash(repo)  # the change as it was when the commands ran
    assert entry["guarded_before"] == entry["guarded_after"] and ".git/config" in entry["guarded_before"]


def test_each_gate_evaluation_runs_the_commands_again(run_cli, repo):
    repo_with(repo, test="sh -c 'echo ran >> .plumbline/ran.txt'")
    verify_now(repo)
    gate(run_cli, repo)
    gate(run_cli, repo)
    assert (repo / ".plumbline" / "ran.txt").read_text() == "ran\nran\n" and len(run_entries(repo)) == 2


def test_gate_runs_lint_typecheck_and_build_where_they_are_declared_after_the_test_command(run_cli, repo):
    repo_with(
        repo,
        test="sh -c 'echo test >> .plumbline/order.txt'", lint="sh -c 'echo lint >> .plumbline/order.txt'",
        typecheck="sh -c 'echo typecheck >> .plumbline/order.txt'", build="sh -c 'echo build >> .plumbline/order.txt'",
    )
    verify_now(repo)
    assert gate(run_cli, repo).returncode == 0
    assert (repo / ".plumbline" / "order.txt").read_text().split() == ["test", "lint", "typecheck", "build"]
    [entry] = run_entries(repo)
    assert [c["name"] for c in entry["commands"]] == ["test", "lint", "typecheck", "build"]


def test_gate_runs_only_the_commands_the_repository_declares(run_cli, repo):
    repo_with(repo, test=exits(0))
    verify_now(repo)
    gate(run_cli, repo)
    [entry] = run_entries(repo)
    assert [c["name"] for c in entry["commands"]] == ["test"]


def test_the_commands_run_from_the_repository_root_wherever_gate_is_run_from(run_cli, repo):
    repo_with(repo, test="sh -c 'pwd > .plumbline/cwd.txt'")
    verify_now(repo)
    assert gate(run_cli, repo, cwd=repo / "src").returncode == 0
    assert (repo / ".plumbline" / "cwd.txt").read_text().strip() == str(repo.resolve())


def test_only_the_first_prefix_of_a_list_is_run(run_cli, repo):
    adopt(repo)
    (repo / "plumbline.toml").write_text(
        (repo / "plumbline.toml").read_text() + '\n[commands]\ntest = ["sh -c \'echo one >> .plumbline/ran.txt\'", "sh -c \'echo two >> .plumbline/ran.txt\'"]\n', encoding="utf-8"
    )
    commit_all(repo, "a list of prefixes")
    begin(repo, "docs")
    verify_now(repo)
    assert gate(run_cli, repo).returncode == 0
    assert (repo / ".plumbline" / "ran.txt").read_text() == "one\n"


def test_a_verifier_that_says_green_while_the_test_command_fails_does_not_pass(run_cli, repo):  # C-04
    repo_with(repo, test=exits(1))
    verify_now(repo, green=True)
    result = gate(run_cli, repo)
    failed_with(result, "the test command exited 1 (sh -c 'exit 1')")
    assert "the verify record is not green" not in result.stdout  # the record is consistent; what it says is not what happened


def test_a_failing_lint_command_fails_the_gate_though_the_test_command_passes(run_cli, repo):
    repo_with(repo, test=exits(0), lint=exits(2))
    verify_now(repo)
    failed_with(gate(run_cli, repo), "the lint command exited 2 (sh -c 'exit 2')")


def test_a_command_that_could_not_run_fails_with_its_exit_status(run_cli, repo):
    repo_with(repo, test="a-command-that-does-not-exist-here")
    verify_now(repo)
    failed_with(gate(run_cli, repo), "the test command exited 127")


def test_the_gate_lists_every_failing_command_and_every_false_check(run_cli, repo):  # C-17
    repo_with(repo, test=exits(1), lint=exits(2))
    record = verify_record(green=False, diff=now_hash(repo))
    record["checks"]["abs_paths"] = False
    put(repo, "verify", record)
    out = gate(run_cli, repo).stdout
    assert "the verify record is not green (failing: AC-1)" in out and "the record's check abs_paths is false" in out
    assert "the test command exited 1" in out and "the lint command exited 2" in out


def test_without_a_declared_test_command_the_gate_says_what_to_add_and_runs_nothing(run_cli, repo):
    adopt(repo)
    begin(repo, "docs")
    verify_now(repo)
    result = gate(run_cli, repo)
    failed_with(result, "no test command is declared: add `test = \"...\"` under [commands] in plumbline.toml and commit it, then evaluate the gate again")
    assert run_entries(repo) == []


def test_a_verify_gate_needs_a_usable_intake_record_to_measure_the_change(run_cli, repo):
    adopt(repo, commands={"test": exits(0)})
    put(repo, "verify", verify_record(), agent=True)  # no run has begun
    result = gate(run_cli, repo)
    assert result.returncode == 2 and "the run's intake record is not usable" in result.stderr and "the change cannot be measured" in result.stderr


# --- a command that runs too long is stopped


def test_a_command_past_the_timeout_is_killed_with_what_it_started_and_fails_the_gate(run_cli, repo):
    repo_with(repo, test="sh -c 'sleep 30 & echo $! > .plumbline/child.pid; wait'", timeout=1)
    verify_now(repo)
    started = time.monotonic()
    result = gate(run_cli, repo)
    assert time.monotonic() - started < 20
    failed_with(result, "the test command timed out after 1 s (sh -c 'sleep 30 & echo $! > .plumbline/child.pid; wait'); `timeout` under [commands] in plumbline.toml allows more")
    [entry] = run_entries(repo)
    [command] = entry["commands"]
    assert command["timed_out"] is True and command["exit_code"] == 124 and entry["timeout"] == 1
    assert process_gone(int((repo / ".plumbline" / "child.pid").read_text()))  # the whole process group was killed


def test_ending_gate_ends_the_commands_it_started(run_cli, repo, home):
    import signal
    import subprocess

    from helpers import CLI, clean_env

    repo_with(repo, test="sh -c 'sleep 60 & echo $! > .plumbline/child.pid; wait'")
    verify_now(repo)
    process = subprocess.Popen([sys.executable, str(CLI), "gate", RUN, "verify"], cwd=str(repo), env=clean_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pid_file = repo / ".plumbline" / "child.pid"
    deadline = time.monotonic() + 15
    while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(pid_file.read_text())
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=15)
    assert process_gone(child)  # the command and what it started are gone with the gate


def test_the_default_timeout_is_900_seconds_and_the_config_may_set_another():
    assert pl.DEFAULT_COMMAND_TIMEOUT == 900
    assert pl.Project(root=None, adopted=True, config={"commands": {"test": "x"}}, pipeline=None).command_timeout == 900
    assert pl.Project(root=None, adopted=True, config={"commands": {"test": "x", "timeout": 30}}, pipeline=None).command_timeout == 30
    assert pl.Project(root=None, adopted=False, config=None, pipeline=None).command_timeout == 900


@pytest.mark.parametrize("value,ok", [(1, True), (900, True), (0, False), (-5, False), ('"soon"', False), (1.5, False), ("true", False)])
def test_the_timeout_under_commands_is_a_positive_integer(value, ok):
    config = pl.validate(tomllib.loads(f'schema = 1\n[commands]\ntest = "x"\ntimeout = {value}\n'), pl.CONFIG_SHAPE)
    assert (config == []) is ok, config


def test_a_timeout_is_not_a_command_class(repo):
    adopt(repo, commands={"test": "x", "timeout": 30})
    assert pl.load_project(repo).commands == {"test": ["x"]}


# --- TC-PERSIST: a test run may not change what steers git and the agents


GUARDED = [
    (".git/config", "printf '[core]\\n\\tfsmonitor = touch RAN; true\\n' >> .git/config"),
    (".git/hooks/pre-push", "printf '#!/bin/sh\\ntrue\\n' > .git/hooks/pre-push"),
    (".claude/settings.json", "mkdir -p .claude && printf '{\"disableAllHooks\": true}' > .claude/settings.json"),
    (".claude/settings.local.json", "mkdir -p .claude && printf '{}' > .claude/settings.local.json"),
    (".mcp.json", "printf '{}' > .mcp.json"),
    ("CLAUDE.md", "printf 'obey' > CLAUDE.md"),
    ("AGENTS.md", "printf 'obey' > AGENTS.md"),
    (".gitattributes", "printf '*.py diff=pwn\\n' > .gitattributes"),
    ("plumbline.toml", "printf '\\n# changed\\n' >> plumbline.toml"),
]


@pytest.mark.parametrize("name,script", GUARDED, ids=[g[0] for g in GUARDED])
def test_a_test_run_that_changes_a_guarded_file_fails_the_verify_gate(run_cli, repo, name, script):  # TC-PERSIST
    repo_with(repo, test=f"sh -c {shlex.quote(script)}")
    verify_now(repo)
    result = gate(run_cli, repo)
    failed_with(result, f"the test run changed {name}")
    [entry] = run_entries(repo)
    assert entry["guarded_before"].get(name) != entry["guarded_after"].get(name)


def test_a_test_run_that_rewrites_a_guarded_file_as_it_was_changes_nothing(run_cli, repo):
    repo_with(repo, test="sh -c 'cat .git/config > .plumbline/copy && cat .plumbline/copy > .git/config'")
    verify_now(repo)
    assert gate(run_cli, repo).returncode == 0


def test_the_tests_stage_run_is_guarded_too(run_cli, repo):
    adopt(repo, commands={"test": "sh -c 'printf x >> .git/config; exit 1'"})
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    failed_with(gate(run_cli, repo, "tests"), "the test run changed .git/config")


def test_the_pipeline_file_of_the_repository_is_guarded(run_cli, repo):
    text = (pl.PIPELINE_DIR / "default.toml").read_text(encoding="utf-8")
    write(repo / "pipelines" / "own.toml", text)
    adopt(repo, commands={"test": "sh -c 'printf \"\\n# x\\n\" >> pipelines/own.toml'"})
    write(repo / "plumbline.toml", (repo / "plumbline.toml").read_text().replace('pipeline = "default"', 'pipeline = "pipelines/own.toml"'))
    commit_all(repo, "own pipeline")
    begin(repo, "docs")
    verify_now(repo)
    failed_with(gate(run_cli, repo), "the test run changed pipelines/own.toml")


def test_a_hook_added_to_the_git_dir_of_a_linked_worktree_is_seen_in_the_common_dir(run_cli, repo, tmp_path):
    adopt(repo, commands={"test": exits(0)})
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "other", str(linked))
    project = pl.load_project(linked)
    before = pl.guarded_snapshot(project)
    (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    assert pl.guarded_snapshot(project)[".git/hooks/pre-commit"] != before.get(".git/hooks/pre-commit")


def test_a_hooks_directory_that_core_hooks_path_points_to_is_guarded(run_cli, repo, tmp_path):
    adopt(repo, commands={"test": exits(0)})
    hooks = tmp_path / "my-hooks"
    hooks.mkdir()
    git(repo, "config", "core.hooksPath", str(hooks))
    before = pl.guarded_snapshot(pl.load_project(repo))
    (hooks / "pre-push").write_text("#!/bin/sh\n", encoding="utf-8")
    after = pl.guarded_snapshot(pl.load_project(repo))
    assert [name for name in after if name not in before] == [".git/hooks/pre-push"]


def test_the_guarded_files_are_the_ones_the_spec_names(repo):
    adopt(repo, commands={"test": exits(0)})
    names = set(pl.guarded_snapshot(pl.load_project(repo)))
    assert {".git/config", ".gitattributes", ".claude/settings.json", ".claude/settings.local.json", ".mcp.json", "CLAUDE.md", "AGENTS.md", "plumbline.toml"} <= names
    assert any(name.startswith(".git/hooks/") for name in names)


# --- pass reads the measured run the ledger holds, and refuses one that does not fit


def test_pass_refuses_a_verify_record_no_gate_ever_measured(run_cli, repo):
    adopt(repo, commands={"test": exits(0)})
    write_docs_run(repo)
    ledger_path = run_path(repo, RUN, "ledger.jsonl")
    kept = [line for line in ledger_path.read_text(encoding="utf-8").splitlines() if '"kind": "run"' not in line]
    ledger_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    result = run_cli("pass", RUN, cwd=repo)
    assert result.returncode == 1 and "the repository's commands have not been run: `plumbline.py gate r1 verify` runs them itself" in result.stdout


def test_pass_does_not_run_the_commands_again(run_cli, repo):
    adopt(repo, commands={"test": "sh -c 'echo ran >> .plumbline/ran.txt'"})
    write_docs_run(repo)
    assert run_cli("pass", RUN, cwd=repo).returncode == 0
    assert not (repo / ".plumbline" / "ran.txt").exists()


def test_pass_refuses_a_measured_run_of_another_change_than_the_record_covers(run_cli, repo):
    adopt(repo, commands={"test": exits(0)})
    write_docs_run(repo)
    run_entry(repo, "verify", "e" * 64)  # the commands ran on some other change
    result = run_cli("pass", RUN, cwd=repo)
    assert result.returncode == 1 and "the commands ran on the change eeeeeeeeeeee" in result.stdout


def test_pass_refuses_when_a_measured_command_failed(run_cli, repo):
    adopt(repo, commands={"test": exits(0)})
    write_docs_run(repo)
    run_entry(repo, "verify", change_of(repo), exit_code=3)
    assert "the test command exited 3" in run_cli("pass", RUN, cwd=repo).stdout


def test_pass_refuses_when_a_guarded_file_changed_during_the_measured_run(run_cli, repo):
    adopt(repo, commands={"test": exits(0)})
    write_docs_run(repo)
    run_entry(repo, "verify", change_of(repo), before={".git/config": "a" * 64}, after={".git/config": "b" * 64})
    assert "the test run changed .git/config" in run_cli("pass", RUN, cwd=repo).stdout


def test_a_command_declared_differently_after_the_run_is_caught_at_pass(run_cli, repo):
    """The run was measured with `true`; the config then says something else: the measured run is not of the declared command."""
    adopt(repo, commands={"test": exits(0)})
    write_docs_run(repo)
    write(repo / "plumbline.toml", (repo / "plumbline.toml").read_text().replace(exits(0), exits(4)))
    commit_all(repo, "the test command is another")
    result = run_cli("pass", RUN, cwd=repo)
    assert result.returncode == 1 and f"the test command is declared as `{exits(4)}`, but the measured run did not run it: evaluate the gate again" in result.stdout


# --- the tests stage: the test command runs, and must fail


@pytest.fixture
def tested(repo):
    """A code.S run with the plan and the test file in place, and the test command whose status the test sets."""
    adopt(repo, commands={"test": CONTROLLED})
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    return repo


def fake_pytest(code):
    """A command that ends with `code` and, to the runner, is a pytest run: pytest is a word of it. The junit flag lands in a shell comment."""
    return f"sh -c 'exit {code}' # pytest"


@pytest.mark.parametrize(
    "code,verdict",
    [
        (1, None),
        (0, "the test command exited 0: every test passed, so none of them fails without the change it tests"),
        (2, "the test command exited 2: pytest could not collect the tests (an import or syntax error), and a test that cannot run reproduces nothing"),
        (5, "the test command exited 5: pytest collected no tests"),
        (3, "the test command exited 3: pytest did not report failing tests (a failing test exits 1)"),
        (4, "the test command exited 4: pytest did not report failing tests (a failing test exits 1)"),
        (127, "the test command could not run (exit status 127)"),
    ],
)
def test_a_pytest_run_of_the_tests_stage_must_exit_1(run_cli, repo, code, verdict):  # C-05
    adopt(repo, commands={"test": fake_pytest(code)})
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    result = gate(run_cli, repo, "tests")
    if verdict is None:
        assert result.returncode == 0, result.stdout
    else:
        failed_with(result, verdict)


@pytest.mark.parametrize("code,passes", [(0, False), (1, True), (2, True), (7, True), (126, False), (127, False)])
def test_another_test_command_fails_with_any_status_but_0_and_the_two_that_say_it_did_not_run(run_cli, repo, code, passes):
    adopt(repo, commands={"test": exits(code)})
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    assert (gate(run_cli, repo, "tests").returncode == 0) is passes


def test_the_tests_gate_enters_its_run_in_the_ledger(run_cli, tested):
    set_exit_code(tested, 1)
    assert gate(run_cli, tested, "tests").returncode == 0
    [entry] = run_entries(tested, "tests")
    assert entry["gate"] == "tests_fail_on_stub" and [c["name"] for c in entry["commands"]] == ["test"]
    assert entry["commands"][0]["exit_code"] == 1 and entry["guarded_before"] == entry["guarded_after"]


def test_the_tests_gate_runs_only_the_test_command(run_cli, repo):
    adopt(repo, commands={"test": "sh -c 'echo test >> .plumbline/order.txt; exit 1'", "lint": "sh -c 'echo lint >> .plumbline/order.txt'"})
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    assert gate(run_cli, repo, "tests").returncode == 0
    assert (repo / ".plumbline" / "order.txt").read_text().split() == ["test"]


def test_without_a_test_command_the_tests_gate_says_what_to_add(run_cli, repo):
    adopt(repo)
    begin(repo, "code.S")
    write_test_file(repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    failed_with(gate(run_cli, repo, "tests"), "no test command is declared")


def test_the_typed_stub_check_must_agree_with_the_measured_run(run_cli, tested):
    set_exit_code(tested, 1)
    put(tested, "tests", written_tests_record(ran=False, all_failed=False))
    failed_with(gate(run_cli, tested, "tests"), "the stub check did not run")
    put(tested, "tests", written_tests_record(ran=True, all_failed=False))  # the third stop of the test-writer: its last round
    failed_with(gate(run_cli, tested, "tests"), "not every test failed on an assertion against the stubs", code=3)


# --- the tests stage run again, once the code exists


def build_and_pass_the_tests_gate(run_cli, repo):
    set_exit_code(repo, 1)
    assert gate(run_cli, repo, "tests").returncode == 0
    set_exit_code(repo, 0)
    put(repo, "build", build_note_record())  # the builder ran: the code exists now


def test_the_tests_stage_run_again_after_the_build_is_recorded_and_not_expected_to_fail(run_cli, tested):
    build_and_pass_the_tests_gate(run_cli, tested)
    put(tested, "tests", written_tests_record(ran=False, all_failed=False))  # revised after a review; nothing to say about stubs now
    result = gate(run_cli, tested, "tests")
    assert result.returncode == 0, result.stdout
    assert [e["commands"][0]["exit_code"] for e in run_entries(tested, "tests")] == [1, 0]


def test_a_revision_still_has_to_cover_the_spec_from_real_test_files(run_cli, tested):
    build_and_pass_the_tests_gate(run_cli, tested)
    record = written_tests_record()
    record["tests"][0]["file"] = "tests/test_ghost.py"
    put(tested, "tests", record)
    failed_with(gate(run_cli, tested, "tests"), "test T-1: the file tests/test_ghost.py does not exist")


def test_a_revision_may_not_change_a_guarded_file_either(run_cli, tested):
    build_and_pass_the_tests_gate(run_cli, tested)
    write(tested / "plumbline.toml", (tested / "plumbline.toml").read_text().replace(CONTROLLED, "sh -c 'printf x >> .git/config; exit 0'"))
    put(tested, "tests", written_tests_record())
    failed_with(gate(run_cli, tested, "tests"), "the test run changed .git/config")


def test_a_tests_stage_that_never_passed_before_the_build_gets_no_allowance(run_cli, tested):
    """Build first, tests afterwards: the tests that pass are the tests that fail on nothing, and the gate says so."""
    put(tested, "build", build_note_record())
    set_exit_code(tested, 0)
    failed_with(gate(run_cli, tested, "tests"), "the test command exited 0: every test passed")


def test_tests_revision_reads_the_order_of_the_ledger(tested):
    run = pl.load_run(pl.load_project(tested), RUN)
    assert pl.tests_revision(run, "tests", ledger(tested)) is False  # no builder has stopped
    rows = [{"kind": "gate", "stage": "tests", "passed": True}, {"kind": "agent", "stage": "build"}]
    assert pl.tests_revision(run, "tests", rows) is True
    assert pl.tests_revision(run, "tests", list(reversed(rows))) is False
    assert pl.tests_revision(run, "tests", [{"kind": "gate", "stage": "tests", "passed": False}, {"kind": "agent", "stage": "build"}]) is False
    assert pl.tests_revision(None, "tests", rows) is False


# --- the inventory: the verify run's tests may not be fewer, nor more of them skipped, than the tests stage's


def inventory_repo(repo, tests_junit, verify_junit):
    adopt(repo, commands={"test": fake_pytest(0)})
    begin(repo, "code.S")
    write_test_file(repo)
    commit_all(repo, "the tests")
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    run_entry(repo, "tests", None, exit_code=1, junit=tests_junit)
    put(repo, "build", build_note_record())
    verify_now(repo)
    run_entry(repo, "verify", now_hash(repo), junit=verify_junit)


def inventory_problems(repo):
    project = pl.load_project(repo)
    run = pl.load_run(project, RUN)
    verify = next(s for s in run.stages if s["id"] == "verify")
    return pl.evaluate_stage(project, RUN, verify, run=run).problems


def counts(tests, failures=0, errors=0, skipped=0):
    return {"tests": tests, "failures": failures, "errors": errors, "skipped": skipped}


def test_a_verify_run_of_as_many_tests_passes_the_inventory(repo):
    inventory_repo(repo, counts(10, failures=2), counts(10))
    assert inventory_problems(repo) == []


def test_a_verify_run_of_more_tests_passes_the_inventory(repo):
    inventory_repo(repo, counts(10, failures=2, skipped=1), counts(14, skipped=1))
    assert inventory_problems(repo) == []


def test_a_verify_run_of_fewer_tests_fails_the_inventory(repo):  # TC-PERSIST
    inventory_repo(repo, counts(10, failures=2), counts(8))
    assert "the verify run ran 8 tests, fewer than the 10 the tests stage's run ran: a test went missing" in inventory_problems(repo)


def test_a_verify_run_that_skips_more_tests_fails_the_inventory(repo):  # TC-PERSIST
    inventory_repo(repo, counts(10, failures=2, skipped=1), counts(10, skipped=3))
    assert "the verify run skipped 3 tests, more than the 1 the tests stage's run skipped: a test was marked skipped" in inventory_problems(repo)


def test_the_inventory_is_compared_only_when_both_runs_have_one(repo):
    inventory_repo(repo, None, counts(1))  # the tests stage's command reported no junit
    assert inventory_problems(repo) == []
    run_entry(repo, "tests", None, exit_code=1, junit=counts(10, failures=2))  # now it did, and the verify run did not
    run_entry(repo, "verify", now_hash(repo), junit=None)
    assert inventory_problems(repo) == []


def test_the_inventory_uses_the_latest_run_of_the_tests_stage(repo):
    inventory_repo(repo, counts(10, failures=2), counts(9))
    run_entry(repo, "tests", None, exit_code=1, junit=counts(9, failures=2))  # the test-writer's later round has fewer tests
    assert inventory_problems(repo) == []


def test_a_run_without_a_tests_stage_has_no_inventory(repo):
    adopt(repo, commands={"test": fake_pytest(0)})
    write_docs_run(repo)
    project = pl.load_project(repo)
    verify = next(s for s in pl.load_run(project, RUN).stages if s["id"] == "verify")
    assert pl.evaluate_stage(project, RUN, verify).passed


# --- pytest commands get a junit report, and only they


@pytest.mark.parametrize(
    "command,expected",
    [
        ("pytest", True), ("pytest -q", True), ("python -m pytest", True), ("python3 -m pytest -x tests/", True), ("uv run pytest", True),
        ("uv run --with pytest pytest -q -p no:cacheprovider", True), ("/venv/bin/pytest -q", True), ("env X=1 pytest", True), ("py.test", True),
        ("make test", False), ("sh run_tests.sh", False), ("npm test", False), ("python -m unittest", False), ("echo hello", False), ("cargo test", False),
        ("pytester --help", False), ("python -m pytester", False), ("pytest 'unbalanced", False), ("", False),
    ],
)
def test_which_commands_are_pytest(command, expected):
    assert pl.is_pytest(command) is expected


def test_a_pytest_command_gets_the_junit_flag_appended_and_the_others_do_not(run_cli, repo):
    # the command prints its own arguments: `_ pytest` makes it a pytest command to the runner, and the flag is appended to the line
    repo_with(repo, test="sh -c 'echo $@ > .plumbline/argv.txt' _ pytest")
    verify_now(repo)
    gate(run_cli, repo)
    assert (repo / ".plumbline" / "argv.txt").read_text().split() == ["pytest", f"--junitxml={repo / '.plumbline' / 'runs' / RUN / 'junit-verify.xml'}"]
    plain = repo / "plumbline.toml"
    plain.write_text(plain.read_text().replace("_ pytest", "_ plain"))
    commit_all(repo, "a command that is no pytest run")
    begin(repo, "docs")
    verify_now(repo)
    gate(run_cli, repo)
    assert (repo / ".plumbline" / "argv.txt").read_text().split() == ["plain"]


def real_pytest_command():
    return f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"


def pytest_repo(repo, *, failing):
    """A repository whose test command is a real pytest run over tests/test_app.py, with `failing` tests failing."""
    write(repo / ".gitignore", ".plumbline/\n__pycache__/\n")
    adopt(repo, commands={"test": real_pytest_command()})
    body = "".join(f"def test_case_{n}():\n    assert {n >= failing}\n\n" for n in range(3))
    write(repo / "tests" / "test_app.py", body)
    return repo


def test_a_real_pytest_run_records_its_junit_counts(run_cli, repo):
    pytest_repo(repo, failing=0)
    commit_all(repo, "tests")
    begin(repo, "docs")
    verify_now(repo)
    result = gate(run_cli, repo)
    assert result.returncode == 0, result.stdout
    [entry] = run_entries(repo)
    assert entry["commands"][0]["junit"] == {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}
    assert (repo / ".plumbline" / "runs" / RUN / "junit-verify.xml").is_file()


def test_a_real_pytest_run_of_failing_tests_exits_1_and_passes_the_tests_gate(run_cli, repo):
    pytest_repo(repo, failing=2)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    record = written_tests_record(covering=("AC-1", "AC-2"))
    for test, name in zip(record["tests"], ("test_ac_1", "test_ac_2")):
        test["file"], test["name"] = "tests/test_app.py", name
    write(repo / "tests" / "test_app.py", "def test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n\ndef test_other():\n    assert True\n")
    put(repo, "tests", record)
    result = gate(run_cli, repo, "tests")
    assert result.returncode == 0, result.stdout
    [entry] = run_entries(repo, "tests")
    assert entry["commands"][0]["exit_code"] == 1 and entry["commands"][0]["junit"] == {"tests": 3, "failures": 2, "errors": 0, "skipped": 0}


def test_a_real_pytest_run_that_cannot_collect_its_tests_exits_2_and_fails_the_tests_gate(run_cli, repo):  # C-05
    pytest_repo(repo, failing=0)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    write(repo / "tests" / "test_app.py", "import module_that_is_not_there\n\ndef test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n")
    put(repo, "tests", written_tests_record())
    result = gate(run_cli, repo, "tests")
    failed_with(result, "the test command exited 2: pytest could not collect the tests")


def test_a_real_pytest_run_that_finds_no_tests_exits_5_and_fails_the_tests_gate(run_cli, repo):
    pytest_repo(repo, failing=0)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    write(repo / "tests" / "test_app.py", "# def test_ac_1 and def test_ac_2 are commented out\n")
    put(repo, "tests", written_tests_record())
    failed_with(gate(run_cli, repo, "tests"), "the test command exited 5: pytest collected no tests")


def test_a_real_pytest_inventory_catches_a_deleted_test_between_the_tests_stage_and_verify(run_cli, repo):  # TC-PERSIST
    pytest_repo(repo, failing=0)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    write(repo / "tests" / "test_app.py", "def test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n\ndef test_third():\n    assert False\n")
    put(repo, "tests", written_tests_record())
    assert gate(run_cli, repo, "tests").returncode == 0
    # the builder makes the tests pass, and one of them goes missing on the way
    write(repo / "tests" / "test_app.py", "def test_ac_1():\n    assert True\n\ndef test_ac_2():\n    assert True\n")
    put(repo, "build", build_note_record())
    verify_now(repo)
    result = gate(run_cli, repo)
    failed_with(result, "the verify run ran 2 tests, fewer than the 3 the tests stage's run ran: a test went missing")


def test_a_real_pytest_inventory_catches_a_test_marked_skipped(run_cli, repo):  # TC-PERSIST
    pytest_repo(repo, failing=0)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    write(repo / "tests" / "test_app.py", "def test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n")
    put(repo, "tests", written_tests_record())
    assert gate(run_cli, repo, "tests").returncode == 0
    write(repo / "tests" / "test_app.py", "import pytest\n\n@pytest.mark.skip\ndef test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert True\n")
    put(repo, "build", build_note_record())
    verify_now(repo)
    failed_with(gate(run_cli, repo), "the verify run skipped 1 tests, more than the 0 the tests stage's run skipped: a test was marked skipped")


def test_a_conftest_that_skips_every_test_is_caught_by_the_inventory(run_cli, repo):  # WP-TESTCFG, the detection side: the reviewer's reproduction
    pytest_repo(repo, failing=0)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    write(repo / "tests" / "test_app.py", "def test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n")
    put(repo, "tests", written_tests_record())
    assert gate(run_cli, repo, "tests").returncode == 0  # two tests, both failing
    write(  # the builder's way out: a root conftest.py that skip-marks every test
        repo / "conftest.py",
        "import pytest\n\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.skip(reason='later'))\n",
    )
    put(repo, "build", build_note_record())
    verify_now(repo)
    result = gate(run_cli, repo)
    failed_with(result, "the verify run skipped 2 tests, more than the 0 the tests stage's run skipped: a test was marked skipped")
    assert "the test command exited" not in result.stdout  # pytest itself says all is well: exit 0, two skipped


def test_a_conftest_that_deselects_tests_is_caught_by_the_inventory_too(run_cli, repo):  # WP-TESTCFG
    pytest_repo(repo, failing=0)
    begin(repo, "code.S")
    put(repo, "plan", spec_record())
    write(repo / "tests" / "test_app.py", "def test_ac_1():\n    assert False\n\ndef test_ac_2():\n    assert False\n")
    put(repo, "tests", written_tests_record())
    assert gate(run_cli, repo, "tests").returncode == 0
    write(repo / "conftest.py", "def pytest_collection_modifyitems(items):\n    items[:] = items[:1]\n")
    put(repo, "build", build_note_record())
    verify_now(repo)
    failed_with(gate(run_cli, repo), "the verify run ran 1 tests, fewer than the 2 the tests stage's run ran: a test went missing")


# --- reading a junit file


JUNIT_NEW = '<?xml version="1.0"?><testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="2" skipped="3" tests="10"></testsuite></testsuites>'
JUNIT_OLD = '<testsuite name="pytest" errors="0" failures="1" skipped="0" tests="4"></testsuite>'
JUNIT_NESTED = '<testsuites><testsuite tests="5" failures="1"><testsuite tests="2" failures="1"/><testsuite tests="3"/></testsuite></testsuites>'


@pytest.mark.parametrize(
    "text,expected",
    [
        (JUNIT_NEW, {"tests": 10, "failures": 2, "errors": 1, "skipped": 3}),
        (JUNIT_OLD, {"tests": 4, "failures": 1, "errors": 0, "skipped": 0}),
        (JUNIT_NESTED, {"tests": 5, "failures": 1, "errors": 0, "skipped": 0}),  # a nested suite is counted once
        ("<testsuites/>", None),
        ("not xml", None),
        ('<testsuite tests="many"/>', None),
        ("", None),
    ],
)
def test_read_junit(tmp_path, text, expected):
    path = tmp_path / "junit.xml"
    path.write_text(text, encoding="utf-8")
    assert pl.read_junit(path) == expected


def test_a_junit_file_that_is_missing_or_huge_reads_as_none(tmp_path):
    assert pl.read_junit(tmp_path / "nope.xml") is None
    big = tmp_path / "big.xml"
    big.write_bytes(b"<testsuite tests='1'/>" + b" " * (9 << 20))
    assert pl.read_junit(big) is None


def test_a_stale_junit_file_is_not_mistaken_for_the_fresh_one(run_cli, repo):
    repo_with(repo, test="sh -c 'exit 0' # pytest")  # a pytest command that writes no junit file
    junit = run_path(repo, RUN, "junit-verify.xml")
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.write_text(JUNIT_NEW, encoding="utf-8")  # left by an earlier run
    verify_now(repo)
    gate(run_cli, repo)
    [entry] = run_entries(repo)
    assert "junit" not in entry["commands"][0] and not junit.exists()
