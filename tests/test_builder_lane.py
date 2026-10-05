"""Each plumbline agent stays in its lane, and the builder stays blind to what tests would show it.

Findings of the 2b review that these tests pin: WP-OUTOFLANE and WP-GITCFG (no agent writes what runs or steers the session),
the write side of WP-TESTCFG (the builder leaves the test harness to the main session), BB-RECORDS (what the builder may read
under .plumbline/, test leftovers, transcripts), BB-NEWRUN (the run is the active run, not the newest by mtime), BB-CWD (an agent
whose working directory drifts is held all the same), the read side of C-06, and C-22 (review agents write their own files, in the
current round). The hook is called in process here, with PLUMBLINE_HOOK_DEBUG set, so an error inside a rule fails the test
instead of passing for an allow."""
import os
from pathlib import Path

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import DEFAULT_TOML, commit_all, git, write
from hookdata import activate_run, bash_payload, start_run, tool_payload
from rundata import adopt, build_note_record, intake_record, put, put_part, spec_record, verify_record, written_tests_record

ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")
REVIEW_ROLES = ("prosecutor", "defender", "detective", "canary")
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


def configure(repo, text):
    with open(repo / "plumbline.toml", "a", encoding="utf-8") as handle:
        handle.write(text)
    commit_all(repo, "configure")


def writes(repo, role, path, tool="Write", cwd=None, **fields):
    """The reason an agent of `role` (None: the main session) is denied a write to `path`, or None."""
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    target = path if os.path.isabs(str(path)) else str(repo / path)
    tool_input = {key: target, "content": "x", "old_string": "a", "new_string": "b", "new_source": "x"}
    payload = tool_payload(cwd or repo, tool, tool_input, agent_type=f"plumbline:{role}" if role else None, **fields)
    return pre.decide(payload)


def reads(repo, tool, tool_input, cwd=None, agent_type=BUILDER, **fields):
    return pre.decide(tool_payload(cwd or repo, tool, tool_input, agent_type=agent_type, **fields))


def read_of(repo, name, **kwargs):
    return reads(repo, "Read", {"file_path": name if os.path.isabs(name) else str(repo / name)}, **kwargs)


# ------------------------------------------------ WP-OUTOFLANE, WP-GITCFG: what no agent writes

LANE = [
    ".git/config", ".git/hooks/pre-commit", ".git/hooks/pre-push", ".git/HEAD", ".git/info/attributes", ".git/info/exclude",
    ".claude/settings.json", ".claude/settings.local.json", ".claude/agents/builder.md", ".claude/skills/x/SKILL.md", ".claude/commands/x.md",
    ".mcp.json", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".gitattributes", ".worktreeinclude", ".pre-commit-config.yaml",
    ".github/workflows/ci.yml", ".husky/pre-commit",
    "docs/CLAUDE.md", "src/.gitattributes", "vendor/lib/.git/config", "packages/a/.claude/settings.json", ".GIT/config", "Claude.md",
]


@pytest.mark.parametrize("role", ROLES)
def test_no_agent_writes_what_runs_or_steers_the_session(started, role):
    for path in LANE:
        reason = writes(started, role, path)
        assert reason, (role, path)
        assert "main session" in reason, (role, path, reason)  # the denial says who writes it instead


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_the_builder_is_denied_the_lane_files_by_every_writing_tool(started, tool):
    for path in (".git/config", ".claude/settings.json", ".mcp.json", "CLAUDE.md", ".github/workflows/ci.yml", ".husky/pre-commit"):
        assert writes(started, "builder", path, tool), (tool, path)


def test_the_reviewer_reproductions_of_wp_outoflane_are_all_denied_to_the_builder(started):
    # settings that disable every hook, a git hook, workflow files: each accepted verbatim before
    for path in (".git/config", ".git/hooks/pre-commit", ".git/hooks/pre-push", ".git/HEAD", ".claude/settings.json", ".claude/settings.local.json",
                 ".claude/agents/x.md", ".mcp.json", "CLAUDE.md", "AGENTS.md", ".gitattributes", ".worktreeinclude", ".github/workflows/ci.yml", ".husky/pre-commit"):
        assert writes(started, "builder", path, "Write"), path


def test_each_kind_of_lane_file_says_who_changes_it(started):
    assert writes(started, "builder", ".git/config") == "plumbline: .git/config is git's own configuration and hooks. Git commands run by the main session change it, and the builder leaves it alone."
    assert writes(started, "verifier", ".claude/settings.json") == "plumbline: .claude/settings.json holds agent settings and instructions. CI and agent settings change through the main session, and the verifier leaves it alone."
    assert writes(started, "defender", ".github/workflows/ci.yml") == "plumbline: .github/workflows/ci.yml is repository automation (CI, hooks, attributes). CI and agent settings change through the main session, and the defender leaves it alone."
    assert writes(started, "builder", "plumbline.toml") == "plumbline: plumbline.toml defines how the pipeline runs; it changes through the main session, and the builder leaves it alone."


def test_the_main_session_writes_the_lane_files_itself(started):
    for path in LANE:
        assert writes(started, None, path) is None, path


@pytest.mark.parametrize("path", ["src/app.py", "docs/guide.md", ".gitignore", "pyproject.toml", "README.md", "src/claude_notes.md", "docs/agents-guide.md", ".github-notes.md", ".gitmodules", "src/gitattributes.py"])
def test_the_builder_still_writes_what_is_not_in_the_lane(started, path):
    assert writes(started, "builder", path) is None, path


def test_a_symlink_into_git_does_not_get_a_write_through(started):
    os.symlink("../.git", started / "src" / "gitlink")
    os.symlink("../.claude", started / "src" / "claudelink")
    assert writes(started, "builder", "src/gitlink/config")
    assert writes(started, "builder", "src/gitlink/hooks/pre-commit")
    (started / ".claude").mkdir()
    assert writes(started, "builder", "src/claudelink/settings.json")


def test_dotdot_does_not_get_a_write_into_git_either(started):
    assert writes(started, "builder", "src/../.git/config")
    assert writes(started, "builder", "src/../CLAUDE.md")


def test_a_linked_worktrees_git_directory_and_the_main_repositorys_are_denied_from_inside_the_worktree(adopted, tmp_path):
    git(adopted, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    worktree = tmp_path / "wt"
    assert (worktree / "plumbline.toml").is_file() and (worktree / ".git").is_file()  # a linked worktree: .git is a file that points elsewhere
    gitdir = Path(os.path.realpath(worktree / git(worktree, "rev-parse", "--git-dir").strip()))
    common = Path(os.path.realpath(worktree / git(worktree, "rev-parse", "--git-common-dir").strip()))
    assert gitdir != common
    start_run(worktree)
    for role in ("builder", "verifier", "prosecutor"):
        for target in (gitdir / "HEAD", gitdir / "config.worktree", common / "config", common / "hooks" / "pre-commit", worktree / ".git"):
            reason = writes(worktree, role, str(target))
            assert reason and "Git commands run by the main session" in reason, (role, target)


def test_a_write_by_a_review_agent_is_denied_the_lane_files_too_where_a_repo_gave_it_more_targets(adopted):
    custom = DEFAULT_TOML.read_text().replace('[roles.defender]\nwrites = ["record"]', '[roles.defender]\nwrites = ["record", "code", "tests"]')
    write(adopted / "pipelines" / "house.toml", custom)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(adopted, "own pipeline")
    start_run(adopted)
    assert writes(adopted, "defender", "src/app.py") is None  # the repo's data gives the defender code
    for path in (".git/config", ".claude/settings.json", "CLAUDE.md", ".github/workflows/ci.yml"):
        assert writes(adopted, "defender", path), path  # and the lane stays closed whatever the policy says


# ------------------------------------------------ WP-TESTCFG, write side: the test harness is not the builder's


@pytest.mark.parametrize("path", ["conftest.py", "src/conftest.py", "pkg/sub/conftest.py", "pytest.ini", "sub/pytest.ini", "tox.ini", "setup.cfg", "noxfile.py", "CONFTEST.PY"])
def test_the_builder_leaves_the_test_configuration_to_the_main_session(started, path):
    reason = writes(started, "builder", path)
    assert reason and "configures how the tests run" in reason and "main session" in reason


def test_the_test_writer_keeps_the_conftest_under_the_test_paths(started):
    assert writes(started, "test-writer", "tests/conftest.py") is None
    assert writes(started, "test-writer", "conftest.py")  # not a test path, and the test-writer writes no source
    assert writes(started, "builder", "tests/conftest.py") == "plumbline: tests/conftest.py is a test path, and the builder does not write tests. The test-writer writes them; build from the spec."


def test_the_main_session_writes_the_test_configuration(started):
    for path in ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "noxfile.py"):
        assert writes(started, None, path) is None, path


def test_the_builder_does_not_write_the_files_the_commands_table_names(adopted):
    write(adopted / "run_tests.sh", "#!/bin/sh\nexit 1\n")
    write(adopted / "scripts" / "lint.sh", "#!/bin/sh\nexit 0\n")
    write(adopted / "build.mk", "all:\n")
    write(adopted / "mypy.ini", "[mypy]\n")
    write(adopted / "notes.txt", "x\n")
    configure(
        adopted,
        '\n[commands]\ntest = "sh run_tests.sh -q"\nlint = "env FOO=1 ./scripts/lint.sh"\nbuild = "make --file=build.mk all"\ntypecheck = "mypy --config-file=mypy.ini ."\n',
    )
    start_run(adopted)
    for path in ("run_tests.sh", "scripts/lint.sh", "build.mk", "mypy.ini"):
        reason = writes(adopted, "builder", path)
        assert reason and "changes through the main session" in reason and "[commands]" in reason, path
    assert writes(adopted, "builder", "run_tests.sh", "Edit")
    assert writes(adopted, "builder", "notes.txt") is None  # not named by a command
    assert writes(adopted, "builder", "src/app.py") is None  # `.` and `src` are directories, and `check` or `make` are not files here
    assert writes(adopted, None, "run_tests.sh") is None  # the main session may


def test_a_file_named_by_a_command_is_protected_through_a_symlink_too(adopted):
    write(adopted / "run_tests.sh", "#!/bin/sh\nexit 1\n")
    os.symlink("../run_tests.sh", adopted / "src" / "alias.sh")
    configure(adopted, '\n[commands]\ntest = "sh run_tests.sh"\n')
    start_run(adopted)
    assert writes(adopted, "builder", "src/alias.sh")


def test_a_named_file_that_does_not_exist_yet_is_not_protected(adopted):
    configure(adopted, '\n[commands]\ntest = "sh scripts/check.sh"\n')
    start_run(adopted)
    assert writes(adopted, "builder", "scripts/check.sh") is None  # only a file that exists is named


def test_a_role_that_the_repositorys_pipeline_gives_no_record_target_writes_no_record(adopted):
    start_run(adopted)  # `plan --intent` refuses a pipeline that gives an agent no record target, so the run begins first and the pipeline is edited after
    custom = DEFAULT_TOML.read_text().replace('[roles.builder]\nwrites = ["record", "code"]', '[roles.builder]\nwrites = ["code"]')
    assert custom != DEFAULT_TOML.read_text()
    write(adopted / "pipelines" / "house.toml", custom)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    commit_all(adopted, "own pipeline")
    assert any("writes must include 'record'" in error for error in pl.load_project(adopted).errors)
    assert writes(adopted, "builder", "src/app.py") is None
    reason = writes(adopted, "builder", ".plumbline/runs/r1/build.json")
    assert reason and "writes only its own record" in reason
    assert writes(adopted, "planner", ".plumbline/runs/r1/plan.json") is None  # the others keep theirs


# ------------------------------------------- BB-NEWRUN: an agent writes its record in the active run


def test_an_agents_record_belongs_in_the_active_run_not_in_a_new_one(started):
    assert writes(started, "builder", ".plumbline/runs/r1/build.json") is None
    reason = writes(started, "builder", ".plumbline/runs/zzz/build.json")
    assert reason == "plumbline: the builder writes its record in the active run (r1); .plumbline/runs/zzz/build.json belongs to run zzz."
    for role, path in (("planner", "plan.json"), ("test-writer", "tests.json"), ("verifier", "verify.json")):
        assert writes(started, role, f".plumbline/runs/other/{path}"), role


def test_the_active_file_decides_which_run_when_two_exist(adopted):
    start_run(adopted, "r1", activate=False)
    start_run(adopted, "r2", activate=False)
    os.utime(adopted / ".plumbline" / "runs" / "r1", (1_000_000_000, 1_000_000_000))
    for child in (adopted / ".plumbline" / "runs" / "r1").iterdir():
        os.utime(child, (1_000_000_000, 1_000_000_000))
    # no ACTIVE file: the newest run
    assert writes(adopted, "builder", ".plumbline/runs/r2/build.json") is None
    assert writes(adopted, "builder", ".plumbline/runs/r1/build.json")
    activate_run(adopted, "r1")
    assert writes(adopted, "builder", ".plumbline/runs/r1/build.json") is None
    assert writes(adopted, "builder", ".plumbline/runs/r2/build.json")


@pytest.mark.parametrize("content", ["gone\n", "", "\n", "../r1\n", "r1 r2\n", "r1/../r2\n", "x" * 300 + "\n"])
def test_a_missing_or_dangling_active_file_means_the_newest_run(adopted, content):
    start_run(adopted, "r1", activate=False)
    start_run(adopted, "r2", activate=False)
    os.utime(adopted / ".plumbline" / "runs" / "r1", (1_000_000_000, 1_000_000_000))
    for child in (adopted / ".plumbline" / "runs" / "r1").iterdir():
        os.utime(child, (1_000_000_000, 1_000_000_000))
    write(adopted / ".plumbline" / "runs" / "ACTIVE", content)
    assert writes(adopted, "builder", ".plumbline/runs/r2/build.json") is None
    assert writes(adopted, "builder", ".plumbline/runs/r1/build.json")


def test_an_agent_has_no_run_to_write_in_before_one_has_begun(adopted):
    reason = writes(adopted, "builder", ".plumbline/runs/r1/build.json")
    assert reason and "no run is in progress" in reason and "plumbline.py plan --intent" in reason


def test_a_new_run_directory_does_not_unhide_the_tests_of_an_older_one(started):
    put(started, "plan", spec_record())
    put(started, "tests", written_tests_record())
    assert read_of(started, ".plumbline/runs/r1/tests.json")
    put(started, "build", build_note_record(), "zzz")  # a run that is newer by mtime
    assert pl.latest_run_id(started) == "zzz"
    assert read_of(started, ".plumbline/runs/r1/tests.json")
    assert read_of(started, "tests/test_app.py")


# ------------------------------------- BB-RECORDS and the read side of C-06: what the builder reads under .plumbline/


@pytest.fixture
def run_with_everything(started):
    """Run r1 with every record a run makes, a second run, and what a test run leaves behind."""
    repo = started
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    put(repo, "build", build_note_record())
    put(repo, "verify", verify_record())
    put(repo, "review", {"target": "diff"})
    put(repo, "test-review", {"target": "tests"})
    put_part(repo, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    put_part(repo, "review", "prosecutor-security", {"lens": "security", "findings": []})
    write(repo / ".plumbline" / "supplied-spec.json", "{}")
    write(repo / ".plumbline" / "pass" / "abc.json", "{}")
    put(repo, "intake", intake_record("code.S", repo), "r0")
    put(repo, "plan", spec_record(), "r0")
    put(repo, "build", build_note_record(), "r0")
    write(repo / ".pytest_cache" / "v" / "cache" / "nodeids", "[]")
    write(repo / ".pytest_cache" / "v" / "cache" / "lastfailed", "{}")
    write(repo / "junit.xml", "<testsuite/>")
    write(repo / "reports" / "junit-unit.xml", "<testsuite/>")
    write(repo / ".coverage", "x")
    write(repo / ".coverage.host.123", "x")
    write(repo / "htmlcov" / "index.html", "<html/>")
    write(repo / ".tox" / "py311" / "log" / "1.log", "x")
    write(repo / "pkg" / ".pytest_cache" / "v" / "n", "x")
    activate_run(repo, "r1")
    return repo


READABLE = [".plumbline/runs/r1/intake.json", ".plumbline/runs/r1/plan.json", ".plumbline/runs/r1/build.json", "src/app.py", "README.md"]
HIDDEN = [
    ".plumbline/runs/r1/tests.json", ".plumbline/runs/r1/verify.json", ".plumbline/runs/r1/review.json", ".plumbline/runs/r1/test-review.json",
    ".plumbline/runs/r1/test-review/round-1/prosecutor-tests.json", ".plumbline/runs/r1/review/round-1/prosecutor-security.json", ".plumbline/runs/r1/ledger.jsonl",
    ".plumbline/runs/r0/intake.json", ".plumbline/runs/r0/plan.json", ".plumbline/runs/r0/build.json", ".plumbline/supplied-spec.json", ".plumbline/pass/abc.json",
    ".plumbline/runs/ACTIVE", ".pytest_cache/v/cache/nodeids", ".pytest_cache/v/cache/lastfailed", "junit.xml", "reports/junit-unit.xml", ".coverage",
    ".coverage.host.123", "htmlcov/index.html", ".tox/py311/log/1.log", "pkg/.pytest_cache/v/n", "tests/test_app.py",
]


@pytest.mark.parametrize("name", READABLE)
def test_the_builder_reads_the_active_runs_intake_plan_and_its_own_record_and_the_source(run_with_everything, name):
    assert read_of(run_with_everything, name) is None, name


@pytest.mark.parametrize("name", HIDDEN)
def test_the_builder_reads_nothing_else_under_plumbline_and_no_test_leftovers(run_with_everything, name):
    assert read_of(run_with_everything, name), name


def test_a_record_the_builder_may_read_is_not_denied_before_it_exists(started):
    # `plan --intent` writes only the intake record: the plan and the builder's own record come later, and a path is judged by its own name
    assert not (started / ".plumbline" / "runs" / "r1" / "plan.json").exists()
    for name in (".plumbline/runs/r1/plan.json", ".plumbline/runs/r1/build.json", ".plumbline/runs/r1/intake.json"):
        assert read_of(started, name) is None, name
    assert read_of(started, ".plumbline/runs/r1/tests.json")  # it may not read this one, there or not
    assert read_of(started, ".plumbline/runs/r1/verify.json")
    assert read_of(started, "tests/not_there_yet.py")  # a path under the tests is one whether or not it exists
    assert read_of(started, ".pytest_cache/not_there_yet")


def test_the_reviewers_bb_records_reproductions_are_denied_to_the_builder(run_with_everything):
    repo = run_with_everything
    for name in (".plumbline/runs/r1/test-review/round-1/prosecutor-tests.json", ".plumbline/runs/r1/test-review.json", ".plumbline/runs/r1/ledger.jsonl",
                 ".pytest_cache/v/cache/nodeids", ".pytest_cache/v/cache/lastfailed", "junit.xml"):
        assert read_of(repo, name), name
        assert reads(repo, "Grep", {"pattern": "x", "path": str(repo / name)}), name


def test_a_record_of_the_run_is_denied_with_the_three_the_builder_may_read_named(run_with_everything):
    reason = read_of(run_with_everything, ".plumbline/runs/r1/verify.json")
    assert reason == (
        "plumbline: under .plumbline/ the builder reads the active run's build.json, intake.json, plan.json and nothing else; .plumbline/runs/r1/verify.json "
        "is another of plumbline's files. Build from the spec (the plan record) and the source."
    )
    assert "what a test run leaves behind" in read_of(run_with_everything, "junit.xml")


def test_the_builder_cannot_search_a_directory_that_holds_any_of_it(run_with_everything):
    repo = run_with_everything
    for tool in ("Grep", "Glob"):
        for path in (".plumbline", ".plumbline/runs", ".plumbline/runs/r1", ".plumbline/runs/r1/review", ".plumbline/pass", ".pytest_cache", "htmlcov", ".tox", ".", str(repo), "pkg"):
            assert reads(repo, tool, {"pattern": "x", "path": path}), (tool, path)
    assert reads(repo, "Grep", {"pattern": "goal", "path": ".plumbline/runs/r1/plan.json"}) is None  # a file it may read
    assert reads(repo, "Grep", {"pattern": "x", "path": "src"}) is None
    assert reads(repo, "Glob", {"pattern": "*.py", "path": "src"}) is None


def test_a_glob_pattern_that_leads_into_plumbline_or_a_cache_is_denied(run_with_everything):
    repo = run_with_everything
    for pattern, path in ((".plumbline/**", "."), ("../.plumbline/runs/*/tests.json", "src"), ("../.pytest_cache/**", "src"), (str(repo / ".plumbline" / "runs" / "**"), "src")):
        assert reads(repo, "Glob", {"pattern": pattern, "path": path}), (pattern, path)


def test_only_the_builder_is_blind_the_other_agents_and_the_main_session_read_the_records(run_with_everything):
    repo = run_with_everything
    for agent in ("plumbline:verifier", "plumbline:prosecutor", "plumbline:defender", "plumbline:detective", "plumbline:planner", "probe:echo", None):
        assert read_of(repo, ".plumbline/runs/r1/verify.json", agent_type=agent) is None, agent
        assert read_of(repo, "junit.xml", agent_type=agent) is None, agent


# ---------------------------------------------------- transcripts: anything under ~/.claude


def test_the_builder_does_not_read_the_agent_transcripts_under_the_home_claude_directory(run_with_everything, home):
    transcript = home / ".claude" / "projects" / "slug" / "S" / "subagents" / "agent-a1.jsonl"
    write(transcript, '{"tool": "Write", "content": "def test_x(): assert False"}\n')
    repo = run_with_everything
    reason = read_of(repo, str(transcript))
    assert reason and "agent transcripts" in reason
    assert read_of(repo, str(home / ".claude" / "settings.json"))
    for tool in ("Grep", "Glob"):
        assert reads(repo, tool, {"pattern": "test_", "path": str(home / ".claude" / "projects")}), tool
        assert reads(repo, tool, {"pattern": "test_", "path": str(home)}), tool  # a search from above .claude reaches it
    assert read_of(repo, str(home / "notes.txt")) is None  # the rest of the home directory is nothing to it


def test_the_builder_still_reads_the_record_schemas_of_a_plugin_installed_under_the_home_claude_directory(run_with_everything, home, monkeypatch):
    # its agent file tells it to read ${CLAUDE_PLUGIN_ROOT}/schemas/build_note.json, and an installed plugin lives under ~/.claude/plugins
    import shutil

    plugin = home / ".claude" / "plugins" / "cache" / "plumbline" / "plumbline" / "0.4.0"
    shutil.copytree(pl.PLUGIN_ROOT / "schemas", plugin / "schemas")
    write(plugin / "agents" / "builder.md", "x\n")
    write(plugin / "scripts" / "plumbline.py", "x\n")
    monkeypatch.setattr(pl, "PLUGIN_ROOT", plugin)
    repo = run_with_everything
    assert read_of(repo, str(plugin / "schemas" / "build_note.json")) is None
    assert reads(repo, "Grep", {"pattern": "files_changed", "path": str(plugin / "schemas")}) is None
    assert reads(repo, "Glob", {"pattern": "*.json", "path": str(plugin / "schemas")}) is None
    # nothing else of the plugin's directory, and none of the transcripts beside it
    assert read_of(repo, str(plugin / "agents" / "builder.md"))
    assert read_of(repo, str(plugin / "scripts" / "plumbline.py"))
    assert reads(repo, "Grep", {"pattern": "x", "path": str(plugin)})
    assert read_of(repo, str(home / ".claude" / "projects" / "slug" / "S.jsonl"))


def test_the_builder_does_not_read_transcripts_wherever_claude_code_keeps_them(run_with_everything, tmp_path, monkeypatch):
    repo = run_with_everything
    elsewhere = tmp_path / "cfg"
    write(elsewhere / "projects" / "slug" / "S.jsonl", "x\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    assert read_of(repo, str(elsewhere / "projects" / "slug" / "S.jsonl"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    other = tmp_path / "moved-home" / ".claude"
    write(other / "projects" / "slug" / "S" / "subagents" / "agent-x.jsonl", "x\n")
    assert read_of(repo, str(other / "projects" / "slug" / "S" / "subagents" / "agent-x.jsonl"))  # by the .claude/projects shape alone
    # the event's own transcript_path says where they are
    custom = tmp_path / "custom-config"
    write(custom / "projects" / "slug" / "S" / "subagents" / "agent-y.jsonl", "x\n")
    payload = tool_payload(repo, "Read", {"file_path": str(custom / "projects" / "slug" / "S" / "subagents" / "agent-y.jsonl")}, agent_type=BUILDER,
                           transcript_path=str(custom / "projects" / "slug" / "S.jsonl"))
    assert pre.decide(payload)


# ------------------------------------------- an ancestor of the repository is a search of it


def test_a_search_from_above_the_repository_covers_it(run_with_everything, tmp_path, monkeypatch):
    repo = run_with_everything
    # keep the transcripts far from the repository, so that only the repository itself is what a search from above would reach
    monkeypatch.setenv("HOME", "/nonexistent-home")
    away = "/nonexistent-home/.claude/projects/slug/S.jsonl"
    for path in ("..", str(repo.parent), str(tmp_path)):
        for tool in ("Grep", "Glob"):
            reason = reads(repo, tool, {"pattern": "def test_", "path": path}, transcript_path=away)
            assert reason and "cover the whole repository" in reason, (tool, path)
    for tool in ("Grep", "Glob"):
        assert reads(repo, tool, {"pattern": "def test_", "path": "/"}, transcript_path=away), tool  # the root holds the repository and the transcripts
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert reads(repo, "Grep", {"pattern": "x", "path": str(outside)}, transcript_path=away) is None  # a sibling directory holds no part of it
    assert reads(repo, "Grep", {"pattern": "x", "path": str(tmp_path / "home")}, transcript_path=away) is None


# ------------------------------------------------------- BB-CWD: the target counts, not only the cwd


def test_a_builder_whose_cwd_drifted_outside_the_repository_is_held_by_the_paths_it_names(run_with_everything, tmp_path):
    repo, outside = run_with_everything, tmp_path / "elsewhere"
    outside.mkdir()
    assert read_of(repo, "tests/test_app.py", cwd=outside)
    assert read_of(repo, ".plumbline/runs/r1/tests.json", cwd=outside)
    assert reads(repo, "Grep", {"pattern": "def", "path": str(repo / "tests")}, cwd=outside)
    assert reads(repo, "Glob", {"pattern": "*.py", "path": str(repo / "tests")}, cwd=outside)
    assert writes(repo, "builder", "tests/test_new.py", cwd=outside)
    assert writes(repo, "builder", "plumbline.toml", cwd=outside)
    assert writes(repo, "builder", ".git/config", cwd=outside)
    assert writes(repo, "prosecutor", "src/app.py", cwd=outside)
    assert writes(repo, "verifier", ".plumbline/runs/r1/build.json", cwd=outside)
    # the source it may read and write is still its own
    assert read_of(repo, "src/app.py", cwd=outside) is None
    assert writes(repo, "builder", "src/app.py", cwd=outside) is None
    assert read_of(outside, str(outside / "notes.txt"), cwd=outside) is None


def test_the_protected_files_are_protected_from_a_cwd_outside_the_repository_too(adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for role in (None, "planner"):
        reason = writes(adopted, role, ".plumbline/pass/x.json", cwd=outside)
        assert reason and "written only by plumbline.py commands" in reason, role


def test_a_bash_write_to_the_protected_files_is_denied_by_its_target_from_a_cwd_outside_the_repository(adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for command in (f"echo x > {adopted}/.plumbline/pass/a.json", f"cp /tmp/x {adopted}/.plumbline/pass/a.json", f"echo x >> {adopted}/.plumbline/runs/r1/ledger.jsonl",
                    f"cd {adopted} && echo x > .plumbline/pass/a.json", f"cd {adopted}/src && echo x > ../.plumbline/pass/a.json"):
        reason = pre.decide(bash_payload(outside, command))
        assert reason and "written only by plumbline.py commands" in reason, command


def test_an_agents_bash_is_held_to_its_policy_where_its_cwd_drifted_but_its_session_is_adopted(adopted, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    payload = bash_payload(outside, "rm -rf x", agent_type="plumbline:verifier")
    assert pre.decide(payload) is None  # nothing names the repository: this is a directory of no concern
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(adopted))
    reason = pre.decide(payload)
    assert reason and reason.startswith("plumbline: the verifier's Bash may run only")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    for command in (f"git -C {adopted} checkout -- .", f"git --git-dir={adopted}/.git reset --hard", f"cd {adopted} && rm -rf src"):
        reason = pre.decide(bash_payload(outside, command, agent_type="plumbline:verifier"))
        assert reason and reason.startswith("plumbline: the verifier's Bash may run only"), command
    assert pre.decide(bash_payload(outside, f"git -C {adopted} diff", agent_type="plumbline:verifier")) is None


def test_a_cwd_that_is_a_repository_that_has_not_adopted_plumbline_is_left_alone(repo, tmp_path):
    other = tmp_path / "other-repo"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    write(other / "tests" / "x.py", "assert False\n")
    assert read_of(other, "tests/x.py", cwd=other) is None
    assert writes(other, "builder", "tests/x.py", cwd=other) is None
    assert writes(other, "builder", ".git/config", cwd=other) is None


# ------------------------------------------------- C-22: the review agents write their own files, in the current round

FILES = {"prosecutor": "prosecutor-security.json", "defender": "defender-2.json", "detective": "detective.json", "canary": "prosecutor-security-b.json"}


def round_path(stage, n, name, run="r1"):
    return f".plumbline/runs/{run}/{stage}/round-{n}/{name}"


@pytest.mark.parametrize("role", REVIEW_ROLES)
@pytest.mark.parametrize("stage", ["review", "test-review"])
def test_a_review_agent_writes_its_own_file_in_the_first_round_of_a_stage_with_no_round_yet(started, role, stage):
    assert writes(started, role, round_path(stage, 1, FILES[role])) is None
    assert writes(started, role, round_path(stage, 2, FILES[role]))  # round 2 does not exist


@pytest.mark.parametrize("role", REVIEW_ROLES)
@pytest.mark.parametrize("other", REVIEW_ROLES)
def test_a_review_agent_writes_nobody_elses_file(started, role, other):
    reason = writes(started, role, round_path("review", 1, FILES[other]))
    if role == other:
        assert reason is None
    else:
        assert reason and f"the {role} writes only its own record" in reason


@pytest.mark.parametrize(
    "role,names",
    [
        ("prosecutor", ["prosecutor-security.json", "prosecutor-tests.json", "prosecutor-a.b_c-1.json"]),
        ("defender", ["defender-1.json", "defender-3.json", "defender-x.json", "screen-1.json", "screen-k.json"]),
        ("detective", ["detective.json"]),
        ("canary", ["prosecutor-security-b.json", "prosecutor-docs-b.json", "canary-key.json"]),
    ],
)
def test_each_review_role_has_the_file_names_of_its_role_only(started, role, names):
    for name in names:
        assert writes(started, role, round_path("review", 1, name)) is None, name
    for name in ("prosecutor.json", "defender.json", "detective-1.json", "detective-x.json", "notes.json", "x.txt", "prosecutor-x.txt", "prosecutor-.json", "screen.json", "canary.json", "canary-key.txt", "prosecutor-security-c.json", "prosecutor-canary.json"):
        if name in ("prosecutor-.json", "prosecutor-security-c.json", "prosecutor-canary.json") and role == "prosecutor":
            continue  # a name of a prosecutor's own kind: any lens, any suffix but the one the canary's record carries
        assert writes(started, role, round_path("review", 1, name)), (role, name)


def test_the_round_is_the_highest_that_exists(started):
    (started / ".plumbline" / "runs" / "r1" / "review" / "round-1").mkdir(parents=True)
    (started / ".plumbline" / "runs" / "r1" / "review" / "round-2").mkdir()
    for role in REVIEW_ROLES:
        assert writes(started, role, round_path("review", 2, FILES[role])) is None, role
        old = writes(started, role, round_path("review", 1, FILES[role]))
        assert old and "current round of review (round-2)" in old and "round-1" in old, role
        assert writes(started, role, round_path("review", 3, FILES[role])), role
    (started / ".plumbline" / "runs" / "r1" / "review" / "round-10").mkdir()
    assert writes(started, "defender", round_path("review", 10, "defender-1.json")) is None  # numerically, not as text
    assert writes(started, "defender", round_path("review", 9, "defender-1.json"))


def test_the_rounds_of_the_two_review_stages_are_counted_apart(started):
    (started / ".plumbline" / "runs" / "r1" / "test-review" / "round-2").mkdir(parents=True)
    assert writes(started, "prosecutor", round_path("test-review", 2, "prosecutor-tests.json")) is None
    assert writes(started, "prosecutor", round_path("test-review", 1, "prosecutor-tests.json"))
    assert writes(started, "prosecutor", round_path("review", 1, "prosecutor-security.json")) is None  # the review stage has no round yet


def test_a_review_agent_writes_in_the_active_run_only(started):
    start_run(started, "r2", activate=False)
    for role in REVIEW_ROLES:
        reason = writes(started, role, round_path("review", 1, FILES[role], run="r2"))
        assert reason and "active run (r1)" in reason, role
    activate_run(started, "r2")
    assert writes(started, "prosecutor", round_path("review", 1, FILES["prosecutor"], run="r2")) is None


def test_a_review_agent_writes_no_stage_record_and_nothing_outside_a_round_directory(started):
    for role in REVIEW_ROLES:
        for path in (".plumbline/runs/r1/review.json", ".plumbline/runs/r1/review/prosecutor-x.json", ".plumbline/runs/r1/review/round-0/prosecutor-x.json",
                     ".plumbline/runs/r1/review/round-1/deep/prosecutor-x.json", ".plumbline/runs/r1/other-stage/round-1/prosecutor-x.json", ".plumbline/runs/r1/ledger.jsonl"):
            assert writes(started, role, path), (role, path)


def test_the_stage_agents_cannot_write_into_a_round_directory(started):
    for role in ("planner", "test-writer", "builder", "verifier"):
        assert writes(started, role, round_path("review", 1, "prosecutor-x.json")), role


# --------------------------------------------- the same rules, through the hook's command line

def test_the_lane_and_blindness_rules_hold_through_the_hook_process_too(run_pre, run_with_everything):
    repo = run_with_everything
    from hookdata import denial

    write_payload = tool_payload(repo, "Write", {"file_path": str(repo / ".git" / "config"), "content": "x"}, agent_type=BUILDER)
    assert "Git commands run by the main session" in denial(run_pre(write_payload, repo))
    read_payload = tool_payload(repo, "Read", {"file_path": str(repo / ".plumbline" / "runs" / "r1" / "verify.json")}, agent_type=BUILDER)
    assert "under .plumbline/" in denial(run_pre(read_payload, repo))
    other = tool_payload(repo, "Read", {"file_path": str(repo / ".plumbline" / "runs" / "r1" / "plan.json")}, agent_type=BUILDER)
    assert denial(run_pre(other, repo)) is None


# ------------------------------------------------- the write tools' path keys, the rest of what a test run leaves, cp into a directory


@pytest.mark.parametrize("role", [None, "builder", "verifier"])
def test_every_path_key_of_a_write_tools_input_is_looked_at(started, role):
    # NotebookEdit takes notebook_path; a call that also carries file_path must not hide the notebook behind an innocent file
    for tool_input in (
        {"file_path": str(started / "src" / "x.py"), "notebook_path": str(started / ".git" / "x.ipynb"), "new_source": "x"},
        {"notebook_path": str(started / "src" / "x.ipynb"), "file_path": str(started / ".claude" / "settings.json"), "new_source": "x"},
    ):
        if role is None:
            continue  # the main session may write the lane files
        reason = pre.decide(tool_payload(started, "NotebookEdit", tool_input, agent_type=f"plumbline:{role}"))
        assert reason, (role, tool_input)
        if role == "builder":  # it may write src/x.py, so what it is denied is the other key's path
            assert "main session" in reason, (role, tool_input)
    reason = pre.decide(tool_payload(started, "Write", {"file_path": str(started / "src" / "x.py"), "notebook_path": str(started / ".plumbline" / "pass" / "x.json"), "content": "x"}, agent_type=None if role is None else f"plumbline:{role}"))
    assert reason, role
    if role != "verifier":  # the verifier is refused src/x.py first, for its own reason
        assert "written only by plumbline.py commands" in reason  # the protected files are protected for everyone, by either key


@pytest.mark.parametrize(
    "name",
    ["coverage.xml", "reports/coverage.xml", "lcov.info", ".nyc_output/out.json", ".hypothesis/examples/x", "test-results/a.xml", "test-reports/a.html", "pytest-report.xml", "pytest-report-1.html",
     "conftest.py", "src/conftest.py", "pkg/sub/conftest.py"],
)
def test_the_builder_does_not_read_more_of_what_a_test_run_leaves_or_the_conftest(started, name):
    write(started / name, "x\n")
    assert read_of(started, name), name
    assert reads(started, "Grep", {"pattern": "x", "path": name}), name


@pytest.mark.parametrize("name", ["pyproject.toml", "setup.cfg", "tox.ini", "src/app.py", "docs/coverage-guide.md", "src/coverage_tools.py", "Makefile", "package.json"])
def test_the_builder_still_reads_what_configures_the_project(started, name):
    write(started / name, "x\n")
    assert read_of(started, name) is None, name


def test_a_file_copied_or_moved_into_a_directory_lands_where_the_destination_says(adopted, tmp_path):
    # OVERRIDE-FORGE: `cp -r /tmp/s/pass .plumbline/` writes .plumbline/pass, which no operand of the command names
    (adopted / ".plumbline").mkdir(exist_ok=True)
    (adopted / ".plumbline" / "runs" / "r1").mkdir(parents=True, exist_ok=True)
    forged = tmp_path / "s" / "pass"
    forged.mkdir(parents=True)
    write(forged / f"{'0' * 40}.override.json", "{}")
    ledger = tmp_path / "s" / "ledger.jsonl"
    write(ledger, "{}\n")
    for command in (
        f"cp -r {forged} .plumbline/",
        f"cp -r {forged} .plumbline",
        f"cp -a {forged} {adopted}/.plumbline/",
        f"cp -t .plumbline -r {forged}",
        f"cp --target-directory=.plumbline -r {forged}",
        f"mv {forged} .plumbline/",
        f"rsync -a {forged} .plumbline/",
        f"ln -s {forged} .plumbline/",
        f"cp {ledger} .plumbline/runs/r1/",
        f"mv {ledger} .plumbline/runs/r1",
        f"cd .plumbline && cp -r {forged} .",
        f"cd .plumbline/runs/r1 && cp {ledger} ./",
    ):
        reason = pre.decide(bash_payload(adopted, command))
        assert reason and "written only by plumbline.py commands" in reason, command
    for command in (
        f"cp -r {tmp_path / 's'} .plumbline/",  # lands as .plumbline/s
        "cp README.md .plumbline/",
        f"cp {forged}/x.json .plumbline/runs/r1/",
        f"cp -r {forged} docs/",
        f"cp -r {forged} .plumbline/backup",  # a destination that does not exist yet is the name itself
    ):
        assert pre.decide(bash_payload(adopted, command)) is None, command
    assert pre.landing_places(pre.Step(["cp", "-r", "/tmp/a/pass", "/tmp/a/x", "dir/"], ["cp", "-r", "/tmp/a/pass", "/tmp/a/x", "dir/"], [], Path("/w"))) == ["dir/pass", "dir/x"]
    assert pre.landing_places(pre.Step(["cp", "a", "b"], ["cp", "a", "b"], [], Path("/nonexistent"))) == []


# ------------------------------------------- what lands under names the command line does not show: globs, contents, archives, downloads


def test_a_copy_of_a_glob_or_of_the_contents_of_a_directory_into_plumbline_is_denied(adopted, tmp_path):
    (adopted / ".plumbline" / "runs" / "r1").mkdir(parents=True, exist_ok=True)
    forged = tmp_path / "s"
    write(forged / "pass" / f"{'0' * 40}.override.json", "{}")
    for command in (
        f"cp -r {forged}/* .plumbline/",
        f"cp -r {forged}/. .plumbline/",
        f"cp -rT {forged} .plumbline",
        f"cp -r --no-target-directory {forged} .plumbline",
        f"cp {forged}/pass/* .plumbline/runs/r1/",
        f"rsync -a {forged}/ .plumbline/",
        f"rsync -a {forged}/pass/ .plumbline/runs/r1/",
        f"mv {forged}/* .plumbline/",
        f"cp -r {forged}/* {adopted}/.plumbline",
        f"cd .plumbline && cp -r {forged}/* .",
        f"cp -t .plumbline -r {forged}/*",
    ):
        reason = pre.decide(bash_payload(adopted, command))
        assert reason and "written only by plumbline.py commands" in reason, command
    for command in (
        f"cp -r {forged}/* docs/",
        f"cp -r {forged}/. docs/",
        f"rsync -a {forged}/ build/",
        f"cp -rT {forged} scratch",
        f"cp {forged}/pass/x.json .plumbline/backup-of-nothing.json",
    ):
        assert pre.decide(bash_payload(adopted, command)) is None, command


def test_an_archive_a_patch_or_a_download_written_into_plumbline_is_denied(adopted, tmp_path):
    (adopted / ".plumbline" / "runs" / "r1").mkdir(parents=True, exist_ok=True)
    for command in (
        "tar -xf x.tar -C .plumbline",
        "tar xf x.tar -C .plumbline",
        "tar -xzf x.tgz --directory=.plumbline",
        "tar --extract --file=x.tar --directory .plumbline",
        "tar -xf x.tar -C .plumbline/runs/r1",
        "cd .plumbline && tar -xf ../x.tar",
        "cd .plumbline/runs/r1 && tar xf ../../../x.tar",
        "unzip x.zip -d .plumbline",
        "unzip -o x.zip -d .plumbline/runs/r1",
        "cd .plumbline && unzip ../x.zip",
        "patch -d .plumbline < x.diff",
        "cd .plumbline && patch -p1 < ../x.diff",
        "cd .plumbline/runs/r1 && patch < ../../../x.diff",
        "patch -d .plumbline/runs/r1 -p0 < x.diff",
        "curl -o .plumbline/pass/x.json http://example.invalid/x",
        "curl --output .plumbline/pass/x.json http://example.invalid/x",
        "curl -o x.json --output-dir .plumbline/pass http://example.invalid/x",
        "wget -O .plumbline/pass/x.json http://example.invalid/x",
        "wget --output-document=.plumbline/runs/r1/ledger.jsonl http://example.invalid/x",
        "wget -P .plumbline/pass http://example.invalid/x",
    ):
        reason = pre.decide(bash_payload(adopted, command))
        assert reason and "written only by plumbline.py commands" in reason, command
    for command in (
        "tar -xf x.tar -C /tmp/out",
        "tar -xf x.tar -C docs",
        "tar -tf x.tar",
        "tar -cf x.tar src",
        "tar cf x.tar .plumbline",
        "unzip x.zip -d build",
        "unzip -l x.zip",
        "patch -d src < x.diff",
        "patch -p1 < x.diff",
        "cd docs && patch -p1 < ../x.diff",
        "curl -o out.json http://example.invalid/x",
        "curl -O http://example.invalid/x",
        "wget -O docs/x.html http://example.invalid/x",
        "wget -P downloads http://example.invalid/x",
    ):
        assert pre.decide(bash_payload(adopted, command)) is None, command
