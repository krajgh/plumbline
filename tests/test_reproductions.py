"""The 2b review's reproductions, piped into the exact command hooks/hooks.json registers (the sh filter included), in an adopted
repository with a real local bare origin. Each row is one reproduction from review-2b/enforcement.md (or its scratch scripts) that
0.3.0 allowed; each is denied now, with a reason that says what to do instead. The finding ids are the ids of the review."""
import json
import os
import subprocess

import pytest

from helpers import CLI, REPO, clean_env, commit_all, git, write
from hookdata import add_origin, remote_ref, start_run, tool_payload
from rundata import adopt_base, build_note_record, genuine_pass, put, put_part, spec_record, verify_record, written_tests_record

REGISTERED = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
REASON = "twenty characters or more of reason"


class World:
    """An adopted repository (the reviewer's `world()`): a declared test command, a bare origin, a run in progress with the records a run makes."""

    def __init__(self, repo, origin, home, tmp_path):
        self.repo, self.origin, self.home, self.tmp_path = repo, origin, home, tmp_path

    def run(self, payload, cwd=None, **env):
        """The registered command, as Claude Code runs it, with `cwd` as its working directory."""
        environment = clean_env(self.home, CLAUDE_PLUGIN_ROOT=str(REPO), GIT_CEILING_DIRECTORIES=str(self.tmp_path), **env)
        result = subprocess.run(REGISTERED, shell=True, cwd=cwd or self.repo, capture_output=True, text=True, input=json.dumps(payload), env=environment)
        assert result.returncode == 0 and result.stderr == "", result.stderr
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def event(self, tool, tool_input, agent=None, cwd=None):
        return tool_payload(cwd or self.repo, tool, tool_input, agent_type=f"plumbline:{agent}" if agent else None)

    def bash(self, command, agent=None, cwd=None, **env):
        return self.run(self.event("Bash", {"command": command, "description": "run it"}, agent, cwd), cwd=cwd, **env)

    def path(self, name):
        return str(self.repo / name)


@pytest.fixture
def world(repo, home, tmp_path):
    write(repo / "run_tests.sh", "#!/bin/sh\necho tests ran\n")
    write(repo / "lint.sh", "#!/bin/sh\necho lint ran\n")
    write(repo / "tests" / "test_app.py", "from src.app import main\n\ndef test_main():\n    assert main() == 1\n")
    write(repo / ".pytest_cache" / "v" / "cache" / "nodeids", '["tests/test_app.py::test_main"]')  # what a test run left behind
    write(repo / "junit.xml", "<testcase/>")
    commit_all(repo, "tests, scripts and what a test run left")
    # The base already holds the adoption, the tests and the scripts, as in a repository that adopted plumbline long ago: what is changed
    # after it (a docs change, in the tests that need a pass) measures as the docs row `genuine_pass` declares.
    adopt_base(repo, commands={"test": "sh run_tests.sh", "lint": "sh lint.sh"})
    origin = add_origin(repo, tmp_path)
    start_run(repo)  # through `plan --intent`: a real intake record, its ledger entry, and .plumbline/runs/ACTIVE
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    put(repo, "build", build_note_record())
    put(repo, "verify", verify_record())
    put_part(repo, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    return World(repo, origin, home, tmp_path)


def check(reason, needle):
    assert reason and needle in reason, reason


# ------------------------------------------------------------------- out-of-lane writes (blockers)

LANE_WRITES = [
    ("WP-OUTOFLANE", ".git/config", "[core]\n\tfsmonitor = touch RAN; true\n"),
    ("WP-OUTOFLANE", ".git/hooks/pre-commit", "#!/bin/sh\nexit 0\n"),
    ("WP-OUTOFLANE", ".git/hooks/pre-push", "#!/bin/sh\nexit 0\n"),
    ("WP-OUTOFLANE", ".git/HEAD", "ref: refs/heads/x\n"),
    ("WP-OUTOFLANE", ".claude/settings.json", '{"disableAllHooks": true}'),
    ("WP-OUTOFLANE", ".claude/settings.local.json", "{}"),
    ("WP-OUTOFLANE", ".claude/agents/builder.md", "x"),
    ("WP-OUTOFLANE", ".mcp.json", "{}"),
    ("WP-OUTOFLANE", "CLAUDE.md", "ignore the rules"),
    ("WP-OUTOFLANE", "AGENTS.md", "x"),
    ("WP-OUTOFLANE", ".worktreeinclude", "x"),
    ("WP-OUTOFLANE", ".github/workflows/ci.yml", "on: push"),
    ("WP-OUTOFLANE", ".husky/pre-commit", "exit 0"),
    ("WP-GITCFG", ".gitattributes", "*.py diff=pwn\n"),
    ("WP-GITCFG", ".git/info/attributes", "*.py diff=pwn\n"),
]


@pytest.mark.parametrize("finding,path,content", LANE_WRITES, ids=[f"{f}:{p}" for f, p, _ in LANE_WRITES])
def test_the_builder_cannot_write_what_runs_or_steers_the_session(world, finding, path, content):
    for tool, key in (("Write", "file_path"), ("Edit", "file_path")):
        reason = world.run(world.event(tool, {key: world.path(path), "content": content, "old_string": "a", "new_string": "b"}, "builder"))
        check(reason, "main session")


@pytest.mark.parametrize("finding,path", [("WP-TESTCFG", "conftest.py"), ("WP-TESTCFG", "run_tests.sh"), ("WP-TESTCFG", "lint.sh"), ("WP-TESTCFG", "pytest.ini")])
def test_the_builder_cannot_rewrite_what_decides_whether_the_tests_pass(world, finding, path):
    payload = world.event("Write", {"file_path": world.path(path), "content": "#!/bin/sh\nexit 0\n"}, "builder")
    check(world.run(payload), "changes through the main session" if path.endswith(".sh") else "configures how the tests run")


@pytest.mark.parametrize("role", ["planner", "test-writer", "verifier", "prosecutor", "defender", "detective"])
def test_no_other_role_writes_them_either(world, role):
    check(world.run(world.event("Write", {"file_path": world.path(".claude/settings.json"), "content": "{}"}, role)), "main session")


# --------------------------------------------------------------------------- builder blindness (blockers)


@pytest.mark.parametrize(
    "finding,name",
    [
        ("BB-RECORDS", ".plumbline/runs/r1/test-review/round-1/prosecutor-tests.json"),
        ("BB-RECORDS", ".plumbline/runs/r1/ledger.jsonl"),
        ("BB-RECORDS", ".plumbline/runs/r1/verify.json"),
        ("BB-RECORDS", ".pytest_cache/v/cache/nodeids"),
        ("BB-RECORDS", "junit.xml"),
        ("C-06", ".plumbline/runs/r1/verify.json"),
    ],
)
def test_the_builder_reads_neither_the_review_of_the_tests_nor_what_a_test_run_leaves(world, finding, name):
    check(world.run(world.event("Read", {"file_path": world.path(name)}, "builder")), "builder")
    check(world.run(world.event("Grep", {"pattern": "assert", "path": world.path(name)}, "builder")), "builder")


def test_the_builder_reads_no_agent_transcript(world):
    transcript = world.home / ".claude" / "projects" / "slug" / "S" / "subagents" / "agent-a1.jsonl"
    write(transcript, '{"content": "def test_x(): assert False"}\n')
    check(world.run(world.event("Read", {"file_path": str(transcript)}, "builder")), "agent transcripts")
    check(world.run(world.event("Grep", {"pattern": "test_", "path": str(world.home / ".claude" / "projects")}, "builder")), "agent transcripts")


def test_a_new_run_directory_does_not_open_the_real_runs_tests_record(world):
    check(world.run(world.event("Write", {"file_path": world.path(".plumbline/runs/zzz/build.json"), "content": "{}"}, "builder")), "active run (r1)")
    put(world.repo, "build", build_note_record(), "zzz")  # the newest run by mtime, as the reviewer made it
    check(world.run(world.event("Read", {"file_path": world.path(".plumbline/runs/r1/tests.json")}, "builder")), "blind to the tests")


def test_what_the_builder_reads_it_still_reads(world):
    for name in (".plumbline/runs/r1/intake.json", ".plumbline/runs/r1/plan.json", ".plumbline/runs/r1/build.json", "src/app.py"):
        assert world.run(world.event("Read", {"file_path": world.path(name)}, "builder")) is None, name
    assert world.run(world.event("Write", {"file_path": world.path("src/new.py"), "content": "x"}, "builder")) is None
    assert world.run(world.event("Write", {"file_path": world.path(".plumbline/runs/r1/build.json"), "content": "x"}, "builder")) is None


def test_bb_cwd_agents_whose_working_directory_left_the_repository_are_still_held(world, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    check(world.run(world.event("Write", {"file_path": world.path("tests/test_new.py"), "content": "x"}, "builder", cwd=outside), cwd=outside), "test path")
    check(world.run(world.event("Write", {"file_path": world.path("plumbline.toml"), "content": "x"}, "builder", cwd=outside), cwd=outside), "main session")
    check(world.run(world.event("Read", {"file_path": world.path("tests/test_app.py")}, "builder", cwd=outside), cwd=outside), "blind to the tests")
    check(world.run(world.event("Grep", {"pattern": "def", "path": world.path("tests")}, "builder", cwd=outside), cwd=outside), "blind to the tests")
    check(world.run(world.event("Write", {"file_path": world.path("src/app.py"), "content": "x"}, "prosecutor", cwd=outside), cwd=outside), "prosecutor writes")
    check(world.bash(f"git -C {world.repo} checkout -- .", "verifier", cwd=outside), "verifier's Bash may run only")
    check(world.bash("rm -rf x", "verifier", cwd=outside, CLAUDE_PROJECT_DIR=str(world.repo)), "verifier's Bash may run only")


# ------------------------------------------------------------------------------------ the push gate


def test_pg_cd_a_cd_that_only_seems_to_move_the_shell_does_not_open_the_push_gate(world):
    for command in (
        "(cd /tmp && true); git push origin feature",
        "pushd /tmp >/dev/null; popd >/dev/null; git push origin feature",
        "cd /tmp; cd -; git push origin feature",
        "false && cd /tmp; git push origin feature",
        "true || cd /tmp; git push origin feature",
    ):
        check(world.bash(command), "has no pass or override record")
    assert remote_ref(world.origin, "feature") is None


def test_pg_dotgit_the_git_directory_and_a_repository_git_does_not_trust_are_gated(world):
    check(world.bash("git push origin feature", cwd=world.repo / ".git"), "has no pass or override record")
    check(world.bash("git push origin feature", cwd=world.repo / ".git" / "hooks"), "has no pass or override record")
    probe = subprocess.run(["git", "-C", str(world.repo), "rev-parse", "--show-toplevel"], capture_output=True, env={**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"})
    if probe.returncode != 0:  # this git knows the switch that makes it refuse the repository as one owned by someone else
        check(world.bash("git push origin feature", GIT_TEST_ASSUME_DIFFERENT_OWNER="1"), "git cannot read")


def test_pg_ref_the_ref_a_push_names_is_what_needs_a_pass(world):
    git(world.repo, "checkout", "-q", "-b", "other", "main")
    write(world.repo / "src" / "secret.py", "TOKEN = 'unreviewed'\n")
    commit_all(world.repo, "unreviewed work")
    git(world.repo, "checkout", "-q", "feature")
    write(world.repo / "README.md", "# demo\nreviewed docs change\n")
    commit_all(world.repo, "reviewed docs")
    genuine_pass(world.repo)
    assert world.bash("git push origin feature") is None  # the covered ref goes
    check(world.bash("git push origin other"), "has no pass or override record")
    check(world.bash("git push --force origin other:main"), "has no pass or override record")
    check(world.bash("git push --all origin"), "push one reviewed branch at a time")
    check(world.bash("git push --mirror origin"), "push one reviewed branch at a time")
    check(world.bash("git push --tags"), "push one reviewed branch at a time")
    assert world.bash("git push origin :other") is None  # deleting a remote ref publishes nothing
    git(world.repo, "push", "-q", "origin", "feature")
    assert remote_ref(world.origin, "feature") == git(world.repo, "rev-parse", "HEAD").strip()
    assert remote_ref(world.origin, "other") is None and remote_ref(world.origin, "main") != git(world.repo, "rev-parse", "other").strip()


def test_pg_alias_an_alias_is_a_push(world):
    check(world.bash("git -c alias.p=push p origin feature"), "has no pass or override record")
    git(world.repo, "config", "alias.ship", "push origin HEAD")
    check(world.bash("git ship"), "has no pass or override record")


@pytest.mark.parametrize(
    "command",
    [
        "git grep --open-files-in-p='touch pwn3;true' return",
        "git grep --open-files='touch pwn4;true' return",
        "git grep --open='touch pwn5;true' return",
        "git grep --ope='touch pwn5b;true' return",
        "git diff --outp=out4.txt",
        "git log --outpu=out6.txt",
        "GIT_EXTERNAL_DIFF='touch pwn7;true' git diff",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external GIT_CONFIG_VALUE_0='touch pwn9;true' git diff",
        "env GIT_EXTERNAL_DIFF='touch pwn8;true' git diff",
    ],
    ids=lambda c: c[:40],
)
@pytest.mark.parametrize("role", ["verifier", "prosecutor", "planner", "defender", "detective"])
def test_pg_gitexec_a_git_read_that_runs_a_program_is_denied(world, role, command):
    check(world.bash(command, role), "Bash may run only" if "GIT_" not in command else "no VAR=value before them")


def test_the_denied_git_reads_left_no_marker_behind(world):
    for name in ("pwn3", "pwn4", "pwn5", "pwn5b", "out4.txt", "out6.txt", "pwn7", "pwn9", "pwn8"):
        assert not (world.repo / name).exists(), name


def test_ov_abbr_an_abbreviated_reason_option_is_the_override_too(world):
    check(world.bash(f'P={CLI}; python3 "$P" override --rea "{REASON}"'), "the builder's command")
    check(world.bash(f'python3 {CLI} override --reason "{REASON}" --proj {world.repo}', cwd=world.tmp_path), "the builder's command")


def test_shf_tools_a_monitor_is_read_like_bash(world):
    payload = world.event("Monitor", {"command": "git push origin feature", "description": "x", "timeout_ms": 1000})
    check(world.run(payload), "has no pass or override record")
    assert "Monitor" in json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["matcher"].split("|")


def test_a_commit_message_with_an_apostrophe_does_not_hide_the_push_that_follows(world):
    command = "git commit -q -m \"$(cat <<'EOF'\nFix: don't crash\nEOF\n)\" && git push origin feature"
    check(world.bash(command), "push")  # denied for the commit-then-push, as ever, and no longer unseen
    push = "echo \"$(cat <<'EOF'\ndon't\nEOF\n)\"; git push origin feature"
    check(world.bash(push), "has no pass or override record")
    check(world.bash("echo $'it\\'s'; rm canary", "verifier"), "Bash may run only")


# ------------------------------------------------------------------------------------ agent launches


def test_ag_isol_ag_model_ag_role_launches_are_held_to_the_plan(world):
    check(world.run(world.event("Agent", {"description": "x", "prompt": "y", "subagent_type": "plumbline:builder", "isolation": "remote"})), "without `isolation`")
    check(world.run(world.event("Agent", {"description": "x", "prompt": "y", "subagent_type": "plumbline:prosecutor", "model": "haiku"})), "pinned to the sonnet model")
    brief = "Read the plan at .plumbline/runs/r1/plan.json and write the code."
    check(world.run(world.event("Agent", {"description": "x", "prompt": brief, "subagent_type": "general-purpose"})), "plumbline stages run through the plumbline:* agents")
    check(world.run(world.event("Agent", {"description": "x", "prompt": brief})), "plumbline stages run through the plumbline:* agents")
    assert world.run(world.event("Agent", {"description": "x", "prompt": brief, "subagent_type": "plumbline:builder"})) is None


# ------------------------------------------------------------------- the review partitions (C-22)


def test_c22_a_review_agent_writes_only_its_own_files_in_the_current_round(world):
    rounds = world.repo / ".plumbline" / "runs" / "r1" / "review"
    (rounds / "round-1").mkdir(parents=True)

    def writes(role, name, n=1):
        payload = world.event("Write", {"file_path": str(rounds / f"round-{n}" / name), "content": "{}"}, role)
        return world.run(payload)

    check(writes("defender", "prosecutor-correctness.json"), "the defender writes only its own record")
    check(writes("prosecutor", "defender-1.json"), "the prosecutor writes only its own record")
    check(writes("detective", "prosecutor-data.json"), "the detective writes only its own record")
    check(writes("defender", "defender-2.json", n=2), "current round of review (round-1)")
    assert writes("defender", "defender-2.json") is None
    assert writes("prosecutor", "prosecutor-security.json") is None
    assert writes("detective", "detective.json") is None


# -------------------------------------------------------------------------------- the controls


def test_a_covered_head_still_pushes_for_real_through_the_registered_command(world):
    write(world.repo / "README.md", "# demo\nreviewed docs change\n")
    commit_all(world.repo, "docs")
    genuine_pass(world.repo)
    assert world.bash("git push origin feature") is None
    result = subprocess.run(["git", "push", "-q", "origin", "feature"], cwd=world.repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert remote_ref(world.origin, "feature") == git(world.repo, "rev-parse", "HEAD").strip()


def test_ordinary_work_is_not_held_up(world):
    for command in ("ls -la", "cat README.md", "python3 -m pytest -q", "git status", "git log --oneline -3", "git diff"):
        assert world.bash(command) is None, command
    for command in ("git diff", "git log --oneline -3", "sh run_tests.sh", "grep -rn main src"):
        assert world.bash(command, "verifier") is None, command
    assert world.run(world.event("Write", {"file_path": world.path("src/app.py"), "content": "x"})) is None


# ------------------------------------------------------------------ the second review pass of the hook (adversarial, on this fix-up)


@pytest.fixture
def covered(world):
    """HEAD (feature) has a genuine pass; `other` holds unreviewed work."""
    git(world.repo, "checkout", "-q", "-b", "other", "main")
    write(world.repo / "src" / "secret.py", "TOKEN = 'unreviewed'\n")
    commit_all(world.repo, "unreviewed work on other")
    git(world.repo, "checkout", "-q", "feature")
    write(world.repo / "README.md", "# demo\nreviewed docs change\n")
    commit_all(world.repo, "reviewed docs")
    genuine_pass(world.repo)
    return world


@pytest.mark.parametrize(
    "finding,command",
    [
        ("PG-MULTI", "git push origin :"),
        ("PG-MULTI", "git push origin +:"),
        ("PG-REFCREATE", "git branch pub other && git push origin pub"),
        ("PG-REFCREATE", "git tag -f covered-tag other && git push origin covered-tag"),
        ("PG-REFCREATE", "git update-ref refs/heads/feature other && git push origin feature"),
        ("PG-FLAGS", "git push --dry-run --no-dry-run origin other"),
        ("PG-FLAGS", "git push origin ':/unreviewed work on other:refs/heads/x'"),
        ("PG-FLAGS", "git send-pack ../origin.git other:refs/heads/x"),
        ("PG-ALIAS", "git config alias.p push && git p origin other"),
        ("PG-ALIAS", "GIT_CONFIG_PARAMETERS=\"'alias.p=push'\" git p origin other"),
        ("PG-WRAP", "env --unset FOO git push origin other"),
        ("PG-WRAP", "timeout --signal KILL 10 git push origin other"),
        ("PG-WRAP", "nice --adjustment 5 git push origin other"),
        ("PG-WRAP", "stdbuf -oL git push origin other"),
        ("PG-WRAP", "flock /tmp/l git push origin other"),
        ("PG-WRAP", "coproc git push origin other"),
        ("PG-WRAP", "trap 'git push origin other' EXIT"),
        ("PG-FILTER", "git-push origin other"),
        ("PG-FILTER", "/usr/lib/git-core/git-push origin other"),
        ("PG-FILTER", "git>/dev/null push origin other"),
        ("PG-FILTER", "git.exe push origin other"),
        ("PS-PARSE", "cmd /c git push origin other"),
        ("PS-PARSE", "iex 'git push origin other'"),
    ],
    ids=lambda v: v if isinstance(v, str) and v.startswith("PG") or isinstance(v, str) and v.startswith("PS") else v[:36],
)
def test_the_reviewers_second_pass_pushes_of_an_unreviewed_branch_are_denied(covered, finding, command):
    reason = covered.bash(command)
    assert reason and ("has no pass or override record" in reason or "push one reviewed branch at a time" in reason), (finding, command, reason)
    assert remote_ref(covered.origin, "other") is None and remote_ref(covered.origin, "pub") is None


def test_a_bare_push_under_matching_settings_is_denied_and_a_configured_wildcard_too(covered):
    git(covered.repo, "config", "push.default", "matching")
    check(covered.bash("git push"), "push.default=matching")
    git(covered.repo, "config", "--unset", "push.default")
    git(covered.repo, "config", "--add", "remote.origin.push", "refs/heads/*:refs/heads/*")
    check(covered.bash("git push origin"), "push one reviewed branch at a time")
    check(covered.bash("git push origin"), "the push refspec configured for this remote")
    check(covered.bash("git push origin 'refs/heads/*:refs/heads/*'"), "a refspec with a wildcard")


def test_the_honest_forms_are_not_held_up(covered):
    for command in (
        "git push origin feature",
        "git switch -c newb && git push -u origin newb",
        "git checkout -b newb && git push origin newb",
        "git tag v9 && git push origin v9",
        "git checkout feature && git push origin feature",
        "git diff --text",
        "git push --dry-run origin other",
        "git push origin :other",
    ):
        assert covered.bash(command) is None, command
    assert covered.bash("git diff --text", "verifier") is None
    assert covered.bash("git grep --text return", "prosecutor") is None


def test_the_override_forgery_by_copying_a_directory_is_denied(covered):
    forged = covered.tmp_path / "s" / "pass"
    forged.mkdir(parents=True)
    write(forged / "x.override.json", "{}")
    check(covered.bash(f"cp -r {forged} .plumbline/"), "written only by plumbline.py commands")
    check(covered.bash(f"rsync -a {forged} .plumbline/"), "written only by plumbline.py commands")


def test_the_builder_does_not_read_the_rest_of_what_a_test_run_leaves(covered):
    write(covered.repo / "coverage.xml", "<coverage/>")
    write(covered.repo / "test-results" / "a.xml", "<testsuite/>")
    write(covered.repo / "conftest.py", "import pytest\n")
    for name in ("coverage.xml", "test-results/a.xml", "conftest.py"):
        check(covered.run(covered.event("Read", {"file_path": covered.path(name)}, "builder")), "builder")


def test_a_general_agent_is_kept_from_the_runs_directory_in_any_spelling(world):
    for brief in ("read .PLUMBLINE/runs/r1/plan.json", "look in .plumbline\\runs"):
        check(world.run(world.event("Agent", {"description": "x", "prompt": brief, "subagent_type": "general-purpose"})), "plumbline stages run through the plumbline:* agents")
