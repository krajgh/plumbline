"""The SessionStart hook: one JSON object, always exit 0."""
import json
import subprocess
import sys

import pytest

import plumbline as pl
import session_start
from helpers import SESSION_START, clean_env, write

DISCIPLINE = "plumbline:subagent-discipline skill."
ADOPTED = "plumbline: adopted in this repository (pipeline 'default', graft off). Changes go through the pipeline before they are pushed: /plumbline:run."
NOT_ADOPTED = "plumbline: not adopted in this repository; /plumbline:init adopts it."
PONYTAIL = (
    "ponytail is not enabled, and plumbline requires it: claude plugin marketplace add "
    "DietrichGebert/ponytail, then claude plugin install ponytail@ponytail."
)


def context(result):
    """The additionalContext, after checking the hook's whole contract."""
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1  # one line
    payload = json.loads(result.stdout)  # exactly one JSON object: extra data would raise
    assert list(payload) == ["hookSpecificOutput"]
    assert list(payload["hookSpecificOutput"]) == ["hookEventName", "additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return payload["hookSpecificOutput"]["additionalContext"]


def enable_ponytail(path, key="ponytail@ponytail", value=True):
    write(path, json.dumps({"enabledPlugins": {key: value}}))


@pytest.fixture
def plain(tmp_path):
    """A directory that is not in any git repository."""
    directory = tmp_path / "plain"
    directory.mkdir()
    return directory


@pytest.fixture
def hook(run_hook, tmp_path):
    """Run the hook in `cwd`; git may not look above tmp_path for a repository."""

    def run(cwd, **env):
        return run_hook(cwd, GIT_CEILING_DIRECTORIES=str(tmp_path), **env)

    return run


# --- the discipline note and its shape


def test_the_discipline_note_is_always_there(hook, plain, repo):
    for where in (plain, repo):
        ctx = context(hook(where))
        assert ctx.startswith("Standing rule: do the work through subagents sized to the job")
        assert DISCIPLINE in ctx


def test_the_note_names_plumbline_and_not_the_bridge():
    assert "bridge" not in session_start.DISCIPLINE_NOTE.lower()
    assert session_start.DISCIPLINE_NOTE.endswith("Details: the plumbline:subagent-discipline skill.")


def test_the_note_keeps_the_wording_the_discipline_moved_from():
    note = session_start.DISCIPLINE_NOTE
    for fragment in (
        "Haiku for mechanical steps (commands, scripted edits, plumbing, collecting numbers)",
        "Sonnet where judgement matters (code that needs its surroundings read, reviews, judging)",
        "This session keeps the brief, the review of what comes back, and the decisions, and it alone writes the files the whole project shares.",
    ):
        assert fragment in note


# --- adoption


def test_an_adopted_repository_gets_the_adopted_line(hook, repo, run_cli):
    run_cli("init", cwd=repo)
    ctx = context(hook(repo))
    assert ADOPTED in ctx
    assert NOT_ADOPTED not in ctx


def test_graft_on_is_reported(hook, repo, run_cli):
    run_cli("init", "--graft", cwd=repo)
    assert "(pipeline 'default', graft on)" in context(hook(repo))


def test_the_pipeline_name_comes_from_the_pipeline(hook, repo):
    custom = (pl.PIPELINE_DIR / "default.toml").read_text().replace('name = "default"', 'name = "house"', 1)
    write(repo / "house.toml", custom)
    write(repo / "plumbline.toml", 'schema = 1\npipeline = "house.toml"\n')
    assert "(pipeline 'house', graft off)" in context(hook(repo))


def test_a_repository_without_plumbline_toml_gets_the_not_adopted_line(hook, repo):
    ctx = context(hook(repo))
    assert NOT_ADOPTED in ctx
    assert "plumbline: adopted in this repository" not in ctx


def test_a_subdirectory_of_an_adopted_repository_counts_as_adopted(hook, repo, run_cli):
    run_cli("init", cwd=repo)
    assert ADOPTED in context(hook(repo / "src"))


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("schema = 2\n", "$.schema: must be 1"),
        ("schema = = 1\n", "invalid TOML"),
        ('schema = 1\npipeline = "nope"\n', "pipeline 'nope' is not shipped with plumbline"),
        ("schema = 1\n[grfat]\nenabled = true\n", "$.grfat: unexpected key"),
        ('schema = 1\n[matrix.docs]\nstages = ["ghost"]\n', "row 'docs': unknown stage 'ghost'"),
    ],
)
def test_an_invalid_config_gets_one_line_naming_the_problem(hook, repo, text, fragment):
    write(repo / "plumbline.toml", text)
    ctx = context(hook(repo))
    [line] = [l for l in ctx.split("\n") if l.startswith("plumbline: plumbline.toml is invalid: ")]
    assert fragment in line
    assert "plumbline.toml: plumbline.toml" not in line
    assert "adopted in this repository" not in ctx and NOT_ADOPTED not in ctx


def test_an_invalid_config_with_several_problems_says_how_many_more(hook, repo):
    write(repo / "plumbline.toml", 'schema = 1\n[matrix.docs]\nstages = ["ghost", "phantom"]\n')
    [line] = [l for l in context(hook(repo)).split("\n") if "is invalid" in l]
    assert "(and 1 more)" in line


def test_nothing_about_adoption_outside_a_git_repository(hook, plain):
    ctx = context(hook(plain))
    assert "adopted" not in ctx and "plumbline.toml" not in ctx
    write(plain / "plumbline.toml", "schema = 1\n")  # a config with no repository around it
    ctx = context(hook(plain))
    assert "adopted" not in ctx and "plumbline.toml" not in ctx


def test_claude_project_dir_wins_over_the_working_directory(hook, repo, plain, run_cli):
    run_cli("init", cwd=repo)
    assert ADOPTED in context(hook(plain, CLAUDE_PROJECT_DIR=str(repo)))


def test_a_claude_project_dir_that_is_not_a_repository_says_nothing_about_adoption(hook, repo, plain):
    assert "adopted" not in context(hook(repo, CLAUDE_PROJECT_DIR=str(plain)))


def test_a_claude_project_dir_that_does_not_exist_still_gives_the_note(hook, plain, tmp_path):
    ctx = context(hook(plain, CLAUDE_PROJECT_DIR=str(tmp_path / "gone")))
    assert DISCIPLINE in ctx and "adopted" not in ctx


def test_without_git_on_the_path_the_hook_still_answers(hook, repo, tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    ctx = context(hook(repo, PATH=str(empty_bin)))
    assert DISCIPLINE in ctx and "adopted" not in ctx


# --- ponytail


def test_the_ponytail_warning_when_no_settings_enable_it(hook, repo):
    assert PONYTAIL in context(hook(repo))


def test_the_ponytail_warning_outside_a_git_repository_too(hook, plain):
    assert PONYTAIL in context(hook(plain))


def test_no_warning_when_the_user_settings_enable_ponytail(hook, repo, home):
    enable_ponytail(home / ".claude" / "settings.json")
    assert "ponytail is not enabled" not in context(hook(repo))


def test_no_warning_when_the_project_settings_enable_it(hook, repo):
    enable_ponytail(repo / ".claude" / "settings.json")
    assert "ponytail is not enabled" not in context(hook(repo))


def test_no_warning_when_the_local_project_settings_enable_it(hook, repo):
    enable_ponytail(repo / ".claude" / "settings.local.json")
    assert "ponytail is not enabled" not in context(hook(repo))


def test_project_settings_are_found_from_a_subdirectory_via_the_top_level(hook, repo):
    enable_ponytail(repo / ".claude" / "settings.json")
    assert "ponytail is not enabled" not in context(hook(repo / "src"))


def test_project_settings_are_found_through_claude_project_dir(hook, repo, plain):
    enable_ponytail(repo / ".claude" / "settings.json")
    assert "ponytail is not enabled" not in context(hook(plain, CLAUDE_PROJECT_DIR=str(repo)))


def test_any_marketplace_suffix_counts(hook, repo, home):
    enable_ponytail(home / ".claude" / "settings.json", key="ponytail@some-mirror")
    assert "ponytail is not enabled" not in context(hook(repo))


@pytest.mark.parametrize(
    "key,value",
    [
        ("ponytail@ponytail", False),
        ("ponytail@ponytail", "true"),
        ("ponytail@ponytail", 1),
        ("ponytail", True),
        ("not-ponytail@ponytail", True),
        ("plumbline@plumbline", True),
    ],
)
def test_a_key_that_is_not_ponytail_set_to_true_does_not_count(hook, repo, home, key, value):
    enable_ponytail(home / ".claude" / "settings.json", key=key, value=value)
    assert PONYTAIL in context(hook(repo))


@pytest.mark.parametrize("text", ["{not json", "[]", '{"enabledPlugins": []}', '{"enabledPlugins": null}', "", '"text"'])
def test_unreadable_or_odd_settings_files_do_not_crash_the_hook(hook, repo, home, text):
    write(home / ".claude" / "settings.json", text)
    write(repo / ".claude" / "settings.json", text)
    assert PONYTAIL in context(hook(repo))


def test_a_disabled_user_setting_is_overridden_by_an_enabling_project_setting(hook, repo, home):
    enable_ponytail(home / ".claude" / "settings.json", value=False)
    enable_ponytail(repo / ".claude" / "settings.local.json")
    assert "ponytail is not enabled" not in context(hook(repo))


# --- the whole message, and robustness


def test_the_full_message_in_an_adopted_repository_without_ponytail(hook, repo, run_cli):
    run_cli("init", cwd=repo)
    parts = context(hook(repo)).split("\n\n")
    assert parts[0].endswith(DISCIPLINE)
    assert parts[1:] == [ADOPTED, PONYTAIL]


def test_the_full_message_when_everything_is_in_order(hook, repo, run_cli, home):
    run_cli("init", cwd=repo)
    enable_ponytail(home / ".claude" / "settings.json")
    assert context(hook(repo)).split("\n\n")[1:] == [ADOPTED]


def test_a_plumbline_that_cannot_be_imported_still_gives_the_note_and_exit_0(plain, home, tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "session_start.py").write_text(SESSION_START.read_text(encoding="utf-8"), encoding="utf-8")
    (broken / "plumbline.py").write_text("raise SystemExit('this Python is too old')\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(broken / "session_start.py")],
        cwd=plain, capture_output=True, text=True, env=clean_env(home),
    )
    ctx = context(result)
    assert DISCIPLINE in ctx
    assert "could not check this repository (this Python is too old)" in ctx


def test_a_failure_of_the_whole_build_still_prints_the_note(monkeypatch, capsys):
    monkeypatch.setattr(session_start, "build_context", lambda: 1 / 0)
    session_start.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["additionalContext"] == session_start.DISCIPLINE_NOTE


def test_the_hook_writes_nothing_to_stderr(hook, repo):
    assert hook(repo).stderr == ""


def test_the_hook_finds_the_project_from_claude_project_dir_whatever_the_cwd(home, repo, run_cli):
    run_cli("init", cwd=repo)
    result = subprocess.run(
        [sys.executable, str(SESSION_START)],
        cwd=str(repo.parent), capture_output=True, text=True,
        env=clean_env(home, CLAUDE_PROJECT_DIR=str(repo)),
    )
    assert ADOPTED in context(result)
