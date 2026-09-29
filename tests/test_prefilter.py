"""The sh filters in front of the PreToolUse and SubagentStop hooks: Python starts only when the input could concern plumbline."""
import json
import os
import shutil
import stat
import subprocess

import pytest

from helpers import REPO, SCRIPTS, clean_env
from hookdata import bash_payload, denial, stop_payload, tool_payload
from rundata import adopt

PRE = SCRIPTS / "pre_tool_use.sh"
STOP = SCRIPTS / "subagent_stop.sh"


@pytest.fixture
def shim(tmp_path):
    """A `python3` that records that it was started and what it was given, and answers as scripted."""
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    log = tmp_path / "shim.log"
    script = bin_dir / "python3"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{log}"\n'
        f'cat > "{log}.stdin"\n'
        '[ -n "$SHIM_STDOUT" ] && printf "%s" "$SHIM_STDOUT"\n'
        '[ -n "$SHIM_STDERR" ] && printf "%s" "$SHIM_STDERR" >&2\n'
        'exit "${SHIM_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    class Shim:
        path = str(bin_dir)

        def started(self):
            return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

        def stdin(self):
            return (log.parent / "shim.log.stdin").read_text(encoding="utf-8")

        def reset(self):
            for name in (log, log.parent / "shim.log.stdin"):
                name.unlink(missing_ok=True)

    return Shim()


def run_filter(script, payload, shim, home, cwd=None, **env):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    environment = clean_env(home, **env)
    environment["PATH"] = f"{shim.path}:{environment['PATH']}"
    return subprocess.run(["sh", str(script)], input=text, capture_output=True, text=True, env=environment, cwd=cwd)


def reaches_python(script, payload, shim, home, **env):
    shim.reset()
    result = run_filter(script, payload, shim, home, **env)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    return bool(shim.started())


REPO_DIR = "/work/project"
SOMEONE = "/ho" + "me/someone"  # built from pieces: no file of this repository holds an absolute home path


def tool(name, tool_input, agent_type=None, cwd=REPO_DIR):
    return tool_payload(cwd, name, tool_input, agent_type=agent_type)


# --- PreToolUse: what passes through, what is skipped

PASSES = {
    "a plumbline agent's Read": tool("Read", {"file_path": f"{REPO_DIR}/tests/test_app.py"}, agent_type="plumbline:builder"),
    "a plumbline agent's Grep": tool("Grep", {"pattern": "x"}, agent_type="plumbline:builder"),
    "a plumbline agent's Bash": tool("Bash", {"command": "ls"}, agent_type="plumbline:verifier"),
    "a plumbline agent's Write": tool("Write", {"file_path": f"{REPO_DIR}/src/app.py", "content": "x"}, agent_type="plumbline:test-writer"),
    "a main-session git push": tool("Bash", {"command": "git push origin main"}),
    "a main-session git commit": tool("Bash", {"command": "git commit -m x"}),
    "gh pr create": tool("Bash", {"command": "gh pr create --fill"}),
    "gh with a repo option before pr create": tool("Bash", {"command": "gh --repo owner/name pr create --fill"}),
    "an Edit of a .plumbline path": tool("Edit", {"file_path": f"{REPO_DIR}/.plumbline/pass/x.json", "old_string": "a", "new_string": "b"}),
    "a Write of a ledger": tool("Write", {"file_path": f"{REPO_DIR}/.plumbline/runs/r1/ledger.jsonl", "content": "x"}),
    "a redirection into .plumbline": tool("Bash", {"command": "echo x > .plumbline/pass/a.json"}),
    "plumbline.py": tool("Bash", {"command": "python3 scripts/plumbline.py status"}),
    "plumbline.toml": tool("Read", {"file_path": f"{REPO_DIR}/plumbline.toml"}),
    "an override through a variable": tool("Bash", {"command": 'python3 "$(cat /tmp/where)" override --reason "twenty characters or more"'}),
    "git followed by a tab": tool("Bash", {"command": "git\tpush"}),
    "git followed by a newline": tool("Bash", {"command": "echo a\ngit\npush"}),
    "git followed by a backslash and a newline": tool("Bash", {"command": "git\\\npush"}),
    "a quoted git": tool("Bash", {"command": "\"git\" push"}),
    "a single-quoted git": tool("Bash", {"command": "'git' push"}),
    "git in a pipeline": tool("Bash", {"command": "cd x && git push"}),
    "a session in a directory named for plumbline": tool("Read", {"file_path": "/work/plumbline/src/app.py"}, cwd="/work/plumbline"),
}

SKIPS = {
    "a main-session Read under a github path": tool("Read", {"file_path": f"{SOMEONE}/github/x/app.py"}, cwd=f"{SOMEONE}/github/x"),
    "a plain ls": tool("Bash", {"command": "ls -la"}),
    "cat of a file": tool("Bash", {"command": "cat README.md"}),
    "a test run": tool("Bash", {"command": "python3 -m pytest -q"}),
    "a Glob": tool("Glob", {"pattern": "**/*.py", "path": "src"}),
    "a Grep": tool("Grep", {"pattern": "def ", "path": "src"}),
    "an Edit of a source file": tool("Edit", {"file_path": f"{REPO_DIR}/src/app.py", "old_string": "a", "new_string": "b"}),
    "a Write of a note": tool("Write", {"file_path": f"{REPO_DIR}/notes.md", "content": "hello"}),
    "a Read of .gitignore": tool("Read", {"file_path": f"{REPO_DIR}/.gitignore"}),
    "a github url": tool("Bash", {"command": "curl -s https://api.github.com/repos/x/y"}),
    "another plugin's agent": tool("Read", {"file_path": "/x/y.py"}, agent_type="probe:echo"),
    "the built-in Explore agent": tool("Grep", {"pattern": "x", "path": "src"}, agent_type="Explore"),
    "a word that merely holds the letters": tool("Bash", {"command": "echo digital gitless"}),
    "a WebFetch": tool("WebFetch", {"url": "https://example.com/docs"}),
}


@pytest.mark.parametrize("name", list(PASSES))
def test_the_filter_passes_what_could_concern_plumbline_through_to_python(shim, home, name):
    assert reaches_python(PRE, PASSES[name], shim, home), name


@pytest.mark.parametrize("name", list(SKIPS))
def test_the_filter_skips_what_has_nothing_to_do_with_plumbline_without_starting_python(shim, home, name):
    assert not reaches_python(PRE, SKIPS[name], shim, home), name


def test_a_plumbline_agents_read_a_main_session_git_push_gh_pr_create_and_an_edit_of_a_plumbline_path_pass_and_a_github_read_and_ls_do_not(shim, home):
    assert reaches_python(PRE, PASSES["a plumbline agent's Read"], shim, home)
    assert reaches_python(PRE, PASSES["a main-session git push"], shim, home)
    assert reaches_python(PRE, PASSES["gh pr create"], shim, home)
    assert reaches_python(PRE, PASSES["an Edit of a .plumbline path"], shim, home)
    assert not reaches_python(PRE, SKIPS["a main-session Read under a github path"], shim, home)
    assert not reaches_python(PRE, SKIPS["a plain ls"], shim, home)


def test_what_reaches_python_is_the_input_as_it_was(shim, home):
    payload = PASSES["a main-session git push"]
    shim.reset()
    run_filter(PRE, payload, shim, home)
    assert json.loads(shim.stdin()) == payload


def test_the_command_may_hold_quotes_newlines_backslashes_and_dollars_and_arrive_unchanged(shim, home):
    command = "git commit -m \"it's a 'test'\"\necho \"$HOME\" `date` \\n \\\\ $(id) ${X} \t end"
    payload = tool("Bash", {"command": command})
    shim.reset()
    result = run_filter(PRE, payload, shim, home)
    assert result.returncode == 0 and shim.started()
    assert json.loads(shim.stdin())["tool_input"]["command"] == command


def test_json_with_raw_newlines_between_the_fields_is_read_whole(shim, home):
    text = '{\n  "tool_name": "Bash",\n  "tool_input": {\n    "command": "git push"\n  }\n}\n'
    shim.reset()
    assert run_filter(PRE, text, shim, home).returncode == 0
    assert shim.started() and json.loads(shim.stdin())["tool_input"]["command"] == "git push"


def test_a_skipped_input_is_silent_and_exits_0_whatever_it_holds(shim, home):
    for text in ("", "\n", "not json at all", "{", '"unterminated', "[]", "null", "0", "'", '"', "\\", "$(rm -rf /)", "`x`", "*", "?", "[", "ls; echo done"):
        assert not reaches_python(PRE, text, shim, home), repr(text)


def test_empty_stdin_starts_nothing(shim, home):
    assert not reaches_python(PRE, "", shim, home)
    shim.reset()
    result = subprocess.run(["sh", str(PRE)], stdin=subprocess.DEVNULL, capture_output=True, text=True, env={**clean_env(home), "PATH": f"{shim.path}:/usr/bin:/bin"})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "") and not shim.started()


def test_a_large_input_is_filtered_and_passed_on_whole(shim, home):
    big = "x = 1\n" * 200_000  # over a megabyte in one field
    skipped = tool("Write", {"file_path": f"{REPO_DIR}/big.py", "content": big})
    assert not reaches_python(PRE, skipped, shim, home)
    passed = tool("Write", {"file_path": f"{REPO_DIR}/.plumbline/runs/r1/plan.json", "content": big})
    shim.reset()
    assert run_filter(PRE, passed, shim, home).returncode == 0
    assert json.loads(shim.stdin())["tool_input"]["content"] == big


def test_input_that_is_not_valid_utf8_is_neither_a_crash_nor_output(shim, home):
    shim.reset()
    environment = {**clean_env(home), "PATH": f"{shim.path}:/usr/bin:/bin"}
    result = subprocess.run(["sh", str(PRE)], input=b'{"command": "\xff\xfe ls"}', capture_output=True, env=environment)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"") and not shim.started()


def test_python_is_not_needed_to_skip(home, tmp_path):
    only_cat = tmp_path / "only-cat"
    only_cat.mkdir()
    os.symlink(shutil.which("cat"), only_cat / "cat")  # the filter needs cat and the shell's builtins, and nothing else
    result = subprocess.run(["/bin/sh", str(PRE)], input=json.dumps(SKIPS["a plain ls"]), capture_output=True, text=True, env={"PATH": str(only_cat), "HOME": str(home)})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("command", ["echo high value", "echo the digit 3", "echo through it", "ls /opt/highlight /opt/digit "])
def test_a_word_that_ends_in_gh_or_git_before_a_space_passes_through_harmlessly(shim, home, command):
    # a word boundary cannot be relied on in JSON text (a newline is written \\n, so `gh` after it is preceded by a letter),
    # so the filter errs towards Python; Python then allows the call, and only the saved milliseconds are lost
    assert reaches_python(PRE, tool("Bash", {"command": command}), shim, home)


def test_pythons_answer_and_exit_status_pass_through_unchanged(shim, home):
    result = run_filter(PRE, PASSES["a main-session git push"], shim, home, SHIM_STDOUT='{"decision":"x"}\n', SHIM_STDERR="why\n", SHIM_EXIT="2")
    assert (result.returncode, result.stdout, result.stderr) == (2, '{"decision":"x"}\n', "why\n")


def test_the_filter_is_found_from_any_way_of_invoking_it(shim, home, tmp_path):
    payload = PASSES["a main-session git push"]
    plugin = tmp_path / "plugin with space"
    (plugin / "scripts").mkdir(parents=True)
    shutil.copy(PRE, plugin / "scripts" / "pre_tool_use.sh")
    (plugin / "scripts" / "pre_tool_use.py").write_text("import sys\nsys.stdout.write('reached ' + __file__)\n", encoding="utf-8")
    environment = {**clean_env(home), "PATH": "/usr/bin:/bin"}
    for command, cwd in ((["sh", str(plugin / "scripts" / "pre_tool_use.sh")], tmp_path), (["sh", "pre_tool_use.sh"], plugin / "scripts"), (["sh", "scripts/pre_tool_use.sh"], plugin), (["sh", "./scripts/pre_tool_use.sh"], plugin)):
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True, env=environment, cwd=cwd)
        assert result.returncode == 0 and result.stdout.startswith("reached ") and result.stdout.endswith("pre_tool_use.py"), (command, result.stderr)


# --- SubagentStop

def stop(agent_type, message="done"):
    return stop_payload(REPO_DIR, agent_type=agent_type, message=message)


@pytest.mark.parametrize("role", ["planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective"])
def test_a_plumbline_agents_stop_reaches_python(shim, home, role):
    assert reaches_python(STOP, stop(f"plumbline:{role}"), shim, home)


@pytest.mark.parametrize("agent_type", ["probe:echo", "Explore", "general-purpose", "", "my-plumbline-helper"])
def test_any_other_agents_stop_is_skipped(shim, home, agent_type):
    assert not reaches_python(STOP, stop(agent_type), shim, home)


def test_a_stop_block_survives_the_filter_status_and_streams_included(shim, home):
    result = run_filter(STOP, stop("plumbline:planner"), shim, home, SHIM_STDOUT='{"decision":"block","reason":"r"}\n', SHIM_STDERR="r\n", SHIM_EXIT="2")
    assert (result.returncode, result.stdout, result.stderr) == (2, '{"decision":"block","reason":"r"}\n', "r\n")
    assert json.loads(shim.stdin())["agent_type"] == "plumbline:planner"


def test_the_stop_filter_is_silent_on_odd_input(shim, home):
    for text in ("", "not json", "{", "[]", "\\", "'", '"plumbline"', "plumbline"):
        assert not reaches_python(STOP, text, shim, home), repr(text)


# --- the registered commands, end to end with the real Python scripts


def hooks_json():
    return json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def registered(event, payload, plugin_root, cwd, home, tmp_path):
    command = hooks_json()[event][0]["hooks"][0]["command"]
    return subprocess.run(
        command, shell=True, cwd=cwd, capture_output=True, text=True, input=json.dumps(payload),
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(plugin_root), CLAUDE_PROJECT_DIR=str(cwd), GIT_CEILING_DIRECTORIES=str(tmp_path)),
    )


def plugin_copy(tmp_path):
    root = tmp_path / "a plugin"
    for name in ("scripts", "schemas", "pipeline"):
        shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_the_registered_pre_tool_use_command_denies_a_builders_read_of_a_test_and_a_push_and_is_silent_on_the_rest(repo, home, tmp_path):
    adopt(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "x.py").write_text("assert False\n", encoding="utf-8")
    root = plugin_copy(tmp_path)
    read = tool_payload(repo, "Read", {"file_path": str(repo / "tests" / "x.py")}, agent_type="plumbline:builder")
    result = registered("PreToolUse", read, root, repo, home, tmp_path)
    assert result.returncode == 0 and "the builder works blind to the tests" in json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    result = registered("PreToolUse", bash_payload(repo, "git push origin feature"), root, repo, home, tmp_path)
    assert "has no pass or override record" in json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    write = tool_payload(repo, "Write", {"file_path": str(repo / ".plumbline" / "pass" / "x.json"), "content": "x"})
    assert "is written only by plumbline.py commands" in json.loads(registered("PreToolUse", write, root, repo, home, tmp_path).stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    quiet = tool_payload(repo, "Read", {"file_path": str(repo / "README.md")})
    result = registered("PreToolUse", quiet, root, repo, home, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    plain = bash_payload(repo, "ls -la")
    assert registered("PreToolUse", plain, root, repo, home, tmp_path).stdout == ""


def test_the_registered_subagent_stop_command_blocks_through_the_filter_and_or_true(repo, home, tmp_path):
    adopt(repo)
    from rundata import put

    put(repo, "plan", {"goal": ""})
    root = plugin_copy(tmp_path)
    result = registered("SubagentStop", stop_payload(repo, message="RECORD: .plumbline/runs/r1/plan.json"), root, repo, home, tmp_path)
    assert result.returncode == 0  # `|| true`
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block" and "not a valid spec" in decision["reason"]
    other = registered("SubagentStop", stop_payload(repo, agent_type="probe:echo", message="no record"), root, repo, home, tmp_path)
    assert (other.returncode, other.stdout, other.stderr) == (0, "", "")


def test_the_scripts_are_plain_posix_sh():
    for script in (PRE, STOP):
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh\n")
        assert "bash" not in text and "[[" not in text and "$'" not in text and "<<<" not in text
        result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert os.access(script, os.X_OK)


def test_the_filter_words_match_the_hook_scripts_that_sit_behind_them():
    # what the Python hook cares about, and what the filter passes: every plumbline agent type, .plumbline paths, git and gh
    pre_text = PRE.read_text(encoding="utf-8")
    for needle in ("*plumbline*", "*override*", "*'git '*", "*'gh '*"):
        assert needle in pre_text
    assert "*'plumbline:'*" in STOP.read_text(encoding="utf-8")
