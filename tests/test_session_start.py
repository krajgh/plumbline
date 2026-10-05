"""The SessionStart hook: one JSON object, always exit 0."""
import json
import re
import subprocess
import sys

import pytest

import plumbline as pl
import session_start
from helpers import SESSION_START, clean_env, write

DISCIPLINE = "plumbline:subagent-discipline skill."
ADOPTED = "plumbline: adopted in this repository (pipeline 'default', graft off). Changes go through the pipeline before they are pushed: /plumbline:run."
NOT_ADOPTED = "plumbline: not adopted in this repository; /plumbline:init adopts it."
MATCHER = "^(?!plumbline:)|^plumbline:(planner|test-writer|builder)$"  # what session start recommends: ponytail stays on for every subagent that is no plumbline agent
ANCHORED = "^plumbline:(planner|test-writer|builder)$"  # the value 0.3.0 recommended: right for plumbline's agents, and it switches ponytail off for all others
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
def adopted(repo, run_cli):
    """A repository that has adopted plumbline: the matcher line is shown only in one."""
    assert run_cli("init", cwd=repo).returncode == 0
    return repo


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
    # in order now means ponytail is enabled and its subagent matcher is set to plumbline's making agents
    write(home / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"ponytail@ponytail": True}, "env": {"PONYTAIL_SUBAGENT_MATCHER": MATCHER}}))
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


# --- ponytail's subagent matcher


ENV_VAR = "PONYTAIL_SUBAGENT_MATCHER"
AGENTS = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")
MAKING = ("planner", "test-writer", "builder")


def settings_with(path, matcher=None, enabled=True, **extra_env):
    env = dict(extra_env)
    if matcher is not None:
        env[ENV_VAR] = matcher
    data = {"enabledPlugins": {"ponytail@ponytail": enabled}}
    if env:
        data["env"] = env
    write(path, json.dumps(data))


def matcher_line(ctx):
    lines = [l for l in ctx.split("\n\n") if ENV_VAR in l or "ponytail reaches every subagent" in l]
    assert len(lines) <= 1, lines
    return lines[0] if lines else None


def test_the_matcher_line_says_what_to_add_once_when_the_matcher_is_unset(hook, adopted, home):
    enable_ponytail(home / ".claude" / "settings.json")
    line = matcher_line(context(hook(adopted)))
    assert line == (
        "ponytail reaches every subagent, plumbline's reviewers included. Scope it to the agents that make the change, once: "
        f'add "env": {{"{ENV_VAR}": "{MATCHER}"}} to ~/.claude/settings.json (plumbline changes none of your settings). '
        "That value keeps ponytail on for every subagent that is not a plumbline agent."
    )
    assert "\n" not in line


def test_the_matcher_line_comes_after_the_adoption_line_and_replaces_the_ponytail_warning(hook, adopted, home):
    enable_ponytail(home / ".claude" / "settings.json")
    parts = context(hook(adopted)).split("\n\n")
    assert parts[1] == ADOPTED and parts[2].startswith("ponytail reaches every subagent") and len(parts) == 3
    assert PONYTAIL not in parts


def test_no_matcher_line_while_ponytail_itself_is_not_enabled(hook, adopted, home):
    ctx = context(hook(adopted))
    assert PONYTAIL in ctx and matcher_line(ctx) is None
    settings_with(home / ".claude" / "settings.json", MATCHER, enabled=False)
    ctx = context(hook(adopted))
    assert PONYTAIL in ctx and matcher_line(ctx) is None


def test_the_right_matcher_in_the_user_settings_silences_the_line(hook, adopted, home):
    settings_with(home / ".claude" / "settings.json", MATCHER)
    assert matcher_line(context(hook(adopted))) is None


def test_the_right_matcher_in_the_project_settings_or_the_local_ones_silences_it_too(hook, adopted, home):
    enable_ponytail(home / ".claude" / "settings.json")
    write(adopted / ".claude" / "settings.json", json.dumps({"env": {ENV_VAR: MATCHER}}))
    assert matcher_line(context(hook(adopted))) is None
    (adopted / ".claude" / "settings.json").unlink()
    write(adopted / ".claude" / "settings.local.json", json.dumps({"env": {ENV_VAR: MATCHER}}))
    assert matcher_line(context(hook(adopted))) is None


def test_the_process_environment_counts_first_because_it_is_what_ponytails_hook_will_see(hook, adopted, home):
    enable_ponytail(home / ".claude" / "settings.json")
    assert matcher_line(context(hook(adopted, **{ENV_VAR: MATCHER}))) is None
    assert "reaches every subagent" not in context(hook(adopted, **{ENV_VAR: MATCHER}))
    wrong = matcher_line(context(hook(adopted, **{ENV_VAR: "builder"})))
    assert wrong and '("builder")' in wrong


def test_the_local_settings_win_over_the_project_settings_and_those_over_the_users(hook, adopted, home):
    settings_with(home / ".claude" / "settings.json", MATCHER)
    write(adopted / ".claude" / "settings.json", json.dumps({"env": {ENV_VAR: "planner"}}))
    assert '("planner")' in matcher_line(context(hook(adopted)))
    write(adopted / ".claude" / "settings.local.json", json.dumps({"env": {ENV_VAR: MATCHER}}))
    assert matcher_line(context(hook(adopted))) is None


@pytest.mark.parametrize(
    "value,said",
    [
        (".*", "it also reaches verifier, prosecutor, defender, detective, canary"),
        ("plumbline", "it also reaches verifier, prosecutor, defender, detective, canary"),
        ("^plumbline:", "it also reaches verifier, prosecutor, defender, detective, canary"),
        ("builder", "it misses planner, test-writer"),
        ("^plumbline:builder$", "it misses planner, test-writer"),
        ("^plumbline:(planner|test-writer)$", "it misses builder"),
        ("^plumbline:(planner|test-writer|builder|verifier)$", "it also reaches verifier"),
        ("^plumbline:(planner|builder|prosecutor)$", "it misses test-writer; it also reaches prosecutor"),
        ("^nothing-like-that$", "it misses planner, test-writer, builder"),
    ],
)
def test_a_matcher_that_does_not_reach_exactly_the_making_agents_is_named_with_what_it_gets_wrong(hook, adopted, home, value, said):
    settings_with(home / ".claude" / "settings.json", value)
    line = matcher_line(context(hook(adopted)))
    assert line == f'{ENV_VAR} ("{value}") should reach exactly plumbline\'s planner, test-writer and builder: {said}. Set it to "{MATCHER}".'


@pytest.mark.parametrize(
    "value",
    [
        MATCHER,
        ANCHORED,  # the value 0.3.0 recommended: exact among plumbline's agents
        ANCHORED.upper(),  # case-insensitive, as ponytail's
        "plumbline:(planner|test-writer|builder)",  # unanchored is fine: no other plumbline agent has those names
        "(planner|test-writer|builder)",
        "builder|planner|test-writer",
        "^plumbline:(?:planner|test-writer|builder)$",
    ],
)
def test_any_matcher_that_reaches_exactly_the_making_agents_is_accepted(hook, adopted, home, value):
    settings_with(home / ".claude" / "settings.json", value)
    assert matcher_line(context(hook(adopted))) is None


def test_a_matcher_that_is_not_a_regular_expression_is_named(hook, adopted, home):
    settings_with(home / ".claude" / "settings.json", "(planner")
    line = matcher_line(context(hook(adopted)))
    assert line == f'{ENV_VAR} ("(planner") is not a valid regular expression, so ponytail reaches every subagent. Set it to "{MATCHER}".'


@pytest.mark.parametrize("value", ["", None, 5, True, ["planner"], {"a": 1}])
def test_an_empty_or_odd_matcher_value_counts_as_unset(hook, adopted, home, value):
    write(home / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"ponytail@ponytail": True}, "env": {ENV_VAR: value}}))
    assert matcher_line(context(hook(adopted))).startswith("ponytail reaches every subagent")


@pytest.mark.parametrize("text", ["{not json", "[]", '{"enabledPlugins": {"ponytail@ponytail": true}, "env": []}', '{"enabledPlugins": {"ponytail@ponytail": true}, "env": null}'])
def test_unreadable_or_odd_settings_do_not_crash_the_matcher_check(hook, adopted, home, text):
    write(home / ".claude" / "settings.json", '{"enabledPlugins": {"ponytail@ponytail": true}}')
    write(adopted / ".claude" / "settings.json", text)
    assert matcher_line(context(hook(adopted))).startswith("ponytail reaches every subagent")


def test_the_matcher_line_shows_only_in_a_repository_that_has_adopted_plumbline(hook, repo, plain, home, adopted):  # C-12
    enable_ponytail(home / ".claude" / "settings.json")
    assert matcher_line(context(hook(adopted))).startswith("ponytail reaches every subagent")
    other = adopted.parent / "other"  # a repository that has not adopted plumbline
    other.mkdir()
    for command in (["git", "init", "-q", "-b", "main", str(other)],):
        assert subprocess.run(command, capture_output=True).returncode == 0
    for where in (other, plain):
        ctx = context(hook(where))
        assert matcher_line(ctx) is None and "ponytail reaches every subagent" not in ctx and PONYTAIL not in ctx
    ctx = context(hook(other))
    assert NOT_ADOPTED in ctx  # the hint stays; the matcher line does not


def test_an_invalid_config_still_counts_as_adopted_for_the_matcher_line(hook, repo, home):
    write(repo / "plumbline.toml", "schema = = 1\n")
    enable_ponytail(home / ".claude" / "settings.json")
    assert matcher_line(context(hook(repo))).startswith("ponytail reaches every subagent")


def test_the_ponytail_warning_is_still_shown_where_plumbline_is_not_adopted(hook, repo, plain):
    assert PONYTAIL in context(hook(repo)) and PONYTAIL in context(hook(plain))


def test_plumbline_writes_no_settings_and_nothing_else_under_the_home(hook, adopted, home):
    enable_ponytail(home / ".claude" / "settings.json")
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    context(hook(adopted))
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def node_or_skip():
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node is not installed")


def test_the_recommended_matcher_keeps_ponytail_on_for_other_subagents_and_reaches_exactly_the_making_agents_among_plumbline_s():  # C-12
    # ponytail tests it with `new RegExp(value, 'i').test(agent_type)` (hooks/ponytail-subagent.js), not with Python's re
    import subprocess

    node_or_skip()
    script = "const re = new RegExp(process.argv[1], 'i'); console.log(JSON.stringify(process.argv.slice(2).map(t => re.test(t))))"
    others = ["Explore", "general-purpose", "probe:builder", "planner", "other-plugin:verifier"]
    types = [f"plumbline:{name}" for name in AGENTS] + others
    result = subprocess.run(["node", "-e", script, MATCHER, *types], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    reached = json.loads(result.stdout)
    assert [t for t, hit in zip(types, reached) if hit] == [f"plumbline:{name}" for name in MAKING] + others  # every subagent that is no plumbline agent, too
    assert session_start.MATCHER_EXAMPLE == MATCHER and session_start.MAKING_AGENTS == MAKING and session_start.ALL_AGENTS == AGENTS


def test_the_anchored_value_of_0_3_0_would_switch_ponytail_off_for_every_other_subagent():
    import subprocess

    node_or_skip()
    script = "const re = new RegExp(process.argv[1], 'i'); console.log(JSON.stringify(process.argv.slice(2).map(t => re.test(t))))"
    result = subprocess.run(["node", "-e", script, ANCHORED, "Explore", "general-purpose"], capture_output=True, text=True)
    assert json.loads(result.stdout) == [False, False]


# --- the matcher is checked the way ponytail uses it: with node where it is installed

PYTHON_ONLY = "^plumbline:(?P<r>planner|test-writer|builder)$"  # a named group, Python's spelling
JS_ONLY = "^plumbline:(?<r>planner|test-writer|builder)$"  # a named group, Javascript's spelling


def without_node(tmp_path):
    """A PATH with git and nothing else: no node."""
    import shutil

    bin_dir = tmp_path / "bin-without-node"
    bin_dir.mkdir(exist_ok=True)
    if not (bin_dir / "git").exists():
        (bin_dir / "git").symlink_to(shutil.which("git"))
    return str(bin_dir)


def test_a_matcher_only_python_accepts_is_named_because_ponytails_javascript_rejects_it(hook, adopted, home):  # C-11
    node_or_skip()
    settings_with(home / ".claude" / "settings.json", PYTHON_ONLY)
    line = matcher_line(context(hook(adopted)))
    assert line == f'{ENV_VAR} ("{PYTHON_ONLY}") is not a valid regular expression, so ponytail reaches every subagent. Set it to "{MATCHER}".'


def test_a_matcher_only_javascript_accepts_is_accepted_when_node_is_there(hook, adopted, home):  # C-11
    node_or_skip()
    settings_with(home / ".claude" / "settings.json", JS_ONLY)
    assert matcher_line(context(hook(adopted))) is None


def test_without_node_the_check_falls_back_to_pythons_regular_expressions(hook, adopted, home, tmp_path):
    settings_with(home / ".claude" / "settings.json", PYTHON_ONLY)
    assert matcher_line(context(hook(adopted, PATH=without_node(tmp_path)))) is None
    settings_with(home / ".claude" / "settings.json", JS_ONLY)
    assert "is not a valid regular expression" in matcher_line(context(hook(adopted, PATH=without_node(tmp_path))))


def test_the_recommended_value_passes_the_check_with_and_without_node(hook, adopted, home, tmp_path):
    settings_with(home / ".claude" / "settings.json", MATCHER)
    assert matcher_line(context(hook(adopted, PATH=without_node(tmp_path)))) is None
    if __import__("shutil").which("node"):
        assert matcher_line(context(hook(adopted))) is None


def test_matcher_reach_answers_from_node_when_it_is_installed_and_from_python_otherwise(monkeypatch):
    node_or_skip()
    assert session_start.matcher_reach(MATCHER) == (True, list(MAKING))
    assert session_start.matcher_reach(PYTHON_ONLY) == (False, [])
    assert session_start.matcher_reach(JS_ONLY) == (True, list(MAKING))
    assert session_start.matcher_reach("-not-an-option") == (True, [])  # a value that looks like an option is a pattern, not a node flag
    monkeypatch.setattr(session_start.shutil, "which", lambda name: None)
    assert session_start.matcher_reach(MATCHER) == (True, list(MAKING))
    assert session_start.matcher_reach(PYTHON_ONLY) == (True, list(MAKING))
    assert session_start.matcher_reach(JS_ONLY) == (False, [])


def test_a_node_that_cannot_answer_is_no_reason_to_fail_the_check(monkeypatch, tmp_path):
    broken = tmp_path / "node"
    broken.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setattr(session_start.shutil, "which", lambda name: str(broken))
    assert session_start.matcher_reach(MATCHER) == (True, list(MAKING))  # Python answers instead


def test_the_session_start_agent_names_are_the_pipelines():
    assert session_start.ALL_AGENTS == pl.AGENT_ROLES


# --- the always-on notes state rules as what to do

PROHIBITION = re.compile(r"\b(never|don't|do not|must not|cannot|can't|won't|shouldn't|forbidden|prohibited|not allowed)\b", re.I)


def test_the_always_on_notes_state_rules_as_what_to_do(hook, adopted, home):
    # a prohibition belongs in a hook wherever one can enforce it; prose keeps what only judgement can apply
    enable_ponytail(home / ".claude" / "settings.json")
    unset = context(hook(adopted))
    settings_with(home / ".claude" / "settings.json", "(planner")
    invalid = context(hook(adopted))
    settings_with(home / ".claude" / "settings.json", "builder")
    wrong = context(hook(adopted))
    (home / ".claude" / "settings.json").unlink()
    warned = context(hook(adopted))
    for text in (unset, invalid, wrong, warned, session_start.DISCIPLINE_NOTE, session_start.NOT_ADOPTED, session_start.PONYTAIL_WARNING):
        assert PROHIBITION.findall(text) == [], text
