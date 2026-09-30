"""Launching agents in an adopted repository: AG-ISOL (no isolation other than none), AG-MODEL (the model an agent's definition
pins), AG-ROLE (a stage's files are briefed to the stage's plumbline agent, not to a general one)."""
import re
import shutil

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import REPO, write
from hookdata import tool_payload
from rundata import adopt

ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective")
ISOLATION = "plumbline: a run's records live in the main checkout, so plumbline:{role} runs there. Launch it without `isolation`."
ROLE_RULE = "plumbline stages run through the plumbline:* agents"


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def pinned(role):
    text = (REPO / "agents" / f"{role}.md").read_text(encoding="utf-8")
    return re.search(r"^model:\s*(\S+)", text, re.M).group(1)


def launch(repo, subagent="plumbline:builder", tool="Agent", cwd=None, **fields):
    tool_input = {"description": "x", "prompt": "y", **fields}
    if subagent is not False:
        tool_input["subagent_type"] = subagent
    return pre.decide(tool_payload(cwd or repo, tool, tool_input))


# ---------------------------------------------------------------------------------------- AG-ISOL


@pytest.mark.parametrize("isolation", ["worktree", "remote", "local", "docker", "", "none", "Worktree", True, False, 0, {"kind": "remote"}])
@pytest.mark.parametrize("role", ROLES)
def test_a_plumbline_agent_is_launched_with_no_isolation_of_any_kind(adopted, role, isolation):
    assert launch(adopted, f"plumbline:{role}", isolation=isolation) == ISOLATION.format(role=role)


def test_the_reviewers_isolation_remote_reproduction_is_denied_and_the_older_tool_name_too(adopted):
    assert launch(adopted, "plumbline:builder", isolation="remote") == ISOLATION.format(role="builder")
    assert launch(adopted, "plumbline:builder", tool="Task", isolation="remote") == ISOLATION.format(role="builder")


def test_no_isolation_key_and_a_null_one_are_the_same_thing(adopted):
    assert launch(adopted, "plumbline:builder") is None
    assert launch(adopted, "plumbline:builder", isolation=None) is None
    assert launch(adopted, "plumbline:prosecutor", run_in_background=True) is None
    assert launch(adopted, "plumbline:builder", run_in_background=False) is None


def test_other_agents_keep_their_isolation(adopted):
    for subagent in ("Explore", "probe:echo", "general-purpose"):
        for isolation in ("worktree", "remote"):
            assert launch(adopted, subagent, isolation=isolation) is None, (subagent, isolation)


def test_the_isolation_rule_is_silent_where_plumbline_is_not_adopted(repo):
    assert launch(repo, "plumbline:builder", isolation="remote") is None
    assert launch(repo, "plumbline:builder", isolation="worktree", model="haiku") is None


# --------------------------------------------------------------------------------------- AG-MODEL


def test_the_pins_are_the_ones_in_the_agent_files():
    assert {role: pinned(role) for role in ROLES} == {
        "planner": "sonnet", "test-writer": "sonnet", "builder": "sonnet", "verifier": "haiku", "prosecutor": "sonnet", "defender": "haiku", "detective": "sonnet"
    }
    for role in ROLES:
        assert pre.pinned_model(pl, role) == pinned(role)


@pytest.mark.parametrize("role", ROLES)
def test_a_plumbline_agent_runs_on_the_model_its_definition_pins(adopted, role):
    assert launch(adopted, f"plumbline:{role}") is None  # no model: the pinned one
    assert launch(adopted, f"plumbline:{role}", model=None) is None
    assert launch(adopted, f"plumbline:{role}", model=pinned(role)) is None
    assert launch(adopted, f"plumbline:{role}", model=pinned(role).upper()) is None
    assert launch(adopted, f"plumbline:{role}", model=f" {pinned(role)} ") is None
    other = "opus" if pinned(role) != "opus" else "haiku"
    reason = launch(adopted, f"plumbline:{role}", model=other)
    assert reason == f"plumbline: plumbline:{role} is pinned to the {pinned(role)} model. Launch it without `model`, or with `model: {pinned(role)}`."


def test_the_reviewers_model_reproduction_is_denied(adopted):
    assert "pinned to the sonnet model" in launch(adopted, "plumbline:prosecutor", model="haiku")
    assert "pinned to the haiku model" in launch(adopted, "plumbline:verifier", model="opus")
    assert "pinned to the haiku model" in launch(adopted, "plumbline:defender", model="sonnet")


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "inherit", "", 5, ["sonnet"], {"name": "sonnet"}, "sonnet-latest", "sonne"])
def test_a_model_that_is_not_the_pinned_alias_is_denied(adopted, model):
    assert "pinned to the sonnet model" in launch(adopted, "plumbline:builder", model=model), model


def test_the_pin_is_read_from_the_agent_definition_not_written_into_the_hook(adopted, tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    shutil.copytree(REPO / "agents", plugin / "agents")
    text = (plugin / "agents" / "builder.md").read_text(encoding="utf-8").replace("model: sonnet", 'model: "opus"', 1)
    (plugin / "agents" / "builder.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(pl, "PLUGIN_ROOT", plugin)
    assert pre.pinned_model(pl, "builder") == "opus"
    assert launch(adopted, "plumbline:builder", model="opus") is None
    assert "pinned to the opus model" in launch(adopted, "plumbline:builder", model="sonnet")
    assert launch(adopted, "plumbline:planner", model="sonnet") is None  # its definition is unchanged


def test_a_missing_or_unpinned_definition_leaves_the_model_alone(adopted, tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "agents").mkdir(parents=True)
    write(plugin / "agents" / "builder.md", "---\nname: builder\n---\nno model line\n")
    monkeypatch.setattr(pl, "PLUGIN_ROOT", plugin)
    assert pre.pinned_model(pl, "builder") is None and pre.pinned_model(pl, "planner") is None
    assert launch(adopted, "plumbline:builder", model="haiku") is None
    assert launch(adopted, "plumbline:planner", model="haiku") is None


def test_only_the_front_matter_is_read_for_the_model(adopted, tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "agents").mkdir(parents=True)
    write(plugin / "agents" / "builder.md", "---\nname: builder\nmodel: haiku\n---\nmodel: opus\n")
    monkeypatch.setattr(pl, "PLUGIN_ROOT", plugin)
    assert pre.pinned_model(pl, "builder") == "haiku"


def test_other_agents_and_unknown_plumbline_types_have_no_pin(adopted):
    assert launch(adopted, "Explore", model="haiku") is None
    assert launch(adopted, "plumbline:wizard", model="haiku") is None


def test_the_model_rule_is_silent_where_plumbline_is_not_adopted(repo):
    assert launch(repo, "plumbline:prosecutor", model="haiku") is None


# --------------------------------------------------------------------------------------- AG-ROLE

BRIEFS = [
    "Read .plumbline/runs/r1/plan.json and build it.",
    "write your record to .plumbline/runs/r1/build.json",
    "the plan is in .plumbline/",
    "look at .plumbline/runs/r1/verify.json and fix what failed",
    "x" * 1000 + " .plumbline/runs/r1/tests.json",
]


@pytest.mark.parametrize("subagent", ["general-purpose", "Explore", "probe:echo", "fork", "Plan", "claude", "", None, False])
@pytest.mark.parametrize("prompt", BRIEFS)
def test_a_brief_about_the_runs_files_goes_to_the_stages_plumbline_agent(adopted, subagent, prompt):
    reason = launch(adopted, subagent, prompt=prompt)
    assert reason and ROLE_RULE in reason, (subagent, prompt)


@pytest.mark.parametrize("prompt", ["see .PLUMBLINE/runs/r1/plan.json", "see .Plumbline/", "look in .plumbline\\runs\\r1", "the record is under .plumbline/runs", "C:\\repo\\.plumbline\\runs"])
def test_the_runs_directory_is_recognised_in_any_case_with_either_slash(adopted, prompt):
    assert ROLE_RULE in launch(adopted, "general-purpose", prompt=prompt), prompt


def test_the_reviewers_ag_role_reproduction_is_denied(adopted):
    # the builder's brief, sent to a general agent or to none in particular
    brief = "You are the builder. Run r1. Read the plan at .plumbline/runs/r1/plan.json and write the code."
    assert ROLE_RULE in launch(adopted, "general-purpose", prompt=brief)
    assert ROLE_RULE in launch(adopted, False, prompt=brief)
    assert ROLE_RULE in launch(adopted, "general-purpose", prompt=brief, tool="Task")


def test_the_denial_names_the_agent_and_the_agents_to_use(adopted):
    reason = launch(adopted, "general-purpose", prompt="see .plumbline/runs/r1/plan.json")
    assert reason == (
        "plumbline: plumbline stages run through the plumbline:* agents, so a brief about .plumbline/ goes to the stage's agent "
        "(plumbline:planner, test-writer, builder, verifier, prosecutor, defender or detective), not to general-purpose."
    )
    assert reason.endswith("not to a general agent.") is False
    assert launch(adopted, False, prompt="see .plumbline/runs/r1/plan.json").endswith("not to a general agent.")


def test_the_description_counts_as_part_of_the_brief(adopted):
    assert ROLE_RULE in launch(adopted, "general-purpose", description="read .plumbline/runs/r1/plan.json", prompt="do it")


@pytest.mark.parametrize("prompt", ["do it", "build the change", "plumbline is a plugin", "the plumbline/ folder", "the .plumbline directory", "see plumbline.toml", "", "/plumbline:run"])
def test_a_brief_that_does_not_mention_the_runs_directory_is_left_alone(adopted, prompt):
    for subagent in ("general-purpose", "Explore", False):
        assert launch(adopted, subagent, prompt=prompt) is None, (subagent, prompt)


def test_a_stage_agent_is_briefed_on_the_runs_files_of_course(adopted):
    for role in ROLES:
        assert launch(adopted, f"plumbline:{role}", prompt="write your record to .plumbline/runs/r1/x.json") is None, role


def test_the_role_rule_is_silent_where_plumbline_is_not_adopted(repo):
    assert launch(repo, "general-purpose", prompt="read .plumbline/runs/r1/plan.json") is None


def test_the_session_project_counts_when_the_working_directory_is_elsewhere(adopted, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert launch(adopted, "general-purpose", prompt="read .plumbline/runs/r1/plan.json", cwd=outside) is None  # nothing there is adopted
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(adopted))
    assert ROLE_RULE in launch(adopted, "general-purpose", prompt="read .plumbline/runs/r1/plan.json", cwd=outside)
    assert launch(adopted, "plumbline:builder", isolation="remote", cwd=outside)


def test_the_launch_rules_through_the_hook_process(run_pre, adopted):
    from hookdata import denial

    payload = tool_payload(adopted, "Agent", {"description": "x", "prompt": "read .plumbline/runs/r1/plan.json", "subagent_type": "general-purpose"})
    assert ROLE_RULE in denial(run_pre(payload, adopted))
    payload = tool_payload(adopted, "Agent", {"description": "x", "prompt": "y", "subagent_type": "plumbline:prosecutor", "model": "haiku"})
    assert "pinned to the sonnet model" in denial(run_pre(payload, adopted))
    payload = tool_payload(adopted, "Agent", {"description": "x", "prompt": "y", "subagent_type": "plumbline:builder", "isolation": "remote"})
    assert "Launch it without `isolation`" in denial(run_pre(payload, adopted))
