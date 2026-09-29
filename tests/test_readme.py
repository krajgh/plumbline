"""The README says what the plugin has: every command, agent, skill, record and setting is in it."""
import re

import plumbline as pl
from helpers import REPO

README = (REPO / "README.md").read_text(encoding="utf-8")


def test_every_command_of_the_cli_is_in_the_commands_table():
    parser = pl.build_parser()
    commands = next(a for a in parser._actions if getattr(a, "choices", None)).choices
    assert "check-diff" in commands and "plan" in commands
    for name in commands:
        assert re.search(rf"^\| `{re.escape(name)}[ `]", README, re.M), name


def test_every_agent_and_skill_is_named():
    for role in pl.AGENT_ROLES:
        assert f"`plumbline:{role}`" in README, role
    for skill in ("run", "override", "status", "init", "subagent-discipline"):
        assert f"/plumbline:{skill}" in README, skill


def test_every_record_type_is_in_the_records_table():
    for name in pl.record_types():
        assert re.search(rf"^\| `{name}` \|", README, re.M), name


def test_every_intent_and_gate_is_named():
    for intent in pl.intent_ids(pl.load_toml(pl.PIPELINE_DIR / "default.toml")):
        assert f"`{intent}`" in README, intent
    for gate in pl.KNOWN_GATES:
        assert gate in README, gate


def test_the_one_time_setups_are_documented():
    assert "PONYTAIL_SUBAGENT_MATCHER" in README and "^plumbline:(planner|test-writer|builder)$" in README
    assert "SubagentStart" in README and "hooks/ponytail-subagent.js" in README
    assert "[commands]" in README and "pre_tool_use.sh" in README and "subagent_stop.sh" in README


def test_the_status_line_names_the_phase_the_manifest_version_belongs_to():
    assert "Phase 2b of 5" in README
