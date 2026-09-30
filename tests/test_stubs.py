"""Stubs live in the run: `.plumbline/runs/<run-id>/stubs/` is where the test-writer puts the stubs of a brand-new module. It is outside the
change, so the size measure and the commit never see it. The hook lets the test-writer write the active run's directory and no other, the
builder is blind to it like the tests, the test-writer's own stub run passes the commands rule, and the way the tests are run against the
stubs (or against today's code, with the new names imported inside the tests) is the way pytest behaves. The hook is called in process
here, with PLUMBLINE_HOOK_DEBUG set, so an error inside a rule fails the test instead of passing for an allow."""
import json
import os
import shlex
import subprocess
import sys

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import DEFAULT_TOML, commit_all, git, numbered, write
from hookdata import activate_run, bash_payload, start_run, tool_payload
from rundata import RUN, adopt, adopt_base, begin, ledger, put, run_path, spec_record, written_tests_record

STUBS = ".plumbline/runs/r1/stubs"
BUILDER = "plumbline:builder"


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


@pytest.fixture
def started(adopted):
    """Run r1 is in progress and named by ACTIVE."""
    start_run(adopted)
    return adopted


def writes(repo, role, path, tool="Write"):
    """The reason an agent of `role` is denied a write to `path`, or None."""
    target = path if os.path.isabs(str(path)) else str(repo / path)
    payload = tool_payload(repo, tool, {"file_path": target, "content": "x", "old_string": "a", "new_string": "b"}, agent_type=f"plumbline:{role}")
    return pre.decide(payload)


def runs(repo, role, command):
    return pre.decide(bash_payload(repo, command, agent_type=f"plumbline:{role}"))


def reads(repo, tool, tool_input, agent_type=BUILDER):
    return pre.decide(tool_payload(repo, tool, tool_input, agent_type=agent_type))


def configure(repo, text):
    with open(repo / "plumbline.toml", "a", encoding="utf-8") as handle:
        handle.write(text)
    commit_all(repo, "configure")


# ------------------------------------------------------------ the lane: the test-writer writes the active run's stubs directory


def test_the_test_writer_writes_files_under_the_active_runs_stubs_directory(started):
    for path in (f"{STUBS}/newmod.py", f"{STUBS}/pkg/deep/newmod.py", f"{STUBS}/data.json", f"{STUBS}/.hidden"):
        for tool in ("Write", "Edit"):
            assert writes(started, "test-writer", path, tool) is None, (tool, path)


def test_another_runs_stubs_are_denied_and_the_denial_names_the_active_one(started):
    start_run(started, "r2", activate=False)
    reason = writes(started, "test-writer", ".plumbline/runs/r2/stubs/newmod.py")
    assert reason == "plumbline: the test-writer writes stubs in the active run's stubs directory (.plumbline/runs/r1/stubs/); .plumbline/runs/r2/stubs/newmod.py belongs to run r2."
    assert "belongs to run gone" in writes(started, "test-writer", ".plumbline/runs/gone/stubs/newmod.py")  # a run that is not there is no run of this checkout either
    activate_run(started, "r2")  # the active run decides
    assert writes(started, "test-writer", ".plumbline/runs/r2/stubs/newmod.py") is None
    assert "belongs to run r1" in writes(started, "test-writer", f"{STUBS}/newmod.py")


@pytest.mark.parametrize(
    "path",
    [
        ".plumbline/runs/r1/stubs", ".plumbline/runs/r1/stubs-2/a.py", ".plumbline/runs/r1/notes.txt", ".plumbline/runs/r1/other/stubs/a.py",
        ".plumbline/runs/r1/round-1/stubs/a.py", ".plumbline/runs/stubs/a.py", ".plumbline/stubs/a.py", ".plumbline/supplied-spec.json",
        ".plumbline/runs/r1/build.json", ".plumbline/runs/r1/verify.json", ".PLUMBLINE/runs/r1/stubs/a.py",
    ],
)
def test_the_rest_of_plumbline_stays_closed_to_the_test_writer(started, path):
    reason = writes(started, "test-writer", path)
    assert reason and "the test-writer writes only its own record (.plumbline/runs/<run-id>/tests.json) and stubs (.plumbline/runs/<run-id>/stubs/)" in reason


@pytest.mark.parametrize("path", [".plumbline/pass/x.json", ".plumbline/runs/r1/ledger.jsonl", ".plumbline/runs/ACTIVE"])
def test_the_protected_files_stay_protected_from_the_test_writer(started, path):
    reason = writes(started, "test-writer", path)
    assert reason and "is written only by plumbline.py commands" in reason


def test_a_stub_needs_a_run_in_progress(adopted):
    reason = writes(adopted, "test-writer", f"{STUBS}/newmod.py")
    assert reason and "no run is in progress" in reason and "stubs directory" in reason and "plumbline.py plan --intent" in reason


@pytest.mark.parametrize("role", ["planner", "builder", "verifier", "prosecutor", "defender", "detective"])
def test_no_other_agent_writes_a_stub(started, role):
    reason = writes(started, role, f"{STUBS}/newmod.py")
    assert reason and f"the {role} writes" in reason and "stubs" not in reason.split(";")[0]


def test_the_main_session_writes_the_stubs_directory_like_any_other_file(started):
    payload = tool_payload(started, "Write", {"file_path": str(started / STUBS / "newmod.py"), "content": "x"})
    assert pre.decide(payload) is None


def test_a_link_out_of_the_stubs_directory_counts_as_where_it_leads(started):
    stubs = started / ".plumbline" / "runs" / "r1" / "stubs"
    stubs.mkdir(parents=True)
    os.symlink("../../../../src", stubs / "escape")  # the repository's src/
    reason = writes(started, "test-writer", f"{STUBS}/escape/evil.py")
    assert reason and "src/evil.py is neither" in reason


def test_the_stubs_target_is_data_so_a_pipeline_can_withhold_it(started):
    custom = DEFAULT_TOML.read_text().replace('writes = ["record", "tests", "stubs"]', 'writes = ["record", "tests"]')
    assert custom != DEFAULT_TOML.read_text()
    write(started / "pipelines" / "house.toml", custom)
    write(started / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(started, "own pipeline")
    reason = writes(started, "test-writer", f"{STUBS}/newmod.py")
    assert reason and "writes only its own record (.plumbline/runs/<run-id>/tests.json); " in reason and "another of plumbline's files" in reason
    assert writes(started, "test-writer", "tests/test_new.py") is None


def test_a_pipeline_can_give_the_target_to_another_role(started):
    custom = DEFAULT_TOML.read_text().replace('[roles.planner]\nwrites = ["record"]', '[roles.planner]\nwrites = ["record", "stubs"]')
    assert custom != DEFAULT_TOML.read_text()
    write(started / "pipelines" / "house.toml", custom)
    write(started / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(started, "own pipeline")
    assert writes(started, "planner", f"{STUBS}/newmod.py") is None
    assert "belongs to run r2" in writes(started, "planner", ".plumbline/runs/r2/stubs/newmod.py")


# --------------------------------------------- the test-writer's own stub run passes the command-prefix rule


@pytest.mark.parametrize("declared", ["python3 -m pytest", "pytest", "uv run pytest", "uv run --with pytest pytest"])
def test_the_test_writers_stub_run_is_the_test_command_followed_by_the_pythonpath_option(started, declared):
    configure(started, f'\n[commands]\ntest = "{declared}"\n')
    for command in (
        f'{declared} -q -o pythonpath="{STUBS} ."',
        f"{declared} -o pythonpath='{STUBS} .' tests/test_new.py::test_adds",
        f"PYTHONPATH={STUBS} {declared} -q",
        f'{declared} -q -o pythonpath="{STUBS} ." 2>&1 | tail -20',
        f'cd {started} && {declared} -q -o pythonpath="{STUBS} ."',
    ):
        assert runs(started, "test-writer", command) is None, command
    assert runs(started, "verifier", f'{declared} -q -o pythonpath="{STUBS} ."') is None  # the verifier has the test command too


def test_the_stub_run_is_refused_to_a_role_without_the_test_command_and_to_a_line_that_is_not_the_test_command(started):
    configure(started, '\n[commands]\ntest = "python3 -m pytest"\n')
    for role in ("planner", "builder", "defender", "prosecutor"):
        assert runs(started, role, f'python3 -m pytest -q -o pythonpath="{STUBS} ."'), role
    assert runs(started, "test-writer", f'python3 -c "print(1)" -o pythonpath="{STUBS} ."')
    assert runs(started, "test-writer", f'python3 -m pytest -q -o pythonpath="{STUBS} ." > out.txt')  # and Bash writes no file, stubs or not
    assert runs(started, "test-writer", f"cp x.py {STUBS}/newmod.py")  # the stubs are written with Write or Edit, like every other file


# ------------------------------------------------------------- the builder is blind to the stubs, as it is to the tests


@pytest.fixture
def with_stubs(started):
    """Run r1 with the test-writer's record, which lists one stub, and two stubs in the run's stubs directory."""
    put(started, "plan", spec_record())
    record = written_tests_record()
    record["files_written"] = ["tests/test_app.py", f"{STUBS}/listed.py"]
    put(started, "tests", record)
    write(started / STUBS / "listed.py", "def add(a, b):\n    return 0\n")
    write(started / STUBS / "pkg" / "unlisted.py", "def sub(a, b):\n    return 0\n")
    write(started / "src" / "app.py", "def main():\n    return 1\n")
    return started


@pytest.mark.parametrize("name", ["listed.py", "pkg/unlisted.py", "not_there_yet.py"])
def test_the_builder_does_not_read_a_stub(with_stubs, name):
    reason = reads(with_stubs, "Read", {"file_path": str(with_stubs / STUBS / name)})
    assert reason and f"{STUBS}/{name}" in reason


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
@pytest.mark.parametrize("path", [STUBS, f"{STUBS}/pkg", f"{STUBS}/listed.py", ".plumbline/runs/r1", ".plumbline/runs", ".plumbline"])
def test_the_builder_does_not_search_or_list_the_stubs_directory_or_a_directory_above_it(with_stubs, tool, path):
    assert reads(with_stubs, tool, {"pattern": "add", "path": path}), (tool, path)


def test_a_glob_pattern_that_leads_into_the_stubs_is_denied(with_stubs):
    for pattern, path in ((".plumbline/runs/r1/stubs/**", "."), ("../.plumbline/runs/*/stubs/*.py", "src"), (str(with_stubs / STUBS / "**"), "src")):
        assert reads(with_stubs, "Glob", {"pattern": pattern, "path": path}), (pattern, path)


def test_the_builder_still_searches_the_source_and_the_other_agents_read_the_stubs(with_stubs):
    assert reads(with_stubs, "Grep", {"pattern": "main", "path": "src"}) is None
    assert reads(with_stubs, "Glob", {"pattern": "*.py", "path": "src"}) is None
    for agent in ("plumbline:test-writer", "plumbline:verifier", "plumbline:prosecutor", "plumbline:detective", None):
        assert reads(with_stubs, "Read", {"file_path": str(with_stubs / STUBS / "listed.py")}, agent_type=agent) is None, agent


def test_the_builder_is_denied_the_stubs_through_the_hook_process_too(run_pre, with_stubs):
    from hookdata import denial

    payload = tool_payload(with_stubs, "Read", {"file_path": str(with_stubs / STUBS / "pkg" / "unlisted.py")}, agent_type=BUILDER)
    assert "under .plumbline/ the builder reads the active run's" in denial(run_pre(payload, with_stubs))


# ------------------------------------------ the stubs are outside the change: no size, no listed file, no hash, no dirty tree


@pytest.fixture
def measured(repo):
    """An adopted repository whose base already has the adoption, run r1 begun, and 40 changed lines of source."""
    adopt_base(repo)
    start_run(repo)
    write(repo / "src" / "feature.py", numbered(40))
    return repo


def classified(repo):
    return pl.classify(repo, pl.load_project(repo).pipeline, "main")


def test_a_stub_in_the_run_is_neither_counted_by_the_size_measure_nor_listed_as_a_change(measured):
    before = classified(measured)
    hashed = pl.change_hash(measured, before["merge_base"])
    write(measured / STUBS / "newmod.py", numbered(300))
    write(measured / STUBS / "pkg" / "other.py", numbered(300))
    after = classified(measured)
    assert (before["lines"], before["size"], before["row"]) == (after["lines"], after["size"], after["row"]) == (40, "S", "code.S")
    assert [f["path"] for f in after["files"]] == [f["path"] for f in before["files"]] == ["src/feature.py"]
    assert pl.change_hash(measured, after["merge_base"]) == hashed  # the hash that the verifier and the reviewers copy is the same
    assert pl.dirty_paths(measured) == ["src/feature.py"]  # `pass` needs a clean tree, and the stubs do not dirty it


def test_the_stubs_are_left_out_even_where_gitignore_does_not_name_plumbline(repo):
    adopt_base(repo)
    write(repo / ".gitignore", "__pycache__/\n")  # this repository's own file: it does not ignore .plumbline/
    commit_all(repo, "ignore less")
    git(repo, "branch", "-f", "main", "HEAD")
    start_run(repo)
    write(repo / "src" / "feature.py", numbered(40))
    write(repo / STUBS / "newmod.py", numbered(300))
    assert ".plumbline/runs/r1/stubs/newmod.py" in git(repo, "status", "--porcelain", "--untracked-files=all")  # git does see it, so the exclusion is plumbline's own
    after = classified(repo)
    assert (after["lines"], after["size"]) == (40, "S") and [f["path"] for f in after["files"]] == ["src/feature.py"]
    assert pl.dirty_paths(repo) == ["src/feature.py"]


def test_a_stub_left_under_a_test_path_is_the_change_and_counts(measured):
    write(measured / "tests" / "_stubs" / "newmod.py", numbered(300))  # where the 0.4.1 prompt put it: the reason the stubs moved
    after = classified(measured)
    assert (after["lines"], after["size"]) == (340, "M") and "tests/_stubs/newmod.py" in [f["path"] for f in after["files"]]


def test_check_diff_reads_the_same_change_with_or_without_the_stubs(run_cli, measured):
    def report():
        result = run_cli("check-diff", "--run", "r1", cwd=measured)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    before = report()
    write(measured / STUBS / "newmod.py", numbered(600))  # with the stubs counted this change measured as size L, and nothing is built at L
    after = report()
    assert after["diff_sha256"] == before["diff_sha256"]
    assert after["row"] == {"declared": "code.S", "measured": "code.S", "missing_stages": []} and after["problems"] == []


# ------------------------------------------------------------------------------------------------ the plan names the directory


def test_the_plan_names_the_stubs_directory_of_the_run(run_cli, repo):
    write(repo / "src" / "new_module.py", numbered(30))
    result = run_cli("plan", "--base", "main", "--run-id", "demo", cwd=repo)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["stubs_dir"] == ".plumbline/runs/demo/stubs" and plan["stubs_dir"] == f"{plan['record_dir']}/{pl.STUBS_DIR}"


# ------------------------------------------------ the way the tests are run: what pytest does with imports and with -o pythonpath


def pytest_in(project, *extra):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra], cwd=project, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    write(root / "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["."]\n')  # as the repository in the first real run had
    write(root / "tests" / "test_new.py", "def test_adds():\n    from newmod import add\n    assert add(1, 2) == 3\n")
    return root


def test_a_name_imported_inside_the_test_fails_when_the_test_runs_and_the_run_exits_1(project):
    today = pytest_in(project)
    assert today.returncode == 1 and "ModuleNotFoundError" in today.stdout and "1 failed" in today.stdout  # a failing test, not a collection error


def test_an_import_at_the_top_of_the_file_stops_the_collection_and_the_run_exits_2(project):
    write(project / "tests" / "test_top.py", "from newmod import add\n\n\ndef test_adds():\n    assert add(1, 2) == 3\n")
    top = pytest_in(project, "tests/test_top.py")
    assert top.returncode == 2 and "error" in top.stdout.lower()


def test_the_documented_form_puts_the_stubs_first_and_the_test_fails_on_an_assertion(project):
    write(project / STUBS / "newmod.py", "def add(a, b):\n    return 0\n")
    stubbed = pytest_in(project, "-o", f"pythonpath={STUBS} .")
    assert stubbed.returncode == 1 and "assert 0 == 3" in stubbed.stdout and "ModuleNotFoundError" not in stubbed.stdout
    assert "ModuleNotFoundError" in pytest_in(project).stdout  # the repository's own test command does not see the stubs


INSIDE = "def test_ac_1():\n    from newmod import add\n    assert add(1, 2) == 3\n\n\ndef test_ac_2():\n    from newmod import add\n    assert add(2, 2) == 4\n"
AT_THE_TOP = "from newmod import add\n\n\ndef test_ac_1():\n    assert add(1, 2) == 3\n\n\ndef test_ac_2():\n    assert add(2, 2) == 4\n"


@pytest.fixture
def gated(repo):
    """A code.S run whose tests stage has its plan and tests record, the test command a real pytest run, and a stub in the run that the command does not use."""
    adopt(repo, commands={"test": f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"})
    begin(repo, "code.S")
    write(repo / "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["."]\n')
    write(run_path(repo, RUN, "stubs", "newmod.py"), "def add(a, b):\n    return 0\n")
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    return repo


def test_gate_measures_the_repositorys_test_command_without_the_stubs_and_the_names_imported_inside_the_tests_fail_at_run_time(run_cli, gated):
    write(gated / "tests" / "test_app.py", INSIDE)
    result = run_cli("gate", RUN, "tests", cwd=gated)
    assert result.returncode == 0, result.stdout
    [entry] = [e for e in ledger(gated) if e["kind"] == "run"]
    assert entry["commands"][0]["exit_code"] == 1  # both tests failed when they ran, on the import of a module that is not there


def test_gate_fails_a_tests_stage_whose_imports_stop_the_collection_and_says_what_to_do(run_cli, gated):
    write(gated / "tests" / "test_app.py", AT_THE_TOP)
    result = run_cli("gate", RUN, "tests", cwd=gated)
    assert result.returncode == 1 and "the test command exited 2: pytest could not collect the tests" in result.stdout
    assert "import what a test needs from the change inside the test function" in result.stdout
    [entry] = [e for e in ledger(gated) if e["kind"] == "run"]
    assert entry["commands"][0]["exit_code"] == 2
