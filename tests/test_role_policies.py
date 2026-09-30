"""Role policies, enforced by the PreToolUse hook: write partitions on Edit, Write and NotebookEdit, command allow-lists on
Bash, and the protected files nobody writes by hand. All of it inside a repository that has adopted plumbline, and silent elsewhere."""
import os
from pathlib import Path

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import CLI, DEFAULT_TOML, commit_all, git, write
from hookdata import bash_payload, denial, start_run, stop_payload, tool_payload
from rundata import adopt, put

ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective")


def agent(role):
    return f"plumbline:{role}"


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


@pytest.fixture
def started(adopted):
    """An adopted repository with run r1 in progress: an agent writes its record into the active run."""
    start_run(adopted)
    return adopted


def configure(repo, text):
    """Add to plumbline.toml and commit it, so that the tree stays clean."""
    with open(repo / "plumbline.toml", "a", encoding="utf-8") as handle:
        handle.write(text)
    commit_all(repo, "configure")


@pytest.fixture
def configured(adopted):
    configure(adopted, '\n[commands]\ntest = "python3 -m pytest"\nlint = ["ruff check", "ruff format --check"]\ntypecheck = "mypy ."\nbuild = "make build"\n')
    return adopted


def writes(run_pre, repo, role, path, tool="Write", key="file_path", cwd=None):
    """The reason an agent of `role` is denied a write to `path` (absolute, or relative to the repo), or None."""
    target = path if os.path.isabs(str(path)) else str(repo / path)
    payload = tool_payload(cwd or repo, tool, {key: target, "content": "x", "old_string": "a", "new_string": "b"}, agent_type=agent(role) if role else None)
    return denial(run_pre(payload, cwd or repo))


def runs(run_pre, repo, role, command, cwd=None):
    payload = bash_payload(cwd or repo, command, agent_type=agent(role) if role else None)
    return denial(run_pre(payload, cwd or repo))


# ------------------------------------------------------------------ write partitions

OWN_RECORD = {
    "planner": ".plumbline/runs/r1/plan.json",
    "test-writer": ".plumbline/runs/r1/tests.json",
    "builder": ".plumbline/runs/r1/build.json",
    "verifier": ".plumbline/runs/r1/verify.json",
    "prosecutor": ".plumbline/runs/r1/review/round-1/prosecutor-security.json",
    "defender": ".plumbline/runs/r1/review/round-1/defender-1.json",
    "detective": ".plumbline/runs/r1/review/round-1/detective.json",
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_every_agent_may_write_its_own_record(run_pre, started, role, tool, key):
    assert writes(run_pre, started, role, OWN_RECORD[role], tool, key) is None


def test_the_review_agents_may_write_in_the_round_directories_of_the_test_review_too(run_pre, started):
    for role, name in (("prosecutor", "prosecutor-tests.json"), ("defender", "defender-2.json"), ("detective", "detective.json")):
        assert writes(run_pre, started, role, f".plumbline/runs/r1/test-review/round-1/{name}") is None, role


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("other", ROLES)
def test_an_agent_writes_only_its_own_record_never_another_agents(run_pre, adopted, role, other):
    if role == other:
        return
    reason = writes(run_pre, adopted, role, OWN_RECORD[other])
    assert reason and f"the {role} writes" in reason and OWN_RECORD[other] in reason


def test_a_review_agent_cannot_write_a_stage_record_or_a_path_outside_a_round_directory(run_pre, adopted):
    for role in ("prosecutor", "defender", "detective"):
        for path in (".plumbline/runs/r1/review.json", ".plumbline/runs/r1/review/prosecutor-x.json", ".plumbline/runs/r1/review/round-0/x.json",
                     ".plumbline/runs/r1/review/round-1/deep/x.json", ".plumbline/runs/r1/build.json", ".plumbline/runs/r1/intake.json",
                     ".plumbline/runs/r1/other-stage/round-1/x.json", ".plumbline/runs/r1/review/round-1/x.txt"):
            assert writes(run_pre, adopted, role, path), (role, path)


def test_the_builder_to_a_test_path_is_denied_and_says_who_writes_tests(run_pre, adopted):
    for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")):
        reason = writes(run_pre, adopted, "builder", "tests/test_new.py", tool, key)
        assert reason == "plumbline: tests/test_new.py is a test path, and the builder does not write tests. The test-writer writes them; build from the spec."


@pytest.mark.parametrize("path", ["tests/x.py", "tests/deep/er/data.json", "test/x.py", "src/test_app.py", "pkg/app_test.py", "web/app.test.ts", "web/app.spec.js"])
def test_the_builder_cannot_write_what_the_tests_type_matches(run_pre, adopted, path):
    assert writes(run_pre, adopted, "builder", path)


@pytest.mark.parametrize("path", ["src/app.py", "src/new/module.py", "README.md", "docs/guide.md", ".gitignore", "pyproject.toml", "src/contest.py", "latest.py"])
def test_the_builder_may_write_everything_that_is_not_tests_plumbline_or_the_pipeline(run_pre, adopted, path):
    assert writes(run_pre, adopted, "builder", path) is None


@pytest.mark.parametrize("path", ["plumbline.toml", ".plumbline/notes.txt", ".plumbline/runs/r1/plan.json", ".plumbline/pass/x.json", ".plumbline/blocks/x"])
def test_the_builder_never_writes_plumbline_toml_or_plumbline_files_but_its_own_record(run_pre, adopted, path):
    assert writes(run_pre, adopted, "builder", path)


def test_the_builder_cannot_write_the_pipeline_file_of_the_repository(run_pre, adopted):
    write(adopted / "pipelines" / "house.toml", DEFAULT_TOML.read_text(encoding="utf-8"))
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(adopted, "own pipeline")
    reason = writes(run_pre, adopted, "builder", "pipelines/house.toml")
    assert reason == "plumbline: pipelines/house.toml defines how the pipeline runs; it changes through the main session, and the builder leaves it alone."
    assert writes(run_pre, adopted, "builder", "pipelines/other.toml") is None
    assert writes(run_pre, adopted, None, "pipelines/house.toml") is None  # the main session and the builder of the repository are free


def test_the_test_writer_writes_test_paths_its_stubs_and_its_record_and_no_source(run_pre, started):
    adopted = started
    for path in ("tests/test_new.py", "tests/helpers/data.py", "src/test_app.py", "web/app.spec.js", OWN_RECORD["test-writer"], ".plumbline/runs/r1/stubs/newmod.py"):
        assert writes(run_pre, adopted, "test-writer", path) is None, path
    reason = writes(run_pre, adopted, "test-writer", "src/app.py")
    assert reason and "the test-writer writes its own record (.plumbline/runs/<run-id>/tests.json), test paths and stubs (.plumbline/runs/<run-id>/stubs/); src/app.py is neither" in reason
    assert "Put the stubs of a brand-new module in the run's stubs directory" in reason
    for path in ("README.md", "plumbline.toml", "conftest.py", ".plumbline/runs/r1/build.json"):
        assert writes(run_pre, adopted, "test-writer", path), path


@pytest.mark.parametrize("role", ["planner", "verifier", "prosecutor", "defender", "detective"])
def test_the_agents_that_write_only_records_are_denied_source_and_test_paths(run_pre, adopted, role):
    for path in ("src/app.py", "tests/test_x.py", "README.md", "plumbline.toml"):
        assert writes(run_pre, adopted, role, path), (role, path)


def test_a_prosecutor_outside_plumbline_runs_is_denied(run_pre, adopted, tmp_path):
    assert writes(run_pre, adopted, "prosecutor", "src/app.py")
    assert writes(run_pre, adopted, "prosecutor", ".plumbline/notes.md")
    reason = writes(run_pre, adopted, "prosecutor", str(tmp_path / "elsewhere.json"))
    assert reason and "the prosecutor writes inside the repository" in reason and "is outside it" in reason


def test_the_denial_names_where_the_agent_may_write(run_pre, adopted):
    assert "(.plumbline/runs/<run-id>/plan.json)" in writes(run_pre, adopted, "planner", "src/app.py")
    assert "{test-review,review}/round-<n>/prosecutor-<lens>.json" in writes(run_pre, adopted, "prosecutor", "src/app.py")
    assert "{test-review,review}/round-<n>/defender-<n>.json" in writes(run_pre, adopted, "defender", "src/app.py")
    assert "{test-review,review}/round-<n>/detective.json" in writes(run_pre, adopted, "detective", "src/app.py")


@pytest.mark.parametrize("role", [*ROLES, None])
@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_nobody_writes_the_pass_directory(run_pre, adopted, role, tool, key):
    for path in (".plumbline/pass/abc.json", ".plumbline/pass/abc.override.json", ".plumbline/pass/new/deeper.json"):
        reason = writes(run_pre, adopted, role, path, tool, key)
        assert reason and "is written only by plumbline.py commands" in reason, (role, path)


@pytest.mark.parametrize("role", [*ROLES, None])
def test_nobody_writes_a_ledger(run_pre, adopted, role):
    for path in (".plumbline/runs/r1/ledger.jsonl", ".plumbline/runs/another-run.2/ledger.jsonl"):
        reason = writes(run_pre, adopted, role, path, "Edit")
        assert reason and "is written only by plumbline.py commands" in reason and "ledger.jsonl" in reason


def test_the_main_session_writes_everything_else_including_the_runs_other_files(run_pre, adopted):
    for path in ("src/app.py", "tests/test_x.py", "plumbline.toml", ".plumbline/runs/r1/verify.json", ".plumbline/notes.txt", ".plumbline/supplied-spec.json"):
        assert writes(run_pre, adopted, None, path) is None, path


def test_a_write_to_a_ledger_named_file_outside_plumbline_is_not_a_ledger(run_pre, adopted):
    assert writes(run_pre, adopted, None, "docs/ledger.jsonl") is None
    assert writes(run_pre, adopted, None, ".plumbline/ledger.jsonl") is None  # only a run's ledger, .plumbline/runs/<run>/ledger.jsonl


def test_a_path_that_leads_into_tests_or_the_pass_directory_through_a_symlink_counts_as_where_it_leads(run_pre, adopted):
    write(adopted / "tests" / "x.py", "x = 1\n")
    (adopted / ".plumbline" / "pass").mkdir(parents=True)
    os.symlink("../tests/x.py", adopted / "src" / "alias.py")
    os.symlink("../.plumbline/pass", adopted / "src" / "passes")
    assert writes(run_pre, adopted, "builder", "src/alias.py")
    assert writes(run_pre, adopted, None, "src/passes/forged.json"), "the main session too"
    assert writes(run_pre, adopted, "builder", "src/passes/forged.json")


def test_relative_paths_resolve_against_the_cwd_of_the_hook_input(run_pre, adopted):
    payload = tool_payload(adopted / "src", "Write", {"file_path": "../tests/test_x.py", "content": "x"}, agent_type=agent("builder"))
    assert denial(run_pre(payload, adopted / "src"))
    payload = tool_payload(adopted / "src", "Write", {"file_path": "new.py", "content": "x"}, agent_type=agent("builder"))
    assert denial(run_pre(payload, adopted / "src")) is None


def test_paths_outside_the_repository_are_denied_to_agents_and_not_to_the_main_session(run_pre, adopted, tmp_path):
    outside = str(tmp_path / "scratch.txt")
    for role in ROLES:
        assert writes(run_pre, adopted, role, outside), role
    assert writes(run_pre, adopted, None, outside) is None


def test_the_tests_type_comes_from_the_repositorys_pipeline_for_the_writers(run_pre, adopted):
    custom = DEFAULT_TOML.read_text().replace('"tests/**", "test/**",', '"spec/**",')
    write(adopted / "pipelines" / "house.toml", custom)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(adopted, "own pipeline")
    assert writes(run_pre, adopted, "builder", "spec/a_spec.rb")
    assert writes(run_pre, adopted, "builder", "tests/x.py") is None  # tests/ is no longer the tests type there
    assert writes(run_pre, adopted, "test-writer", "spec/a_spec.rb") is None
    assert writes(run_pre, adopted, "test-writer", "tests/x.py")


def test_a_pipeline_without_a_roles_table_gets_the_shipped_policies(run_pre, adopted):
    text = DEFAULT_TOML.read_text(encoding="utf-8")
    write(adopted / "old.toml", text[: text.index("# Role policies")])
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "old.toml"\n')
    commit_all(adopted, "an older pipeline")
    assert writes(run_pre, adopted, "builder", "tests/x.py")
    assert writes(run_pre, adopted, "builder", "src/app.py") is None
    assert runs(run_pre, adopted, "builder", "ls")


def test_a_broken_plumbline_toml_still_partitions_the_writes_with_the_shipped_policies(run_pre, adopted):
    write(adopted / "plumbline.toml", "schema = = 1\n")
    assert writes(run_pre, adopted, "builder", "tests/x.py")
    assert writes(run_pre, adopted, "builder", "src/app.py") is None


def test_agents_of_other_plugins_and_unknown_plumbline_agents_are_not_partitioned(run_pre, adopted):
    for agent_type in ("probe:echo", "Explore", "plumbline:wizard", "plumbline:", "general-purpose"):
        payload = tool_payload(adopted, "Write", {"file_path": str(adopted / "src" / "app.py"), "content": "x"}, agent_type=agent_type)
        assert denial(run_pre(payload, adopted)) is None, agent_type
        payload = bash_payload(adopted, "rm -rf x", agent_type=agent_type)
        assert denial(run_pre(payload, adopted)) is None, agent_type


def test_nothing_is_partitioned_where_plumbline_is_not_adopted(run_pre, repo):
    write(repo / "tests" / "x.py", "x = 1\n")
    for role in ROLES:
        assert writes(run_pre, repo, role, "tests/x.py") is None and writes(run_pre, repo, role, ".plumbline/pass/x.json") is None
        assert runs(run_pre, repo, role, "rm -rf x") is None
    assert writes(run_pre, repo, None, ".plumbline/pass/x.json") is None
    assert runs(run_pre, repo, None, "echo x > .plumbline/pass/a.json") is None


def test_a_missing_tool_input_key_is_not_a_write(run_pre, adopted):
    payload = tool_payload(adopted, "Write", {"content": "x"}, agent_type=agent("builder"))
    assert denial(run_pre(payload, adopted)) is None
    payload = tool_payload(adopted, "NotebookEdit", {"new_source": "x"}, agent_type=agent("builder"))
    assert denial(run_pre(payload, adopted)) is None


# ------------------------------------------------------------- command allow-lists

MESSAGE_START = {r: f"plumbline: the {r}'s Bash may run only " for r in ROLES}


def test_the_verifier_running_rm_is_denied_and_the_denial_names_what_it_may_run(run_pre, configured):
    reason = runs(run_pre, configured, "verifier", "rm -rf x")
    assert reason.startswith(MESSAGE_START["verifier"]) and reason.endswith("`rm -rf x` is none of these.")
    for named in ("python3 -m pytest", "ruff check", "ruff format --check", "mypy .", "make build", "read-only git (diff, show, log, status, rev-parse, merge-base, ls-files, grep, blame)",
                  "read-only search tools (grep, rg, cat, head, tail, wc, sed -n, ls, find without -exec or -delete)", "`plumbline.py check-diff`", "`plumbline.py check-record TYPE FILE`"):
        assert named in reason, named


def test_the_verifier_running_the_declared_test_command_is_allowed(run_pre, configured):
    for command in ("python3 -m pytest", "python3 -m pytest -q tests/test_app.py::test_x", "PYTHONPATH=src python3 -m pytest -q", "timeout 120 python3 -m pytest",
                    "env CI=1 python3 -m pytest 2>&1 | tail -20", "ruff check src", "ruff format --check .", "mypy . --strict", "make build", "cd src && python3 -m pytest"):
        assert runs(run_pre, configured, "verifier", command) is None, command


def test_the_declared_command_is_a_prefix_of_words_not_of_characters(run_pre, configured):
    for command in ("python3 -m pytestx", "python3 -m", "python3", "python3 -c 'import os'", "python3 script.py", "ruff", "make deploy", "mypy"):
        assert runs(run_pre, configured, "verifier", command), command


def test_a_command_named_with_a_path_matches_only_that_path_and_a_bare_name_matches_wherever_the_program_lives(run_pre, adopted):
    configure(adopted, '\n[commands]\ntest = "./run_tests.sh"\nlint = "ruff check"\nbuild = "scripts/build.sh --fast"\n')
    assert runs(run_pre, adopted, "verifier", "./run_tests.sh -q") is None
    assert runs(run_pre, adopted, "verifier", "scripts/build.sh --fast all") is None
    for command in ("./other.sh", "./run_tests.sh.bak", "rm -rf x", "/tmp/run_tests.sh", "scripts/build.sh", "scripts/build.sh --slow", "run_tests.sh"):
        assert runs(run_pre, adopted, "verifier", command), command
    assert runs(run_pre, adopted, "verifier", "/usr/local/bin/ruff check src") is None  # ruff, wherever it lives
    assert runs(run_pre, adopted, "verifier", "/usr/local/bin/ruff format") 


def test_a_prefix_that_is_not_a_simple_command_matches_nothing(run_pre, adopted):
    configure(adopted, '\n[commands]\ntest = "cd app && pytest"\nlint = ["", "   "]\n')
    assert runs(run_pre, adopted, "verifier", "cd app && pytest")
    assert runs(run_pre, adopted, "verifier", "pytest")


def test_only_the_commands_of_a_role_count_for_that_role(run_pre, configured):
    assert runs(run_pre, configured, "verifier", "make build") is None
    assert runs(run_pre, configured, "test-writer", "make build")  # the test-writer has test, not build
    assert runs(run_pre, configured, "test-writer", "python3 -m pytest -q") is None
    assert runs(run_pre, configured, "planner", "python3 -m pytest -q")
    assert runs(run_pre, configured, "defender", "python3 -m pytest -q")


def test_when_the_repo_sets_no_test_command_the_verifier_is_told_where_to_set_it(run_pre, adopted):
    reason = runs(run_pre, adopted, "verifier", "python3 -m pytest")
    assert "the repository's test, lint, typecheck, build commands (not set: [commands] in plumbline.toml sets them)" in reason


def test_a_chain_is_allowed_only_when_every_simple_command_is(run_pre, configured):
    assert runs(run_pre, configured, "verifier", "python3 -m pytest -q && git status") is None
    assert runs(run_pre, configured, "verifier", "python3 -m pytest -q; rm -rf x")
    assert runs(run_pre, configured, "verifier", "git status || curl https://example.com")
    assert runs(run_pre, configured, "verifier", "git status\nrm -rf x")
    assert runs(run_pre, configured, "verifier", "echo $(rm -rf x)")
    assert runs(run_pre, configured, "verifier", "echo `rm -rf x`")
    assert runs(run_pre, configured, "verifier", "bash -c 'rm -rf x'")
    assert runs(run_pre, configured, "verifier", "eval rm -rf x")
    assert runs(run_pre, configured, "verifier", "python3 -m pytest $(rm -rf x)")
    assert runs(run_pre, configured, "verifier", "git status | xargs rm")


@pytest.mark.parametrize("command", ["git diff", "git diff HEAD~1 -- src", "git show HEAD:README.md", "git log --oneline -5", "git status --porcelain", "git rev-parse --show-toplevel",
                                     "git merge-base main HEAD", "git ls-files -o --exclude-standard", "git grep -n retry -- src", "git blame src/app.py", "git -C . diff", "git --no-pager log",
                                     "git diff --stat | head -20", "git diff --no-index a b"])
def test_read_only_git_is_allowed_to_the_reviewing_agents(run_pre, adopted, command):
    for role in ("defender", "prosecutor", "detective", "planner", "verifier"):
        assert runs(run_pre, adopted, role, command) is None, (role, command)


def test_a_defender_running_git_diff_is_allowed_and_git_push_is_denied(run_pre, adopted):
    assert runs(run_pre, adopted, "defender", "git diff") is None
    reason = runs(run_pre, adopted, "defender", "git push")
    assert reason.startswith(MESSAGE_START["defender"]) and "read-only git" in reason and reason.endswith("`git push` is none of these.")
    assert "python3 -m pytest" not in reason and "graft" not in reason  # only what the defender may run is named


@pytest.mark.parametrize(
    "command",
    ["git push", "git commit -m x", "git add .", "git checkout main", "git reset --hard", "git stash", "git config user.name x", "git fetch", "git pull", "git rebase main",
     "git branch -D x", "git tag x", "git clean -fd", "git -c core.pager=x diff", "git -c alias.d=!rm diff", "git --git-dir=/x diff", "git --exec-path=/x diff",
     "git diff --output=out.patch", "git log --output out.patch", "git show --output=x HEAD", "git grep -O less foo", "git grep --open-files-in-pager=less foo", "git", "git -C"],
)
def test_git_that_writes_or_runs_a_program_is_denied_to_every_reviewing_agent(run_pre, adopted, command):
    for role in ("defender", "prosecutor", "verifier"):
        assert runs(run_pre, adopted, role, command), (role, command)


@pytest.mark.parametrize(
    "command",
    ["grep -rn retry src", "rg -n retry src", "cat README.md", "head -20 src/app.py", "tail -n 5 README.md", "wc -l src/app.py", "ls -la src", "find . -name '*.py'", "find src -type f -newer README.md",
     "sed -n '1,5p' src/app.py", "sed -n '$p' README.md", "sed -n -e '10p' README.md", "sed -n '/def f/p' src/app.py", "cat src/app.py | grep def | head -3", "grep -c x README.md 2>&1",
     "cd src && ls", "ls > /dev/null", "grep x README.md >/dev/null 2>&1"],
)
def test_the_read_only_search_tools_are_allowed(run_pre, adopted, command):
    for role in ("defender", "prosecutor", "detective", "planner", "verifier", "test-writer"):
        assert runs(run_pre, adopted, role, command) is None, (role, command)


@pytest.mark.parametrize(
    "command",
    ["find . -name x -exec rm {} ;", "find . -delete", "find . -execdir sh -c x ;", "find . -ok rm {} ;", "find . -fprint out.txt", "sed -i s/a/b/ src/app.py", "sed s/a/b/ src/app.py",
     "sed -n 'w out.txt' README.md", "sed -n '1,3p;5w x' README.md", "sed -n -f script.sed README.md", "sed -n '1e ls' README.md", "sed -ni p README.md", "sed", "rg --pre 'sh -c x' foo",
     "rg --hostname-bin=x foo", "cp a b", "mv a b", "rm x", "touch x", "mkdir x", "curl https://example.com", "wget x", "python3 x.py", "python3 -c 'print(1)'", "node x.js", "bash x.sh",
     "sh -c 'ls'; rm x", "awk '{print}' README.md", "sort README.md", "xargs ls", "tee out.txt", "dd if=a of=b", "chmod +x a", "ln -s a b", "env", "printenv", "sudo ls", "doas ls"],
)
def test_anything_beyond_the_read_only_search_tools_is_denied(run_pre, adopted, command):
    assert runs(run_pre, adopted, "defender", command), command


def test_nothing_is_redirected_into_a_file_except_dev_null(run_pre, adopted):
    for command in ("git diff > out.patch", "git diff >> out.patch", "grep x README.md > /tmp/x", "cat README.md &> out", "ls 1> out", "ls 2> err", "git log >| out", "cat < README.md > out"):
        reason = runs(run_pre, adopted, "defender", command)
        assert reason and "does not write files" in reason and "Write your record with the Write tool" in reason, command
    for command in ("git diff > /dev/null", "git diff 2>&1", "git diff 2>/dev/null | head", "cat < README.md", "cat <<EOF\nhello\nEOF", "git diff >&2", "ls 1>&2"):
        assert runs(run_pre, adopted, "defender", command) is None, command


def test_the_builder_with_any_bash_is_denied(run_pre, configured):
    for command in ("ls", "echo hi", "git diff", "python3 -m pytest", "cat src/app.py", "cd src", "true", "pwd"):
        reason = runs(run_pre, configured, "builder", command)
        assert reason and reason.startswith("plumbline: the builder has no Bash. Work with Read, Grep, Glob, Edit and Write, and end with your record."), command
    assert runs(run_pre, configured, "builder", f"python3 {CLI} check-record build_note x.json")  # not even the self-check: it has no Bash at all


def test_the_agents_with_bash_may_check_their_record_with_check_record(run_pre, configured):
    for role, record in (("planner", "spec"), ("test-writer", "tests_record"), ("verifier", "verify_record"), ("prosecutor", "findings_record"), ("defender", "defense_record"), ("detective", "gaps_record")):
        assert runs(run_pre, configured, role, f'python3 "{CLI}" check-record {record} .plumbline/runs/r1/x.json') is None, role
        assert runs(run_pre, configured, role, f"python3 {CLI} check-record {record} x.json && echo fine") is None


def test_check_diff_is_the_verifiers_alone(run_pre, configured):
    assert runs(run_pre, configured, "verifier", f'python3 "{CLI}" check-diff --run r1') is None
    assert runs(run_pre, configured, "verifier", f"python3 -u {CLI} check-diff") is None
    for role in ("planner", "test-writer", "prosecutor", "defender", "detective"):
        assert runs(run_pre, configured, role, f'python3 "{CLI}" check-diff'), role


@pytest.mark.parametrize("subcommand", ["gate r1 verify", "pass r1", "merge-review r1 review", "plan --intent fix", "classify", "init", "status", "tokens r1", "render x.json", "validate-pipeline"])
def test_agents_run_no_other_plumbline_command(run_pre, configured, subcommand):
    for role in ("verifier", "planner", "prosecutor"):
        assert runs(run_pre, configured, role, f'python3 "{CLI}" {subcommand}'), (role, subcommand)


def test_only_this_plugins_own_script_counts_as_plumbline_py(run_pre, configured):
    write(configured / "tests" / "plumbline.py", "print('mine')\n")
    for command in ("python3 tests/plumbline.py check-diff", "python3 tests/plumbline.py check-record spec x.json", "python3 ../elsewhere/plumbline.py check-diff", "python3 -m plumbline check-diff", "python3 -c 'x' check-diff"):
        assert runs(run_pre, configured, "verifier", command), command


def test_a_graft_command_is_allowed_only_where_graft_is_on_and_only_to_the_roles_that_list_it(run_pre, adopted):
    for role in ("planner", "prosecutor", "detective"):
        assert runs(run_pre, adopted, role, "graft query retry"), role  # graft is off
    write(adopted / "plumbline.toml", 'schema = 1\n\n[graft]\nenabled = true\n')
    commit_all(adopted, "graft on")
    for role in ("planner", "prosecutor", "detective"):
        assert runs(run_pre, adopted, role, "graft query retry") is None, role
    for role in ("defender", "verifier", "test-writer"):
        assert runs(run_pre, adopted, role, "graft query retry"), role
    assert "the graft wrapper" in runs(run_pre, adopted, "prosecutor", "rm x")
    write(adopted / "plumbline.toml", "schema = 1\n")
    commit_all(adopted, "graft off")
    assert "the graft wrapper (graft is off in this repository)" in runs(run_pre, adopted, "prosecutor", "rm x")


def test_the_no_effect_builtins_and_assignments_are_allowed_wherever_any_command_is(run_pre, adopted):
    for command in ("cd src", "pwd", "true", "false", ":", "echo done", "printf '%s' x", "FOO=bar", "cd src && pwd && ls", "# a comment", "", "   "):
        assert runs(run_pre, adopted, "defender", command) is None, repr(command)


def test_running_as_another_user_is_denied(run_pre, configured):
    for command in ("sudo python3 -m pytest", "sudo -u root ls", "doas git diff"):
        reason = runs(run_pre, configured, "verifier", command)
        assert reason == f"plumbline: the verifier does not run commands as another user (`{command}`)."


def test_wrappers_that_only_pass_through_are_seen_through(run_pre, configured):
    for command in ("env git diff", "timeout 30 grep x README.md", "nice -n 5 ls", "time git status", "command git log", "nohup ls"):
        assert runs(run_pre, configured, "defender", command) is None, command
    for command in ("env FOO=1 rm x", "timeout 30 curl x", "nice -n 5 rm x"):
        assert runs(run_pre, configured, "defender", command), command
    # an assignment in front of git or a search tool is refused (GIT_EXTERNAL_DIFF and the like make them run programs); a test command may carry one
    assert runs(run_pre, configured, "defender", "env FOO=1 git diff")
    assert runs(run_pre, configured, "verifier", "env CI=1 python3 -m pytest -q") is None


def test_the_allow_list_is_not_applied_to_the_main_session(run_pre, configured):
    for command in ("rm -rf x", "curl https://example.com", "python3 script.py", "echo x > out.txt"):
        assert runs(run_pre, configured, None, command) is None, command


def test_the_power_shell_tool_is_held_to_the_same_policy(run_pre, adopted):
    payload = tool_payload(adopted, "PowerShell", {"command": "rm -rf x"}, agent_type=agent("verifier"))
    assert denial(run_pre(payload, adopted))


def test_an_agent_in_a_subdirectory_is_held_to_the_policy_of_its_repository(run_pre, configured):
    assert runs(run_pre, configured, "verifier", "rm x", cwd=configured / "src")
    assert runs(run_pre, configured, "verifier", "python3 -m pytest", cwd=configured / "src") is None


def test_the_policy_is_read_from_the_pipeline_so_a_repo_can_change_it_by_data(run_pre, adopted):
    custom = DEFAULT_TOML.read_text().replace('[roles.defender]\nwrites = ["record"]\ncommands = ["git-read", "search"]', '[roles.defender]\nwrites = ["record", "tests"]\ncommands = ["git-read"]')
    write(adopted / "pipelines" / "house.toml", custom)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(adopted, "own pipeline")
    assert runs(run_pre, adopted, "defender", "git diff") is None
    assert runs(run_pre, adopted, "defender", "grep x README.md")  # search is no longer in its list
    assert writes(run_pre, adopted, "defender", "tests/x.py") is None  # and tests are now in its write targets
    assert writes(run_pre, adopted, "defender", "src/app.py")


# --------------------------------------------------------------- protected files from Bash

PROTECTED_WRITES = [
    "echo x > .plumbline/pass/a.json",
    "echo x >> .plumbline/pass/a.json",
    "echo x >| .plumbline/pass/a.json",
    "echo x &> .plumbline/pass/a.json",
    "echo x 1> .plumbline/pass/a.json",
    "> .plumbline/pass/a.json",
    "echo x > .plumbline/runs/r1/ledger.jsonl",
    "echo x >> .plumbline/runs/r1/ledger.jsonl",
    "echo x | tee .plumbline/runs/r1/ledger.jsonl",
    "echo x | tee -a .plumbline/runs/r1/ledger.jsonl",
    "echo x | tee .plumbline/pass/a.json",
    "cat a | tee out.txt .plumbline/pass/a.json",
    "cp /tmp/forged.json .plumbline/pass/a.json",
    "cp -t .plumbline/pass /tmp/forged.json",
    "mv /tmp/forged.json .plumbline/pass/a.json",
    "install /tmp/forged.json .plumbline/pass/a.json",
    "ln -s /tmp/forged.json .plumbline/pass/a.json",
    "dd if=/tmp/forged of=.plumbline/pass/a.json",
    "truncate -s 0 .plumbline/runs/r1/ledger.jsonl",
    "rm .plumbline/pass/abc.json",
    "rm -rf .plumbline/pass",
    "rm .plumbline/runs/r1/ledger.jsonl",
    "sed -i s/pass/fail/ .plumbline/pass/abc.json",
    "touch .plumbline/pass/a.json",
    "mv .plumbline/pass/abc.json /tmp/x",  # moving a record away removes it
    "cd .plumbline && echo x > pass/a.json",
    "cd .plumbline/pass && echo x > a.json",
    "cd .plumbline/runs/r1 && echo x >> ledger.jsonl",
    "cd .plumbline/runs/r1 && rm ledger.jsonl",
    "bash -c 'echo x > .plumbline/pass/a.json'",
    "sh -c \"echo x | tee .plumbline/runs/r1/ledger.jsonl\"",
    "eval 'echo x > .plumbline/pass/a.json'",
    "echo $(echo x > .plumbline/pass/a.json)",
    "true && echo x > ./.plumbline/pass/a.json",
    "echo x > .plumbline/pass/../pass/a.json",
    "echo x > \"$PWD/.plumbline/pass/a.json\"",
]


@pytest.mark.parametrize("command", PROTECTED_WRITES)
def test_a_command_that_writes_a_protected_file_is_denied_to_the_main_session(run_pre, adopted, command):
    reason = runs(run_pre, adopted, None, command)
    if "$PWD" in command:
        assert reason is None  # a path that is computed by the shell cannot be followed
        return
    assert reason and "is written only by plumbline.py commands" in reason, command


def test_the_main_session_echo_into_the_pass_directory_and_a_pipe_into_a_ledger_are_denied(run_pre, adopted):
    assert "is written only by plumbline.py commands" in runs(run_pre, adopted, None, "echo x > .plumbline/pass/a.json")
    assert "is written only by plumbline.py commands" in runs(run_pre, adopted, None, "echo x | tee .plumbline/runs/r1/ledger.jsonl")


@pytest.mark.parametrize("role", ROLES)
def test_a_protected_file_is_denied_to_agents_too_whatever_their_own_policy_says(run_pre, adopted, role):
    reason = runs(run_pre, adopted, role, "echo x > .plumbline/pass/a.json")
    assert reason  # by the allow-list, or by the protected file rule
    if role != "builder":
        assert runs(run_pre, adopted, role, "cat > .plumbline/pass/a.json")


def test_protected_files_are_denied_from_a_subdirectory_too(run_pre, adopted):
    (adopted / ".plumbline" / "pass").mkdir(parents=True)
    assert runs(run_pre, adopted, None, "echo x > ../.plumbline/pass/a.json", cwd=adopted / "src")
    assert runs(run_pre, adopted, None, f"echo x > {adopted}/.plumbline/pass/a.json", cwd=adopted / "src")
    assert runs(run_pre, adopted, None, "echo x > pass/a.json", cwd=adopted / ".plumbline")
    assert runs(run_pre, adopted, None, "echo x > a.json", cwd=adopted / ".plumbline" / "pass")


def test_an_agent_cannot_hide_a_write_to_the_pass_directory_behind_a_symlink_either(run_pre, adopted):
    (adopted / ".plumbline" / "pass").mkdir(parents=True)
    os.symlink(".plumbline/pass", adopted / "passes")
    assert runs(run_pre, adopted, "verifier", "echo x > passes/a.json")  # an agent's Bash never redirects into a file
    assert writes(run_pre, adopted, None, "passes/a.json")  # and the write tools follow the link for everyone


@pytest.mark.parametrize(
    "command",
    [
        "cat .plumbline/pass/abc.json",
        "ls .plumbline/pass",
        "ls -la .plumbline/runs/r1",
        "grep passed .plumbline/runs/r1/ledger.jsonl",
        "tail -5 .plumbline/runs/r1/ledger.jsonl",
        "cp .plumbline/pass/abc.json /tmp/copy.json",
        "cp .plumbline/runs/r1/ledger.jsonl /tmp/ledger.copy",
        "echo x > .plumbline/runs/r1/verify.json",
        "echo x > .plumbline/notes.txt",
        "echo x > notes-about-pass.txt",
        "echo pass > docs/pass.md",
        "git diff > /tmp/x.patch",
        f"python3 {CLI} status",
        f"python3 {CLI} pass r1",
        f"python3 {CLI} gate r1 verify",
        "echo '.plumbline/pass/a.json' > list.txt",
        "wc -l .plumbline/runs/r1/ledger.jsonl",
    ],
)
def test_reading_a_protected_file_and_writing_anything_else_are_allowed(run_pre, adopted, command):
    assert runs(run_pre, adopted, None, command) is None, command


def test_plumbline_commands_write_the_protected_files_and_nothing_stops_them(run_cli, adopted):
    from rundata import write_docs_run

    write_docs_run(adopted)
    assert run_cli("pass", "r1", cwd=adopted).returncode == 0
    assert list((adopted / ".plumbline" / "pass").glob("*.json")) and (adopted / ".plumbline" / "runs" / "r1" / "ledger.jsonl").is_file()


# ---------------------------------------------------- the parser: redirections and steps


@pytest.mark.parametrize(
    "text,words,redirects",
    [
        ("echo x > out.txt", [["echo", "x"]], [[(">", "out.txt")]]),
        ("echo x>out.txt", [["echo", "x"]], [[(">", "out.txt")]]),
        ("echo x >> out.txt", [["echo", "x"]], [[(">>", "out.txt")]]),
        ("echo x >| out.txt", [["echo", "x"]], [[(">|", "out.txt")]]),
        ("echo x 2> err 1>&2", [["echo", "x"]], [[(">", "err"), (">&", "2")]]),
        ("git push 2>&1 | tail", [["git", "push"], ["tail"]], [[(">&", "1")], []]),
        ("cat < in.txt", [["cat"]], [[("<", "in.txt")]]),
        ("cat <<< 'a b'", [["cat"]], [[("<<<", "a b")]]),
        ("> out.txt", [[]], [[(">", "out.txt")]]),
        ("cmd &> out.txt", [["cmd"], []], [[], [(">", "out.txt")]]),
        ("echo $(echo x > inner.txt) > outer.txt", [["echo", "x"], ["echo", "$(...)"]], [[(">", "inner.txt")], [(">", "outer.txt")]]),
        ("tee out.txt", [["tee", "out.txt"]], [[]]),
        ("cat <<EOF > out.txt\nbody\nEOF\nls", [["cat"], ["ls"]], [[(">", "out.txt")], []]),
        ("echo 'a > b'", [["echo", "a > b"]], [[]]),
        ('echo "a > b" > c', [["echo", "a > b"]], [[(">", "c")]]),
    ],
)
def test_split_commands_ex_returns_each_commands_words_and_redirections(text, words, redirects):
    parsed = pre.split_commands_ex(text)
    assert [w for w, _ in parsed] == words
    assert [r for _, r in parsed] == redirects


def test_split_commands_is_split_commands_ex_without_the_redirections_and_the_empty_commands():
    for text in ("git push", "> out", "a && b > c || d", "echo $(git push) > x", "cat <<EOF\nx\nEOF\nls", "cmd &> f"):
        assert pre.split_commands(text) == [w for w, _ in pre.split_commands_ex(text) if w]


def test_a_redirection_operator_at_the_end_does_not_swallow_the_next_command():
    assert pre.split_commands_ex("echo x >; ls") == [(["echo", "x"], []), (["ls"], [])]
    assert [w for w, _ in pre.split_commands_ex("echo >\nls")] == [["echo"], ["ls"]]


def test_walk_follows_cd_shells_and_substitutions_with_the_directory_each_runs_in():
    steps = list(pre.walk("cd sub && bash -c 'cd deeper && echo x > out' ; echo $(ls)", Path("/w/repo")))
    seen = [(s.argv[0] if s.argv else "", str(s.cwd), s.redirects) for s in steps]
    assert ("cd", "/w/repo", []) in seen and ("bash", "/w/repo/sub", []) in seen
    assert ("echo", "/w/repo/sub/deeper", [(">", "out")]) in seen
    assert ("ls", "/w/repo/sub", []) in seen


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["tee", "a", "-a", "b"], ["a", "b"]),
        (["rm", "-rf", "x", "y"], ["x", "y"]),
        (["mv", "a", "b"], ["a", "b"]),
        (["cp", "a", "b"], ["b"]),
        (["cp", "-t", "dir", "a", "b"], ["dir", "b"]),
        (["cp", "--target-directory", "dir", "a"], ["dir", "a"]),
        (["install", "-m", "644", "a", "b"], ["b"]),
        (["dd", "if=a", "of=b"], ["b"]),
        (["sed", "-i", "s/a/b/", "f"], ["s/a/b/", "f"]),
        (["sed", "s/a/b/", "f"], []),
        (["perl", "-pi", "-e", "x", "f"], ["x", "f"]),
        (["sed", "-ni", "p", "f"], ["p", "f"]),
        (["sed", "-i.bak", "s/a/b/", "f"], ["s/a/b/", "f"]),
        (["sed", "--in-place=.bak", "s/a/b/", "f"], ["s/a/b/", "f"]),
        (["sed", "-n", "p", "f"], []),
        (["cat", "f"], []),
        ([], []),
    ],
)
def test_written_operands_are_what_a_writer_writes_or_removes(argv, expected):
    assert pre.written_operands(argv) == expected


def test_the_hook_roles_are_the_pipelines_roles():
    assert pre.ROLES == pl.AGENT_ROLES and pre.REVIEW_ROLES == ("prosecutor", "defender", "detective")
    assert pre.CONFIG_COMMANDS == pl.CONFIG_COMMANDS and pre.PASS_DIR == pl.PASS_DIR and pre.RUNS_DIR == pl.RUNS_DIR and pre.STUBS_DIR == pl.STUBS_DIR == "stubs"
    assert set(pre.GIT_READ) == {"diff", "show", "log", "status", "rev-parse", "merge-base", "ls-files", "grep", "blame"}


# ------------------------------------------------- silent outside adopted repositories

OUTSIDE = [
    ("Write", {"file_path": "{repo}/tests/x.py", "content": "x"}, "plumbline:builder"),
    ("Write", {"file_path": "{repo}/src/app.py", "content": "x"}, "plumbline:test-writer"),
    ("Write", {"file_path": "{repo}/.plumbline/pass/x.json", "content": "x"}, None),
    ("Edit", {"file_path": "{repo}/.plumbline/runs/r1/ledger.jsonl", "old_string": "a", "new_string": "b"}, None),
    ("NotebookEdit", {"notebook_path": "{repo}/tests/n.ipynb", "new_source": "x"}, "plumbline:builder"),
    ("Write", {"file_path": "/tmp/anything.txt", "content": "x"}, "plumbline:prosecutor"),
    ("Bash", {"command": "rm -rf x"}, "plumbline:verifier"),
    ("Bash", {"command": "ls"}, "plumbline:builder"),
    ("Bash", {"command": "git push origin main"}, None),
    ("Bash", {"command": "git commit -m x"}, "plumbline:defender"),
    ("Bash", {"command": "gh pr create --fill"}, None),
    ("Bash", {"command": "echo x > .plumbline/pass/a.json"}, None),
    ("Bash", {"command": "echo x | tee .plumbline/runs/r1/ledger.jsonl"}, "plumbline:planner"),
    ("Bash", {"command": 'python3 /p/scripts/plumbline.py override --reason "twenty characters or more"'}, None),
    ("Bash", {"command": 'python3 /p/scripts/plumbline.py override --reason "twenty characters or more"'}, "plumbline:verifier"),
    ("Read", {"file_path": "{repo}/tests/x.py"}, "plumbline:builder"),
    ("Grep", {"pattern": "x"}, "plumbline:builder"),
]


@pytest.mark.parametrize("tool,tool_input,agent_type", OUTSIDE, ids=[f"{t}-{a}-{i}" for i, (t, _, a) in enumerate(OUTSIDE)])
def test_every_pre_tool_use_input_is_silent_and_exits_0_where_plumbline_is_not_adopted(run_pre, repo, tool, tool_input, agent_type):
    write(repo / "tests" / "x.py", "assert False\n")
    tool_input = {k: v.replace("{repo}", str(repo)) if isinstance(v, str) else v for k, v in tool_input.items()}
    result = run_pre(tool_payload(repo, tool, tool_input, agent_type=agent_type), repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_subagent_stop_hook_is_silent_too_where_plumbline_is_not_adopted(run_stop, repo):
    for role in ROLES:
        result = run_stop(stop_payload(repo, agent_type=agent(role), message="finished, no record line"), repo)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), role


def test_nothing_is_denied_inside_a_plain_directory_either(run_pre, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for tool, tool_input, agent_type in (("Write", {"file_path": str(plain / "x.py"), "content": "x"}, "plumbline:builder"), ("Bash", {"command": "rm -rf x"}, "plumbline:verifier")):
        result = run_pre(tool_payload(plain, tool, tool_input, agent_type=agent_type), plain, GIT_CEILING_DIRECTORIES=str(tmp_path))
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_hook_stays_silent_on_a_record_only_run_directory_of_a_repo_that_has_not_adopted(run_pre, repo):
    put(repo, "plan", {"goal": ""})  # run files exist, plumbline.toml does not
    payload = tool_payload(repo, "Write", {"file_path": str(repo / ".plumbline/runs/r1/build.json"), "content": "x"}, agent_type=agent("planner"))
    assert run_pre(payload, repo).stdout == ""


# ------------------------------------------------------- records live in the main checkout


def launch(run_pre, repo, subagent, **fields):
    tool_input = {"description": "x", "prompt": "y", "subagent_type": subagent, **fields}
    return denial(run_pre(tool_payload(repo, "Agent", tool_input), repo))


@pytest.mark.parametrize("role", ROLES)
def test_a_plumbline_agent_is_not_launched_into_a_worktree(run_pre, adopted, role):
    reason = launch(run_pre, adopted, agent(role), isolation="worktree")
    assert reason == f"plumbline: a run's records live in the main checkout, so plumbline:{role} runs there. Launch it without `isolation`."


def test_a_plumbline_agent_is_launched_in_the_main_checkout_in_the_foreground_or_the_background(run_pre, adopted):
    assert launch(run_pre, adopted, agent("builder"), run_in_background=False) is None
    assert launch(run_pre, adopted, agent("prosecutor"), run_in_background=True) is None
    assert launch(run_pre, adopted, agent("planner")) is None


def test_other_agents_may_use_a_worktree(run_pre, adopted):
    assert launch(run_pre, adopted, "Explore", isolation="worktree") is None
    assert launch(run_pre, adopted, "probe:echo", isolation="worktree") is None


def test_the_worktree_rule_is_silent_where_plumbline_is_not_adopted(run_pre, repo):
    assert launch(run_pre, repo, agent("builder"), isolation="worktree") is None


# ------------------------------------------------------------ compound commands: the head and the end run nothing, the body is checked


def test_loops_and_conditionals_are_allowed_when_every_command_in_them_is(run_pre, configured):
    for command in (
        "for f in README.md src/app.py; do cat $f; done",
        "for f in $(git diff --name-only); do head -5 $f; done",
        "if git diff --quiet; then echo clean; else echo dirty; fi",
        "if git diff --quiet; then echo clean; fi",
        "case x in x) cat README.md;; esac",
        "while false; do :; done",
        "for f in a b; do git log --oneline -1 -- $f; done | head -3",
        "for f in README.md; do cat $f; done 2>/dev/null",
    ):
        for role in ("verifier", "prosecutor", "defender"):
            assert runs(run_pre, configured, role, command) is None, (role, command)


def test_a_loop_or_conditional_that_runs_something_else_is_denied(run_pre, configured):
    for command in (
        "for f in a b; do rm $f; done",
        "for x in $(rm y); do echo; done",
        "if true; then rm x; fi",
        "if rm x; then echo; fi",
        "case x in x) rm y;; esac",
        "for f in a; do cat $f > out; done",
        "for f in a; do curl $f; done",
        "select x in a; do rm x; done",
        "for f in a; do git push; done",
    ):
        assert runs(run_pre, configured, "verifier", command), command
