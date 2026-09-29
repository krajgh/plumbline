"""The PreToolUse hook: the push gate and the commit checks on Bash, and the builder's blindness on Read, Grep and Glob.
It denies with the JSON permissionDecision form, and only ever inside a repository that has adopted plumbline."""
import json
import os
from pathlib import Path

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import DEFAULT_TOML, commit_all, git, write
from hookdata import bash_payload, denial, tool_payload
from rundata import RUN, adopt, put, spec_record, written_tests_record
from samples import sample

# built from pieces, so that no file of this repository holds what the commit check forbids
HOME_PATH = "/ho" + "me/someone/project/file.txt"
MAC_PATH = "/Us" + "ers/someone/project/file.txt"
KEY = "sk-" + "ant-" + "a" * 24


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def bash(run_pre, repo, command, **fields):
    return denial(run_pre(bash_payload(repo, command, **fields), repo))


def cover_with_pass(repo, sha=None):
    record = sample("pass_record")
    record["commit"] = sha or head(repo)
    pl.write_json_atomic(repo / ".plumbline" / "pass" / f"{record['commit']}.json", record)


def cover_with_override(repo, sha=None):
    record = sample("override_record")
    record["commit"] = sha or head(repo)
    pl.write_json_atomic(repo / ".plumbline" / "pass" / f"{record['commit']}.override.json", record)


# --- the push gate


@pytest.mark.parametrize("command", ["git push", "git push origin feature", "git push -u origin feature", "git push --force-with-lease origin feature"])
def test_a_push_is_denied_without_a_pass_or_override_record_for_head(run_pre, adopted, command):
    reason = bash(run_pre, adopted, command)
    assert reason and f"HEAD {head(adopted)[:7]} has no pass or override record" in reason
    assert "/plumbline:run" in reason and "/plumbline:override" in reason


def test_a_pr_is_denied_the_same_way(run_pre, adopted):
    reason = bash(run_pre, adopted, "gh pr create --fill")
    assert reason and "has no pass or override record" in reason and "opened for review" in reason


def test_a_push_is_allowed_with_a_pass_record_for_head(run_pre, adopted):
    cover_with_pass(adopted)
    assert bash(run_pre, adopted, "git push origin feature") is None
    assert bash(run_pre, adopted, "gh pr create --fill") is None


def test_a_push_is_allowed_with_an_override_record_for_head(run_pre, adopted):
    cover_with_override(adopted)
    assert bash(run_pre, adopted, "git push origin feature") is None


def test_a_record_for_another_commit_does_not_allow_a_push(run_pre, adopted):
    cover_with_pass(adopted, "0" * 40)
    assert bash(run_pre, adopted, "git push")
    cover_with_pass(adopted)
    write(adopted / "README.md", "next\n")
    commit_all(adopted, "next")  # HEAD moved past the commit the pass covers
    assert bash(run_pre, adopted, "git push")


def test_a_pass_record_that_says_fail_or_is_invalid_does_not_allow_a_push(run_pre, adopted):
    record = sample("pass_record")
    record.update(commit=head(adopted), verdict="fail")
    pl.write_json_atomic(adopted / ".plumbline" / "pass" / f"{head(adopted)}.json", record)
    assert bash(run_pre, adopted, "git push")
    write(adopted / ".plumbline" / "pass" / f"{head(adopted)}.json", "{not json")
    assert bash(run_pre, adopted, "git push")


def test_the_pass_command_is_what_opens_the_gate(run_cli, run_pre, adopted):
    from rundata import write_docs_run

    write_docs_run(adopted)
    assert bash(run_pre, adopted, "git push")
    assert run_cli("pass", RUN, cwd=adopted).returncode == 0
    assert bash(run_pre, adopted, "git push") is None


def test_a_push_in_a_repository_that_has_not_adopted_plumbline_is_not_touched(run_pre, repo):
    assert bash(run_pre, repo, "git push origin feature") is None
    assert bash(run_pre, repo, "gh pr create") is None


@pytest.mark.parametrize(
    "command",
    [
        "git push --dry-run",
        "git push -n origin main",
        "git push --dry-run origin main",
        "git push -h",
        "git push --help",
        "echo git push",
        "echo 'git push origin main'",
        'echo "please run git push later"',
        "printf '%s' 'gh pr create'",
        "grep -rn 'git push' docs/",
        'git commit -m "git push comes next"',
        "cat README.md",
        "git status",
        "git log --oneline -3",
        "git pull --rebase",
        "gh pr list",
        "gh pr view 3",
        "gh pr create --dry-run",
        "gh pr create --help",
        "git pushd",
        "gitpush",
        "ls > git-push.txt",
        "# git push",
        "cat <<'EOF' > notes.md\ngit push origin main\nEOF",
        "git commit -m \"$(cat <<'EOF'\nDocument git push and gh pr create.\n\nCo-Authored-By: X <x@example.com>\nEOF\n)\"",
        "git",
    ],
)
def test_what_is_not_a_push_is_not_denied(run_pre, adopted, command):
    assert bash(run_pre, adopted, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "cd . && git push",
        "true; git push",
        "false || git push",
        "git status | cat; git push",
        "git push 2>&1 | tail -5",
        "git push origin main &",
        "(git push)",
        "FOO=bar git push",
        "env GIT_TRACE=1 git push",
        "sudo -u me git push",
        "timeout 60 git push",
        "nohup git push &",
        "command git push",
        "/usr/bin/git push",
        "git -C . push",
        "git -c push.default=current push",
        "bash -c 'git push origin main'",
        'sh -c "git push"',
        "eval 'git push'",
        "echo $(git push)",
        'echo "$(git push)"',
        "echo `git push`",
        "git push\ngit status",
        "git \\\n push",
        "gh pr create",
        "gh --repo owner/name pr create --fill",
        "gh pr create --title 'x' --body 'y'",
        "if true; then git push; fi",
        "for b in a b; do git push origin $b; done",
        "while false; do :; done; ! git push",
        "{ git push; }",
        "gh pr create --body \"$(cat <<'EOF'\nWhat changed, and why.\nEOF\n)\"",
    ],
)
def test_pushes_are_recognised_however_the_command_is_written(run_pre, adopted, command):
    assert bash(run_pre, adopted, command), command


def test_the_power_shell_tool_is_gated_too(run_pre, adopted):
    payload = tool_payload(adopted, "PowerShell", {"command": "git push origin main"})
    assert denial(run_pre(payload, adopted))


def test_a_push_is_gated_in_the_directory_it_runs_in(run_pre, adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    payload = bash_payload(outside, f"git -C {adopted} push")
    assert denial(run_pre(payload, outside, GIT_CEILING_DIRECTORIES=str(tmp_path)))
    payload = bash_payload(outside, f"cd {adopted} && git push")
    assert denial(run_pre(payload, outside, GIT_CEILING_DIRECTORIES=str(tmp_path)))
    plain = bash_payload(outside, "git push")
    assert denial(run_pre(plain, outside, GIT_CEILING_DIRECTORIES=str(tmp_path))) is None  # not a repository at all


def test_a_push_from_a_subdirectory_is_gated_by_its_repository(run_pre, adopted):
    assert denial(run_pre(bash_payload(adopted / "src", "git push"), adopted / "src"))
    assert denial(run_pre(bash_payload(adopted, "cd src && git push"), adopted))


def test_a_push_that_follows_a_commit_in_the_same_command_is_denied_whatever_the_old_head_has(run_pre, adopted):
    cover_with_pass(adopted)  # the current HEAD is covered, but the commit made in this command will not be
    reason = bash(run_pre, adopted, "git commit -m more && git push")
    assert reason and "changes HEAD" in reason and "push in a separate command" in reason
    assert bash(run_pre, adopted, "git merge other && git push")
    assert bash(run_pre, adopted, "git checkout main; git push")
    assert bash(run_pre, adopted, "git push && git commit -m after") is None  # the push comes first


# --- the commit checks


def stage(repo, name, text):
    write(repo / name, text)
    git(repo, "add", name)


def commit(run_pre, repo, command="git commit -m change"):
    return bash(run_pre, repo, command)


def test_a_staged_symlink_is_denied(run_pre, adopted):
    os.symlink("README.md", adopted / "link")
    git(adopted, "add", "link")
    reason = commit(run_pre, adopted)
    assert reason and "adds a symlink: link" in reason and "held back" in reason


def test_a_home_path_in_an_added_line_is_denied(run_pre, adopted):
    stage(adopted, "notes.md", f"see\nthe file {HOME_PATH}\n")
    reason = commit(run_pre, adopted)
    assert reason and "adds an absolute home path: notes.md:2" in reason


def test_a_macos_home_path_is_denied_too(run_pre, adopted):
    stage(adopted, "notes.md", f"{MAC_PATH}\n")
    assert "adds an absolute home path: notes.md:1" in commit(run_pre, adopted)


def test_a_key_shaped_secret_is_denied_and_never_echoed(run_pre, adopted):
    stage(adopted, "config.txt", f"x = 1\napi_key = {KEY}\n")
    reason = commit(run_pre, adopted)
    assert reason and "adds a key-shaped secret: config.txt:2" in reason
    assert KEY not in reason and "sk-ant" not in reason


def test_sk_ant_needs_twenty_more_characters(run_pre, adopted):
    stage(adopted, "config.txt", "sk-" + "ant-" + "a" * 19 + "\n")
    assert commit(run_pre, adopted) is None
    stage(adopted, "config.txt", "sk-" + "ant-" + "a" * 20 + "\n")
    assert commit(run_pre, adopted)
    stage(adopted, "config.txt", "sk-" + "ant-" + "api03-Ab_9-" + "x" * 12 + "\n")  # underscores and hyphens belong to keys
    assert commit(run_pre, adopted)


def test_a_clean_commit_passes(run_pre, adopted):
    stage(adopted, "src/new.py", "def f():\n    return 1  # a comment about /home directories in general\n")
    stage(adopted, "docs/guide.md", "Use the path `<home>/project/` and https://example.com/ho" + "me/user/ here.\n")
    assert commit(run_pre, adopted) is None
    assert commit(run_pre, adopted, "git commit -m 'a message with /ho" + "me/someone/ in it'") is None  # the message is not a file


def test_every_problem_is_listed_once_per_place(run_pre, adopted):
    os.symlink("README.md", adopted / "link")
    git(adopted, "add", "link")
    stage(adopted, "a.txt", f"{HOME_PATH}\n{KEY}\n")
    reason = commit(run_pre, adopted)
    assert "adds a symlink: link" in reason
    assert "adds an absolute home path: a.txt:1" in reason
    assert "adds a key-shaped secret: a.txt:2" in reason


def test_only_added_lines_count_not_the_ones_a_commit_removes_or_leaves(run_pre, adopted):
    stage(adopted, "old.txt", f"{HOME_PATH}\nkeep\n")
    git(adopted, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "old")  # committed without the hook, as before adoption
    write(adopted / "old.txt", "keep\nnew line\n")  # removes the path
    git(adopted, "add", "old.txt")
    assert commit(run_pre, adopted) is None


def test_a_removed_symlink_is_not_an_added_one(run_pre, adopted):
    os.symlink("README.md", adopted / "link")
    git(adopted, "add", "link")
    git(adopted, "commit", "-q", "-m", "link")
    git(adopted, "rm", "-q", "link")
    assert commit(run_pre, adopted) is None


def test_what_is_only_modified_in_the_working_tree_is_not_committed_by_a_plain_commit(run_pre, adopted):
    write(adopted / "README.md", f"# demo\n{HOME_PATH}\n")  # not staged
    assert commit(run_pre, adopted) is None
    assert commit(run_pre, adopted, "git commit -a -m change")
    assert commit(run_pre, adopted, "git commit -am change")
    assert commit(run_pre, adopted, "git commit -m change README.md")  # a pathspec commits the working-tree version
    assert commit(run_pre, adopted, "git commit --all --message=change")


def test_a_git_add_earlier_in_the_same_command_brings_the_working_tree_and_untracked_files_into_scope(run_pre, adopted):
    write(adopted / "fresh.txt", f"token {KEY}\n")  # untracked
    assert commit(run_pre, adopted) is None
    reason = commit(run_pre, adopted, "git add -A && git commit -m change")
    assert reason and "adds a key-shaped secret: fresh.txt:1" in reason
    os.symlink("README.md", adopted / "alias")
    assert "adds a symlink: alias" in commit(run_pre, adopted, "git add . && git commit -m change")


def test_untracked_files_under_plumbline_are_not_scanned(run_pre, repo):
    # a repository that does not ignore .plumbline/: its run files are untracked, and hold home paths (the ledger names transcripts)
    write(repo / "plumbline.toml", "schema = 1\n")
    commit_all(repo, "adopt without ignoring .plumbline/")
    write(repo / ".plumbline" / "runs" / "r1" / "ledger.jsonl", f'{{"transcript": "{HOME_PATH}"}}\n')
    assert commit(run_pre, repo, "git add -A && git commit -m change") is None
    write(repo / "notes.md", f"{HOME_PATH}\n")  # the same thing outside .plumbline/ is caught
    assert "adds an absolute home path: notes.md:1" in commit(run_pre, repo, "git add -A && git commit -m change")


def test_the_first_commit_of_a_repository_is_checked_too(run_pre, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "main")
    write(fresh / "plumbline.toml", "schema = 1\n")
    write(fresh / "a.txt", f"{KEY}\n")
    git(fresh, "add", "-A")
    assert "adds a key-shaped secret: a.txt:1" in commit(run_pre, fresh)
    assert "adds a key-shaped secret" in commit(run_pre, fresh, "git commit -am first")


def test_commits_are_not_checked_where_plumbline_is_not_adopted(run_pre, repo):
    stage(repo, "notes.md", f"{HOME_PATH}\n{KEY}\n")
    os.symlink("README.md", repo / "link")
    git(repo, "add", "link")
    assert commit(run_pre, repo) is None


def test_a_commit_that_is_not_a_commit_is_not_checked(run_pre, adopted):
    stage(adopted, "notes.md", f"{HOME_PATH}\n")
    for command in ("git status", "git diff --cached", "echo git commit", "git log", "git show HEAD", "git stash"):
        assert bash(run_pre, adopted, command) is None, command


# --- the builder's blindness


BUILDER = "plumbline:builder"


def read_of(repo, name, agent_type=BUILDER, cwd=None):
    payload = tool_payload(cwd or repo, "Read", {"file_path": str(repo / name)}, agent_type=agent_type)
    return payload


def looks(run_pre, repo, tool, tool_input, agent_type=BUILDER, cwd=None):
    payload = tool_payload(cwd or repo, tool, tool_input, agent_type=agent_type)
    return denial(run_pre(payload, cwd or repo))


def test_a_read_of_a_test_file_is_denied_for_the_builder_and_allowed_for_other_agents(run_pre, adopted):
    write(adopted / "tests" / "x.py", "def test_x():\n    assert False\n")
    reason = denial(run_pre(read_of(adopted, "tests/x.py"), adopted))
    assert reason and "the builder works blind to the tests, and tests/x.py is one" in reason
    for other in ("plumbline:planner", "plumbline:test-writer", "plumbline:verifier", "plumbline:prosecutor", "probe:echo", "Explore"):
        assert denial(run_pre(read_of(adopted, "tests/x.py", agent_type=other), adopted)) is None, other
    main_thread = tool_payload(adopted, "Read", {"file_path": str(adopted / "tests" / "x.py")})  # no agent_id, no agent_type
    assert denial(run_pre(main_thread, adopted)) is None


@pytest.mark.parametrize(
    "name",
    ["tests/x.py", "tests/deep/er/data.json", "test/x.py", "src/test_app.py", "pkg/app_test.py", "web/app.test.ts", "web/app.spec.js", "tests/fixtures/expected.txt"],
)
def test_the_builder_cannot_read_what_the_tests_type_matches(run_pre, adopted, name):
    assert denial(run_pre(read_of(adopted, name), adopted)), name


@pytest.mark.parametrize("name", ["src/app.py", "README.md", "docs/testing-guide.md", "src/contest.py", "latest.py", "pkg/testdata_loader.py"])
def test_the_builder_may_read_everything_else(run_pre, adopted, name):
    assert denial(run_pre(read_of(adopted, name), adopted)) is None, name


def test_a_file_the_newest_runs_tests_record_lists_is_denied_even_if_it_is_not_matched_by_the_type(run_pre, adopted):
    write(adopted / "tools" / "helper.py", "x = 1\n")
    assert denial(run_pre(read_of(adopted, "tools/helper.py"), adopted)) is None
    record = written_tests_record()
    record["files_written"] = ["tests/test_app.py", "tools/helper.py"]
    put(adopted, "tests", record)
    reason = denial(run_pre(read_of(adopted, "tools/helper.py"), adopted))
    assert reason and "tools/helper.py is one" in reason
    assert denial(run_pre(read_of(adopted, "src/app.py"), adopted)) is None


def test_the_tests_record_itself_is_off_limits_but_the_plan_is_not(run_pre, adopted):
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record())
    assert denial(run_pre(read_of(adopted, ".plumbline/runs/r1/plan.json"), adopted)) is None  # the builder builds from the plan
    reason = denial(run_pre(read_of(adopted, ".plumbline/runs/r1/tests.json"), adopted))
    assert reason and ".plumbline/runs/r1/tests.json is one" in reason
    assert looks(run_pre, adopted, "Grep", {"pattern": "scenario", "path": ".plumbline/runs/r1"})  # a search there would show it
    assert looks(run_pre, adopted, "Grep", {"pattern": "goal", "path": ".plumbline/runs/r1/plan.json"}) is None


def test_a_test_listed_by_file_name_in_the_tests_array_counts_too(run_pre, adopted):
    write(adopted / "check" / "cases.py", "x = 1\n")
    record = written_tests_record()
    record["tests"][0]["file"] = "check/cases.py"
    put(adopted, "tests", record)
    assert denial(run_pre(read_of(adopted, "check/cases.py"), adopted))


def test_only_the_newest_runs_tests_record_is_used(run_pre, adopted):
    write(adopted / "tools" / "helper.py", "x = 1\n")
    old = written_tests_record()
    old["files_written"] = ["tools/helper.py"]
    put(adopted, "tests", old, "old-run")
    old_dir = adopted / ".plumbline" / "runs" / "old-run"
    for path in [old_dir, *old_dir.iterdir()]:
        os.utime(path, (1_000_000_000, 1_000_000_000))
    put(adopted, "tests", written_tests_record(), "new-run")
    assert denial(run_pre(read_of(adopted, "tools/helper.py"), adopted)) is None


def test_a_symlink_into_the_tests_is_followed(run_pre, adopted):
    write(adopted / "tests" / "x.py", "assert False\n")
    os.symlink("../tests/x.py", adopted / "src" / "alias.py")
    assert denial(run_pre(read_of(adopted, "src/alias.py"), adopted))


def test_a_grep_or_glob_without_an_explicit_path_is_denied_for_the_builder(run_pre, adopted):
    for tool, tool_input in (("Grep", {"pattern": "def f"}), ("Glob", {"pattern": "**/*.py"}), ("Grep", {"pattern": "x", "path": ""}), ("Grep", {"pattern": "x", "path": None}), ("Glob", {"pattern": "*.py", "path": "  "})):
        reason = looks(run_pre, adopted, tool, tool_input)
        assert reason and f"{tool} needs an explicit path that is not a test path" in reason, (tool, tool_input)
        assert looks(run_pre, adopted, tool, tool_input, agent_type="plumbline:planner") is None
        assert denial(run_pre(tool_payload(adopted, tool, tool_input), adopted)) is None  # the main thread


def test_a_grep_or_glob_of_a_directory_without_tests_is_allowed(run_pre, adopted):
    assert looks(run_pre, adopted, "Grep", {"pattern": "def f", "path": "src"}) is None
    assert looks(run_pre, adopted, "Grep", {"pattern": "def f", "path": str(adopted / "src")}) is None
    assert looks(run_pre, adopted, "Glob", {"pattern": "**/*.py", "path": "src"}) is None
    assert looks(run_pre, adopted, "Grep", {"pattern": "x", "path": "src/app.py"}) is None


def test_a_grep_or_glob_that_leads_to_tests_is_denied(run_pre, adopted):
    write(adopted / "tests" / "x.py", "assert False\n")
    write(adopted / "src" / "app.test.ts", "it('x')\n")
    for tool, tool_input in (
        ("Grep", {"pattern": "x", "path": "tests"}),
        ("Grep", {"pattern": "x", "path": "tests/x.py"}),
        ("Grep", {"pattern": "x", "path": str(adopted / "tests")}),
        ("Grep", {"pattern": "x", "path": "."}),  # the whole repository holds tests
        ("Grep", {"pattern": "x", "path": str(adopted)}),
        ("Grep", {"pattern": "x", "path": "src"}),  # src holds a .test.ts file
        ("Glob", {"pattern": "**/*.py", "path": "tests"}),
        ("Glob", {"pattern": "tests/**", "path": "."}),
        ("Glob", {"pattern": "../tests/*.py", "path": "src"}),
        ("Glob", {"pattern": str(adopted / "tests" / "*.py"), "path": "src"}),
        ("Glob", {"pattern": "*.ts", "path": "src"}),
    ):
        assert looks(run_pre, adopted, tool, tool_input), (tool, tool_input)


def test_a_grep_path_is_resolved_against_the_cwd_of_the_hook_input(run_pre, adopted):
    write(adopted / "tests" / "x.py", "assert False\n")
    assert looks(run_pre, adopted, "Grep", {"pattern": "x", "path": "../tests"}, cwd=adopted / "src")
    assert looks(run_pre, adopted, "Grep", {"pattern": "x", "path": "app.py"}, cwd=adopted / "src") is None
    assert looks(run_pre, adopted, "Grep", {"pattern": "x", "path": "x.py"}, cwd=adopted / "tests")


def test_a_path_that_does_not_exist_yet_under_a_tests_directory_is_denied(run_pre, adopted):
    assert looks(run_pre, adopted, "Grep", {"pattern": "x", "path": "tests/new_dir"})
    assert denial(run_pre(read_of(adopted, "tests/not_yet.py"), adopted))


def test_paths_outside_the_repository_are_none_of_its_business(run_pre, adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    write(outside / "test_x.py", "x = 1\n")
    assert denial(run_pre(tool_payload(adopted, "Read", {"file_path": str(outside / "test_x.py")}, agent_type=BUILDER), adopted)) is None
    assert looks(run_pre, adopted, "Grep", {"pattern": "x", "path": str(outside)}) is None


def test_the_builder_is_not_blind_where_plumbline_is_not_adopted(run_pre, repo):
    write(repo / "tests" / "x.py", "assert False\n")
    assert denial(run_pre(read_of(repo, "tests/x.py"), repo)) is None
    assert looks(run_pre, repo, "Grep", {"pattern": "x"}) is None


def test_the_tests_type_comes_from_the_repositorys_pipeline(run_pre, adopted):
    custom = DEFAULT_TOML.read_text().replace('"tests/**", "test/**",', '"spec/**",')
    write(adopted / "pipelines" / "house.toml", custom)
    write(adopted / "plumbline.toml", 'schema = 1\npipeline = "pipelines/house.toml"\n')
    write(adopted / "spec" / "a_spec.rb", "x\n")
    write(adopted / "tests" / "x.py", "x\n")
    assert denial(run_pre(read_of(adopted, "spec/a_spec.rb"), adopted))
    assert denial(run_pre(read_of(adopted, "tests/x.py"), adopted)) is None


def test_a_broken_plumbline_toml_still_blinds_the_builder_with_the_default_tests_type(run_pre, adopted):
    write(adopted / "plumbline.toml", "schema = = 1\n")
    write(adopted / "tests" / "x.py", "x\n")
    assert denial(run_pre(read_of(adopted, "tests/x.py"), adopted))


def test_other_tools_are_left_alone(run_pre, adopted):
    for tool, tool_input in (("Edit", {"file_path": str(adopted / "tests" / "x.py"), "old_string": "a", "new_string": "b"}), ("Write", {"file_path": str(adopted / "tests" / "x.py"), "content": "x"}), ("WebFetch", {"url": "https://example.com"})):
        assert denial(run_pre(tool_payload(adopted, tool, tool_input, agent_type=BUILDER), adopted)) is None


# --- it never crashes and never holds anything up


@pytest.mark.parametrize("garbage", ["", "not json", "[]", "null", "5", '"text"', "{", '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": {"command": 5}, "cwd": "/"}', '{"tool_name": "Bash", "tool_input": null}', '{"tool_name": "Read", "agent_type": "plumbline:builder", "tool_input": "x"}'])
def test_garbage_on_stdin_allows_the_call(run_pre, adopted, garbage):
    result = run_pre(garbage, adopted)
    assert result.returncode == 0 and result.stdout == ""


def test_a_cwd_that_does_not_exist_allows_the_call(run_pre, adopted, tmp_path):
    payload = bash_payload(adopted, "git push") | {"cwd": str(tmp_path / "gone")}
    assert denial(run_pre(payload, adopted)) is None


def test_the_hook_writes_nothing_to_stderr_when_it_denies(run_pre, adopted):
    result = run_pre(bash_payload(adopted, "git push"), adopted)
    assert result.stderr == "" and result.returncode == 0


# --- parsing a command line, in process


@pytest.mark.parametrize(
    "text,expected",
    [
        ("git push", [["git", "push"]]),
        ("echo git push", [["echo", "git", "push"]]),
        ('git commit -m "a && b; c | d"', [["git", "commit", "-m", "a && b; c | d"]]),
        ("git commit -m 'it'\"'\"'s'", [["git", "commit", "-m", "it's"]]),
        ("a && b || c; d | e & f", [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]),
        ("git push 2>&1 | tail -5", [["git", "push"], ["tail", "-5"]]),
        ("git push > out.txt 2> err.txt", [["git", "push"]]),
        ("cat < in.txt", [["cat"]]),
        ("git push # and then some", [["git", "push"]]),
        ("echo a#b", [["echo", "a#b"]]),
        ("git \\\n push", [["git", "push"]]),
        ("git\npush", [["git"], ["push"]]),
        ("cat <<EOF\ngit push\nEOF\nls", [["cat"], ["ls"]]),
        ("cat <<-'EOF'\n\tgit push\n\tEOF\nls", [["cat"], ["ls"]]),
        ("cat <<< 'git push'", [["cat"]]),
        ("echo $(git push) done", [["git", "push"], ["echo", "$(...)", "done"]]),
        ("echo `git push`", [["git", "push"], ["echo", "`...`"]]),
        ('echo "$(git push)"', [["git", "push"], ["echo", "$(...)"]]),
        ('git commit -m ""', [["git", "commit", "-m", ""]]),
        ("(cd x && git push)", [["cd", "x"], ["git", "push"]]),
        ("", []),
        ("   ", []),
    ],
)
def test_split_commands(text, expected):
    assert pre.split_commands(text) == expected


def test_split_commands_survives_unbalanced_input():
    for text in ("echo 'unterminated", 'echo "unterminated', "echo $(unterminated", "echo `unterminated", "cat <<EOF\nnever ends", "git push \\"):
        pre.split_commands(text)


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["git", "push"], ["git", "push"]),
        (["FOO=bar", "BAZ=1", "git", "push"], ["git", "push"]),
        (["sudo", "-u", "me", "git", "push"], ["git", "push"]),
        (["env", "-u", "X", "A=1", "git", "push"], ["git", "push"]),
        (["nice", "-n", "5", "git", "push"], ["git", "push"]),
        (["timeout", "-k", "5", "60", "git", "push"], ["git", "push"]),
        (["command", "git", "push"], ["git", "push"]),
        (["time", "nohup", "git", "push"], ["git", "push"]),
        (["then", "git", "push"], ["git", "push"]),
        (["do", "FOO=1", "sudo", "git", "push"], ["git", "push"]),
        (["!", "git", "push"], ["git", "push"]),
        (["echo", "git", "push"], ["echo", "git", "push"]),
        ([], []),
        (["sudo"], []),
    ],
)
def test_strip_wrappers(argv, expected):
    assert pre.strip_wrappers(argv) == expected


def actions(command, cwd="/work/repo"):
    return [(a.kind, str(a.cwd)) for a in pre.analyze(command, Path(cwd))]


def test_analyze_follows_cd_and_git_dash_c():
    assert actions("git push") == [("push", "/work/repo")]
    assert actions("cd sub && git push") == [("push", "/work/repo/sub")]
    assert actions("cd /elsewhere && git push") == [("push", "/elsewhere")]
    assert actions("git -C ../other push") == [("push", "/work/other")]
    assert actions("git -C a -C b push") == [("push", "/work/repo/a/b")]
    assert actions("cd $X && git push") == [("push", "/work/repo")]  # a computed directory cannot be followed
    assert actions("git add . && git commit -m x") == [("add", "/work/repo"), ("commit", "/work/repo")]
    assert actions("git merge x") == [("head-moves", "/work/repo")]
    assert actions("git status") == [] and actions("git push --dry-run") == [] and actions("gh pr create") == [("pr-create", "/work/repo")]


def test_analyze_looks_inside_shells_eval_and_substitutions_but_not_beyond_a_few_levels():
    assert actions("bash -c 'git push'") == [("push", "/work/repo")]
    assert actions("bash -lc 'cd x && git push'") == [("push", "/work/repo/x")]
    assert actions("eval git push") == [("push", "/work/repo")]
    assert actions("echo $(git push)") == [("push", "/work/repo")]
    deep = "git push"
    for _ in range(5):
        deep = "bash -c " + json.dumps(deep)
    assert actions(deep) == []  # nested deeper than the parser follows


@pytest.mark.parametrize(
    "args,added,expected",
    [
        (["-m", "msg"], False, "index"),
        (["-m", "msg", "--amend"], False, "index"),
        (["-a", "-m", "msg"], False, "tracked"),
        (["-am", "msg"], False, "tracked"),
        (["-ma", "msg"], False, "tracked"),  # the message is "a" and msg is a pathspec: no -a, but a pathspec
        (["-mall", "-s"], False, "index"),  # the message is "all"
        (["--all", "-m", "msg"], False, "tracked"),
        (["-m", "msg", "file.txt"], False, "tracked"),
        (["-m", "msg", "--", "file.txt"], False, "tracked"),
        (["--only", "-m", "msg"], False, "tracked"),
        (["-o", "-m", "msg"], False, "tracked"),
        (["--include", "-m", "msg"], False, "tracked"),
        (["--message=msg", "--author=A <a@b.c>"], False, "index"),
        (["--message", "msg", "--author", "A <a@b.c>", "--date", "now"], False, "index"),
        (["-F", "file", "-s", "-S"], False, "index"),
        (["-C", "HEAD~1"], False, "index"),
        (["-m", "msg"], True, "all"),
    ],
)
def test_commit_scope(args, added, expected):
    assert pre.commit_scope(args, added) == expected


@pytest.mark.parametrize(
    "pattern,prefix",
    [("tests/**/*.py", "tests"), ("**/*.py", ""), ("src/*.js", "src"), ("src/app.py", "src"), ("/abs/dir/*.py", "/abs/dir"), ("/x.py", "/"), ("*.py", ""), ("a/b/c/*", "a/b/c"), ("", "")],
)
def test_the_static_prefix_of_a_glob_pattern(pattern, prefix):
    assert pre._static_prefix(pattern) == prefix


def test_added_lines_of_a_unified_diff():
    patch = "\n".join(
        [
            "diff --git a/a.txt b/a.txt", "--- a/a.txt", "+++ b/a.txt", "@@ -1,0 +1,2 @@", "+one", "+two",
            "@@ -9 +11 @@", "-old", "+new",
            "diff --git a/dir/b b.txt b/dir/b b.txt", "--- a/dir/b b.txt", "+++ b/dir/b b.txt\t", "@@ -0,0 +1 @@", "+third",
        ]
    )
    assert list(pre.added_lines(patch)) == [("a.txt", 1, "one"), ("a.txt", 2, "two"), ("a.txt", 11, "new"), ("dir/b b.txt", 1, "third")]


def test_the_home_path_and_secret_patterns():
    for text in (HOME_PATH, MAC_PATH, "x = '" + HOME_PATH + "'", "cd " + HOME_PATH):
        assert pre.HOME_PATH.search(text), text
    for text in ("https://example.com/ho" + "me/user/x", "/ho" + "me/", "/ho" + "me", "<home>/x/", "/ho" + "me/<user>/x", "~/x/", "a/Us" + "ers/b/c"):
        assert not pre.HOME_PATH.search(text), text
    assert pre.SECRET.search(KEY) and not pre.SECRET.search("sk-" + "ant-short") and not pre.SECRET.search("task-" + "a" * 30)
