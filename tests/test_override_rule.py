"""Override is the builder's, by mechanism: a hook denies `plumbline.py override` to the model, and the
/plumbline:override skill (disable-model-invocation, shell injection) is how the builder runs it."""
import json
import re
import subprocess
from pathlib import Path

import pytest

import plumbline as pl
from helpers import CLI, REPO, clean_env, commit_all, git, run_script, write
from hookdata import bash_payload, denial
from rundata import adopt
from test_manifests import frontmatter

ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective")
REASON = "The pipeline cannot run offline; a one-line typo fix, reviewed by hand."
DENIAL = (
    "plumbline: `plumbline.py override` is the builder's command, and the builder types it: /plumbline:override followed by the reason. "
    "An agent, or the main session, does not run it. To skip the pipeline for this commit, ask the builder to type /plumbline:override."
)


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def runs(run_pre, repo, command, role=None, cwd=None):
    payload = bash_payload(cwd or repo, command, agent_type=f"plumbline:{role}" if role else None)
    return denial(run_pre(payload, cwd or repo))


# --- the hook


def test_a_bash_override_is_denied_to_the_main_session(run_pre, adopted):
    assert runs(run_pre, adopted, f'python3 {CLI} override --reason "{REASON}"') == DENIAL


@pytest.mark.parametrize("role", ROLES)
def test_a_bash_override_is_denied_to_every_agent(run_pre, adopted, role):
    reason = runs(run_pre, adopted, f'python3 "{CLI}" override --reason "{REASON}"', role)
    assert reason  # the builder has no Bash at all, so it is refused first; the others by the override rule or the allow-list
    if role != "builder":
        assert reason == DENIAL or "is none of these" in reason


def test_the_override_rule_speaks_before_the_allow_list_so_the_agent_is_told_who_types_it(run_pre, adopted):
    for role in ("verifier", "planner", "defender"):
        assert runs(run_pre, adopted, f'python3 {CLI} override --reason "{REASON}"', role) == DENIAL


@pytest.mark.parametrize(
    "command",
    [
        'python3 {cli} override --reason "{reason}"',
        "python3 {cli} override --reason={reason_word}",
        "python3 -u {cli} override --reason x",
        "python3.12 {cli} override --reason x",
        "python {cli} override --reason x",
        "python3 {cli} override --run r1 --reason x",
        "cd scripts && python3 plumbline.py override --reason x",
        "./scripts/plumbline.py override --reason x",
        "uv run scripts/plumbline.py override --reason x",
        "env FOO=1 python3 {cli} override --reason x",
        "timeout 20 python3 {cli} override --reason x",
        "sudo python3 {cli} override --reason x",
        "nohup python3 {cli} override --reason x &",
        "true && python3 {cli} override --reason x",
        "git status; python3 {cli} override --reason x",
        "bash -c 'python3 {cli} override --reason x'",
        'sh -c "python3 {cli} override --reason x"',
        "eval python3 {cli} override --reason x",
        "echo $(python3 {cli} override --reason x)",
        "S={cli}; python3 $S override --reason x",
        'python3 "$(cat /tmp/where)" override --reason x',
        'python3 "$SCRIPT" override --reason "{reason}"',
        "python3 -c \"import plumbline; plumbline.main(['override', '--reason', 'x'])\"",
        "node -e \"require('child_process').execSync('plumbline.py override --reason x')\"",
        "python3 {cli} override --reason - <<EOF\n{reason}\nEOF",
        "cat reason.txt | python3 {cli} override --reason -",
    ],
)
def test_an_override_is_recognised_however_the_command_is_written(run_pre, adopted, command):
    text = command.format(cli=CLI, reason=REASON, reason_word="a-reason-of-twenty-characters")
    assert runs(run_pre, adopted, text) == DENIAL, text
    assert runs(run_pre, adopted, text, "verifier"), text


@pytest.mark.parametrize(
    "command",
    [
        "python3 {cli} status",
        "python3 {cli} pass r1",
        "python3 {cli} check-record spec x.json",
        "python3 {cli} gate r1 verify",
        "git commit -m 'docs: explain the override for plumbline'",
        "grep -rn override README.md",
        "echo override",
        "cat skills/override/SKILL.md",
        "ls skills/override",
        "python3 other.py override",
        "python3 -c 'print(1)'",
        "git log --grep=override",
    ],
)
def test_what_is_not_an_override_is_not_denied_by_this_rule(run_pre, adopted, command):
    assert runs(run_pre, adopted, command.format(cli=CLI)) is None, command


def test_the_override_rule_follows_the_directory_and_project_the_command_acts_in(run_pre, adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env = {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    command = f'python3 {CLI} override --reason "{REASON}"'
    plain = denial(run_pre(bash_payload(outside, command), outside, **env))
    assert plain is None  # no adopted repository there: the command refuses by itself, and this hook stays silent
    assert denial(run_pre(bash_payload(outside, f"cd {adopted} && {command}"), outside, **env)) == DENIAL
    assert denial(run_pre(bash_payload(outside, f"{command} --project {adopted}"), outside, **env)) == DENIAL
    assert denial(run_pre(bash_payload(outside, f"{command} --project={adopted}"), outside, **env)) == DENIAL
    assert denial(run_pre(bash_payload(outside, f"{command} --project {outside}"), outside, **env)) is None


def test_the_override_rule_holds_from_a_subdirectory_of_the_repository(run_pre, adopted):
    assert runs(run_pre, adopted, f'python3 {CLI} override --reason "{REASON}"', cwd=adopted / "src") == DENIAL


def test_the_power_shell_tool_is_held_to_the_rule_too(run_pre, adopted):
    from hookdata import tool_payload

    payload = tool_payload(adopted, "PowerShell", {"command": f'python3 {CLI} override --reason "{REASON}"'})
    assert denial(run_pre(payload, adopted)) == DENIAL


def test_the_override_command_itself_still_works_when_the_builder_runs_it(run_cli, adopted):
    # the hook only sees the model's tool calls; the CLI is what the skill's injection runs
    result = run_cli("override", "--reason", REASON, cwd=adopted)
    assert result.returncode == 0, result.stderr
    assert (adopted / ".plumbline" / "pass" / f"{git(adopted, 'rev-parse', 'HEAD').strip()}.override.json").is_file()


# --- override --reason - : the reason on stdin


def override_via_stdin(home, repo, text):
    return run_script(CLI, ["override", "--reason", "-"], repo, home, stdin=text)


def override_record(repo):
    [path] = (repo / ".plumbline" / "pass").glob("*.override.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_reason_of_dash_is_read_from_stdin_and_trimmed(home, adopted):
    result = override_via_stdin(home, adopted, f"\n  {REASON}  \n\n")
    assert result.returncode == 0, result.stderr
    assert override_record(adopted)["reason"] == REASON
    assert pl.check_record("override_record", override_record(adopted)) == []


def test_a_reason_on_stdin_may_hold_every_character_that_shell_would_read(home, adopted):
    nasty = "it's \"quoted\", $(touch pwned) `touch pwned` ${HOME} \\n; rm -rf x && echo done <tag> | more"
    assert override_via_stdin(home, adopted, nasty + "\n").returncode == 0
    assert override_record(adopted)["reason"] == nasty


def test_a_short_reason_on_stdin_is_refused_like_any_other(home, adopted):
    for text in ("", "   \n", "too short\n"):
        result = override_via_stdin(home, adopted, text)
        assert result.returncode == 1 and "the reason must be at least 20 characters" in result.stderr and "nothing was written" in result.stderr
    assert not (adopted / ".plumbline" / "pass").exists()


def test_a_reason_that_is_the_text_dash_alone_is_not_a_reason(home, adopted):
    result = run_script(CLI, ["override", "--reason", "-"], adopted, home, stdin="-")
    assert result.returncode == 1


# --- the skill


SKILL = REPO / "skills" / "override" / "SKILL.md"


def skill():
    return frontmatter(SKILL)


def fenced_injection(body):
    """The body of the fenced block opened with three backticks and a bang, which Claude Code runs when the skill loads."""
    match = re.search(r"```!\n(.*?)\n```", body, re.S)
    assert match, "the skill carries no fenced shell injection"
    return match.group(1)


def test_the_override_skill_is_the_builders_alone():
    fields, _ = skill()
    assert fields["disable-model-invocation"] == "true"
    assert fields["name"] == "override" and ": " not in fields["description"]
    assert fields["allowed-tools"] == "Bash(python3 *)"  # the injected command needs pre-approval to run without a prompt
    assert set(fields) == {"name", "description", "disable-model-invocation", "argument-hint", "allowed-tools"}


def test_the_override_skill_runs_the_command_through_shell_injection_with_the_arguments_as_the_reason():
    _, body = skill()
    command = fenced_injection(body)
    assert command.startswith('python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" override --reason - <<\'PLUMBLINE_OVERRIDE_REASON\'')
    assert "\n$ARGUMENTS\nPLUMBLINE_OVERRIDE_REASON" in command  # the arguments are the body of a quoted here-document
    assert "no tool call of yours was involved" in body


def test_the_injection_is_the_only_place_the_skill_runs_a_command():
    _, body = skill()
    assert len(re.findall(r"```!", body)) == 1
    assert "!`" not in body  # no inline injection besides the fenced block


def test_the_override_skill_does_not_ask_the_model_to_run_anything():
    _, body = skill()
    text = body.replace(fenced_injection(body), "")
    assert "Bash" not in text and "run `python3" not in text


def run_injection(home, repo, arguments):
    """Run the skill's fenced command through a real shell, as Claude Code does after it has substituted the plugin root and the arguments."""
    command = fenced_injection(skill()[1]).replace("${CLAUDE_PLUGIN_ROOT}", str(REPO)).replace("$ARGUMENTS", arguments)
    return subprocess.run(["bash", "-c", command], cwd=repo, capture_output=True, text=True, env=clean_env(home))


def test_the_injection_records_the_override_for_head_with_the_builders_reason(home, adopted):
    result = run_injection(home, adopted, REASON)
    assert result.returncode == 0, result.stderr
    head = git(adopted, "rev-parse", "HEAD").strip()
    assert f"override recorded for {head[:7]}" in result.stdout
    assert override_record(adopted)["reason"] == REASON


@pytest.mark.parametrize(
    "arguments",
    [
        "it's a typo fix, reviewed by hand; nothing else changed",
        'a "quoted" reason that is long enough to count',
        "$(touch pwned) and `touch pwned` and ${HOME} in a long enough reason",
        "PLUMBLINE reason with a; semicolon && and | pipes > redirects long enough",
        "a reason\nover two lines that is long enough to count",
    ],
)
def test_no_character_of_the_reason_is_read_as_shell_syntax(home, adopted, arguments):
    result = run_injection(home, adopted, arguments)
    assert result.returncode == 0, result.stderr
    assert override_record(adopted)["reason"] == arguments.strip()
    assert not (adopted / "pwned").exists()


def test_a_too_short_reason_fails_the_injection_which_aborts_the_skill_with_the_message(home, adopted):
    result = run_injection(home, adopted, "typo")
    assert result.returncode == 1 and "the reason must be at least 20 characters" in result.stderr
    assert not (adopted / ".plumbline" / "pass").exists()


def test_no_skill_that_the_model_may_invoke_puts_its_arguments_into_an_injected_command():
    for path in sorted((REPO / "skills").glob("*/SKILL.md")):
        fields, body = frontmatter(path)
        if fields.get("disable-model-invocation") == "true":
            continue
        for block in re.findall(r"```!\n(.*?)\n```", body, re.S):
            assert "$ARGUMENTS" not in block and "$0" not in block and "$1" not in block, path.parent.name  # a model could inject shell through them
        assert "!`" not in body, path.parent.name


def test_the_status_skill_injects_a_fixed_command_and_may_be_invoked_by_the_model():
    fields, body = frontmatter(REPO / "skills" / "status" / "SKILL.md")
    assert "disable-model-invocation" not in fields and fields["allowed-tools"] == "Bash(python3 *)"
    assert fenced_injection(body) == 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" status'


def test_the_run_skill_is_invocable_by_the_model_and_the_init_skill_is_not():
    run_fields, _ = frontmatter(REPO / "skills" / "run" / "SKILL.md")
    assert "disable-model-invocation" not in run_fields
    assert frontmatter(REPO / "skills" / "init" / "SKILL.md")[0]["disable-model-invocation"] == "true"


def test_the_run_skill_tells_the_model_to_leave_override_to_the_builder():
    _, body = frontmatter(REPO / "skills" / "run" / "SKILL.md")
    assert "`/plumbline:override` is the builder's command, typed by the builder" in body
    assert "override --reason" not in body  # it carries no command line for it
