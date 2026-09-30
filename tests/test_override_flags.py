"""OV-ABBR, hook side: `plumbline.py override --rea "..."` runs the override too (argparse takes any unambiguous prefix of a long
option), so the hook recognises the abbreviations of --reason and --project as it does the full words. The CLI itself has
allow_abbrev=False and refuses them; the hook does not depend on that."""
import pytest

import pre_tool_use as pre
from helpers import CLI
from hookdata import bash_payload
from rundata import adopt

REASON = "The pipeline cannot run offline; a one-line typo fix, reviewed by hand."
DENIAL = (
    "plumbline: `plumbline.py override` is the builder's command, and the builder types it: /plumbline:override followed by the reason. "
    "An agent, or the main session, does not run it. To skip the pipeline for this commit, ask the builder to type /plumbline:override."
)


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def ask(repo, command, role=None, cwd=None):
    return pre.decide(bash_payload(cwd or repo, command, agent_type=f"plumbline:{role}" if role else None))


REASON_FORMS = ["--reason", "--reaso", "--reas", "--rea", "--re", "--reason=text", "--rea=text", "--re=text"]


@pytest.mark.parametrize("option", REASON_FORMS)
def test_an_abbreviated_reason_option_is_still_an_override(adopted, option):
    value = "" if "=" in option else ' "twenty characters or more of reason"'
    # the script path held in a variable, as in the reviewer's reproduction: only the words `override` and the option give it away
    assert ask(adopted, f'P={CLI}; python3 "$P" override {option}{value}') == DENIAL
    assert ask(adopted, f'python3 {CLI} override {option}{value}') == DENIAL
    assert ask(adopted, f'python3 "$(cat /tmp/where)" override {option}{value}') == DENIAL
    assert ask(adopted, f'python3 "$SCRIPT" override --run r1 {option}{value}') == DENIAL


def test_the_reviewers_override_reproduction_is_denied_by_the_hook_and_refused_by_the_cli(run_cli, adopted):
    command = f'P={CLI}; python3 "$P" override --rea "{REASON}"'
    assert ask(adopted, command) == DENIAL
    assert ask(adopted, command, "verifier") == DENIAL
    # The premise moved: 0.3.0's CLI took `--rea` for `--reason` and wrote a real override. It has allow_abbrev=False now, so the command
    # refuses the abbreviation itself, and the hook (which does not depend on that) stops the line before the CLI is reached.
    for option in ("--rea", "--reas", "--reaso", "--re"):
        result = run_cli("override", option, REASON, cwd=adopted)
        assert result.returncode == 2 and "the following arguments are required: --reason" in result.stderr, (option, result.stderr)
    result = run_cli("override", "--reason", REASON, "--proj", str(adopted), cwd=adopted)
    assert result.returncode == 2 and "unrecognized arguments: --proj" in result.stderr, result.stderr
    assert not (adopted / ".plumbline").exists()  # none of the refusals wrote an override
    # the option typed in full is what the builder's /plumbline:override runs, and it records the override
    result = run_cli("override", "--reason", REASON, cwd=adopted)
    assert result.returncode == 0 and "override recorded" in result.stdout, result.stderr
    assert len(list((adopted / ".plumbline" / "pass").glob("*.override.json"))) == 1


@pytest.mark.parametrize("option", ["--project", "--projec", "--proje", "--proj", "--pro", "--pr", "--p"])
def test_an_abbreviated_project_option_still_names_the_repository(adopted, tmp_path, monkeypatch, option):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    command = f'python3 {CLI} override --reason "{REASON}"'
    assert ask(adopted, f"{command} {option} {adopted}", cwd=outside) == DENIAL
    assert ask(adopted, f"{command} {option}={adopted}", cwd=outside) == DENIAL
    assert ask(adopted, f'P={CLI}; python3 "$P" override --rea "{REASON}" {option} {adopted}', cwd=outside) == DENIAL
    assert ask(adopted, f"{command} {option} {outside}", cwd=outside) is None  # a project that is not adopted: the command refuses by itself


def test_the_options_of_override_that_are_not_the_reason_do_not_make_an_override(adopted):
    for command in (
        "git log --grep=override --reverse",
        "git log --grep override --rev",
        "echo override --r",
        "echo override --run r1",
        "echo override --rul",
        "grep -rn override --regexp x",
        "echo override --help",
        "echo override --",
        "echo override --=x",
        "echo override reason",
        "echo override -re",
    ):
        assert ask(adopted, command) is None, command


def test_an_ambiguous_prefix_is_not_taken_for_the_reason(adopted):
    # `--r` fits both --reason and --run, so argparse refuses it; `--ru` and `--run` are the run option
    for command in ("echo override --r x", "echo override --ru r1", "echo override --run r1"):
        assert ask(adopted, command) is None, command


@pytest.mark.parametrize(
    "word,expected",
    [
        ("--reason", "reason"), ("--reason=x", "reason"), ("--reaso", "reason"), ("--rea", "reason"), ("--rea=x y", "reason"), ("--re", "reason"),
        ("--run", "run"), ("--ru", "run"), ("--run=r1", "run"),
        ("--project", "project"), ("--proj", "project"), ("--pro=x", "project"), ("--p", "project"),
        ("--help", "help"), ("--he", "help"), ("--h", "help"),
        ("--r", None), ("--", None), ("-", None), ("-r", None), ("reason", None), ("--reasons", None), ("--rex", None), ("--x", None), ("--=x", None), ("", None),
    ],
)
def test_override_option(word, expected):
    assert pre.override_option(word) == expected


def test_the_plain_forms_are_still_denied_as_before(adopted):
    for command in (
        f'python3 {CLI} override --reason "{REASON}"',
        f'python3 {CLI} override --reason={REASON.replace(" ", "-")}',
        f"echo x | python3 {CLI} override --reason -",
        f'bash -c \'python3 {CLI} override --rea "{REASON}"\'',
    ):
        assert ask(adopted, command) == DENIAL, command
        assert ask(adopted, command, "verifier") == DENIAL, command


def test_an_override_is_recognised_in_a_line_that_moves_about_first(adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert ask(adopted, f'(cd /tmp && true); python3 {CLI} override --rea "{REASON}"') == DENIAL
    assert ask(adopted, f'cd /tmp; cd -; python3 {CLI} override --rea "{REASON}"') == DENIAL
    assert ask(adopted, f'cd {adopted}; python3 {CLI} override --rea "{REASON}"', cwd=outside) == DENIAL
