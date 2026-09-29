"""The seven agents: pinned models, tools and turn limits, and prompts that state what to do."""
import re

import pytest

import plumbline as pl
from helpers import REPO
from test_manifests import frontmatter

AGENTS = REPO / "agents"

# (agent, model, tools) as the spec pins them: the builder has no Bash, the verifier has Bash and reads and no Edit
EXPECTED = {
    "planner": ("sonnet", ["Read", "Grep", "Glob", "Bash", "Write"]),
    "test-writer": ("sonnet", ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]),
    "builder": ("sonnet", ["Read", "Edit", "Write", "Grep", "Glob"]),
    "verifier": ("haiku", ["Read", "Grep", "Glob", "Bash", "Write"]),
    "prosecutor": ("sonnet", ["Read", "Grep", "Glob", "Bash", "Write"]),
    "defender": ("haiku", ["Read", "Grep", "Glob", "Bash", "Write"]),
    "detective": ("sonnet", ["Read", "Grep", "Glob", "Bash", "Write"]),
}
NAMES = sorted(EXPECTED)


def agent(name):
    return frontmatter(AGENTS / f"{name}.md")


def test_there_is_one_agent_for_each_record_an_agent_ends_with():
    assert sorted(p.stem for p in AGENTS.glob("*.md")) == NAMES
    assert set(pl.AGENT_ROLES) == set(NAMES) == set(pl.AGENT_RECORDS)


@pytest.mark.parametrize("name", NAMES)
def test_each_frontmatter_has_name_description_model_tools_and_max_turns(name):
    fields, body = agent(name)
    assert set(fields) == {"name", "description", "model", "tools", "maxTurns"}
    assert fields["name"] == name  # the agent type is `plumbline:<name>`
    assert fields["description"].strip() and body.strip()
    assert ": " not in fields["description"], "a colon and space in a plain YAML scalar breaks the frontmatter"
    assert not fields["description"].startswith(("'", '"', "[", "{", "&", "*", "!", "|", ">", "%", "@", "`", "#"))
    assert re.fullmatch(r"[1-9][0-9]*", fields["maxTurns"]) and 5 <= int(fields["maxTurns"]) <= 60  # a stage that needs more is split


@pytest.mark.parametrize("name", NAMES)
def test_the_model_and_the_tools_are_pinned_as_the_spec_says(name):
    fields, _ = agent(name)
    model, tools = EXPECTED[name]
    assert fields["model"] == model
    assert [t.strip() for t in fields["tools"].split(",")] == tools


def test_planner_test_writer_builder_prosecutor_and_detective_run_on_sonnet_and_verifier_and_defender_on_haiku():
    assert {n for n in NAMES if agent(n)[0]["model"] == "sonnet"} == {"planner", "test-writer", "builder", "prosecutor", "detective"}
    assert {n for n in NAMES if agent(n)[0]["model"] == "haiku"} == {"verifier", "defender"}


def test_the_builder_has_no_bash_and_so_cannot_run_or_read_tests_through_the_shell():
    tools = [t.strip() for t in agent("builder")[0]["tools"].split(",")]
    assert "Bash" not in tools and "PowerShell" not in tools
    assert pl.default_roles()["builder"]["commands"] == []  # the policy says so too


def test_the_verifier_has_bash_and_reads_and_no_edit():
    tools = [t.strip() for t in agent("verifier")[0]["tools"].split(",")]
    assert {"Bash", "Read"} <= set(tools) and "Edit" not in tools


@pytest.mark.parametrize("name", NAMES)
def test_every_prompt_tells_the_agent_to_end_with_a_record_line(name):
    _, body = agent(name)
    assert re.search(r"^RECORD: <the path of your record>$", body, re.M)
    assert "End your final message with this line, last" in body
    assert body.rstrip().endswith("```")  # the record line is the last thing the prompt asks for


@pytest.mark.parametrize("name", NAMES)
def test_every_prompt_names_the_schema_of_its_record_inside_the_plugin(name):
    _, body = agent(name)
    record = pl.AGENT_RECORDS[name]
    assert f"${{CLAUDE_PLUGIN_ROOT}}/schemas/{record}.json" in body
    assert (REPO / "schemas" / f"{record}.json").is_file()


@pytest.mark.parametrize("name", [n for n in NAMES if n != "builder"])
def test_an_agent_with_bash_checks_its_record_with_check_record_before_it_finishes(name):
    _, body = agent(name)
    record = pl.AGENT_RECORDS[name]
    assert f'python3 "${{CLAUDE_PLUGIN_ROOT}}/scripts/plumbline.py" check-record {record} <the path of your record>' in body


def test_the_builder_has_no_bash_so_it_reads_its_record_against_the_schema_instead():
    _, body = agent("builder")
    assert "check-record" not in body
    assert "You have no Bash to check the record with, so read it once more against the schema" in body


@pytest.mark.parametrize("name", ["planner", "test-writer", "builder"])
def test_the_agents_that_make_the_change_name_ponytails_ladder(name):
    assert "ponytail's ladder" in agent(name)[1]


@pytest.mark.parametrize("name", ["verifier", "prosecutor", "defender", "detective"])
def test_the_agents_that_only_check_do_not_carry_ponytails_ladder(name):
    assert "ponytail" not in agent(name)[1]


def test_the_planner_and_the_builder_list_the_rungs_of_the_ladder_in_order():
    for name in ("planner", "builder"):
        body = agent(name)[1]
        rungs = ["need to exist", "already in this codebase", "standard library", "natively", "installed dependency", "one line", "minimum that works"]
        positions = [body.index(rung) for rung in rungs]
        assert positions == sorted(positions), name


SEVERITY_CLAUSES = (
    "breaks a stated acceptance criterion, loses or corrupts data, exposes a secret or personal data, or breaks the main path for most users",
    "wrong behaviour on a realistic path, with limited reach or a workaround",
    "an edge case, a leak without near-term impact, or an inconsistency",
)


def test_the_prosecutor_carries_the_severity_rubric_the_schema_carries():
    body = agent("prosecutor")[1]
    rubric = pl.load_schema("review_record")["properties"]["findings"]["items"]["properties"]["severity"]["description"]
    for level, clause in zip(("BLOCKING", "MAJOR", "MINOR"), SEVERITY_CLAUSES):
        assert clause in body and re.search(rf"- {level} ", body)
        assert f"{level}: {clause}" in rubric  # the two copies say the same


def test_the_prosecutor_asks_for_every_field_of_a_finding_and_one_lens_per_run():
    body = agent("prosecutor")[1]
    finding = pl.load_schema("findings_record")["properties"]["findings"]["items"]
    for key in finding["required"]:
        assert f"`{key}`" in body, key
    assert "through one lens" in body and "unique across the round" in body and "a quote from the code" in body


def test_the_prosecutor_names_all_six_lenses():
    body = agent("prosecutor")[1]
    for lens in pl.KNOWN_LENSES:
        assert f"- {lens}:" in body


def test_the_defender_refutes_only_with_a_quote():
    body = agent("defender")[1]
    assert "a refutation counts only with a quote of the code" in body
    assert "`refuted`" in body and "`conceded`" in body
    assert "Refute with a quote, or concede" in body


def test_the_detective_runs_only_when_no_blocker_stands():
    body = agent("detective")[1]
    assert "You run only once no blocker stands" in body
    assert "no blocker stands" in agent("detective")[0]["description"]


def test_the_verifier_maps_failing_tests_to_criteria_without_assertion_text():
    body = agent("verifier")[1]
    assert "Leave the assertion message out" in body
    assert "error_type" in body and "diff_sha256" in body and "check-diff --run" in body


def test_the_test_writer_runs_the_tests_against_a_stub_and_records_the_check():
    body = agent("test-writer")[1]
    assert "stub" in body and "stub_check" in body and "all_failed_on_assertions" in body
    assert "a fix" in body  # in a fix the tests run against today's code


def test_the_planner_writes_at_size_l_a_split_proposal():
    assert "split_proposal" in agent("planner")[1]


# Rules are stated as what to do: what a hook enforces is not repeated as a prohibition, and what only judgement can apply is
# still phrased as an instruction. A prohibition word in a prompt is a sign that a rule is left to prose.
PROHIBITION = re.compile(r"\b(never|don't|do not|must not|cannot|can't|won't|shouldn't|forbidden|prohibited|not allowed)\b", re.I)


@pytest.mark.parametrize("name", NAMES)
def test_the_prompts_state_rules_as_what_to_do(name):
    body = agent(name)[1]
    found = [m.group(0) for line in body.splitlines() if not line.startswith("```") for m in PROHIBITION.finditer(line)]
    assert found == [], f"{name}: {found}"


def test_the_prompts_carry_no_private_names_or_paths():
    for name in NAMES:
        text = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
        assert "/ho" + "me/" not in text


def test_the_agent_prompts_and_the_pipeline_agree_on_who_writes_which_record():
    stages = pl.load_toml(pl.PIPELINE_DIR / "default.toml")["stage"]
    by_role = {s["role"]: s["record"] for s in stages if s.get("kind", "agent") == "agent" and s["role"] != "main"}
    for role, record in by_role.items():
        assert pl.AGENT_RECORDS[role] == record
