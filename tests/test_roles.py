"""The roles table of the pipeline (what each agent may write and run), and [commands] in plumbline.toml."""
import copy

import pytest

import plumbline as pl
from helpers import DEFAULT_TOML, default_pipeline, write

# The policies of the spec, written out here as the oracle.
SPEC_ROLES = {
    "planner": {"writes": ["record"], "commands": ["git-read", "search", "graft"]},
    "test-writer": {"writes": ["record", "tests", "stubs"], "commands": ["test", "git-read", "search"]},
    "builder": {"writes": ["record", "code"], "commands": []},
    "verifier": {"writes": ["record"], "commands": ["test", "lint", "typecheck", "build", "git-read", "search", "plumbline-check"]},
    "prosecutor": {"writes": ["record"], "commands": ["git-read", "search", "graft"]},
    "defender": {"writes": ["record"], "commands": ["git-read", "search"]},
    "detective": {"writes": ["record"], "commands": ["git-read", "search", "graft"]},
    "canary": {"writes": ["record"], "commands": ["git-read", "search"]},
}


def check(mutate):
    pipeline = default_pipeline()
    mutate(pipeline)
    return pl.validate_pipeline(pipeline)


def test_the_default_pipeline_carries_the_specs_role_policies_and_validates():
    pipeline = default_pipeline()
    assert pipeline["roles"] == SPEC_ROLES
    errors, _notes = pl.validate_pipeline(pipeline)
    assert errors == []


def test_the_known_write_targets_and_command_classes_are_the_specs():
    assert pl.KNOWN_WRITE_TARGETS == ("record", "tests", "stubs", "code")
    assert pl.KNOWN_COMMAND_CLASSES == ("test", "lint", "typecheck", "build", "git-read", "search", "plumbline-check", "graft")


def test_the_test_writer_alone_has_the_stubs_write_target_and_the_policy_comment_documents_it():
    roles = default_pipeline()["roles"]
    assert [name for name, policy in roles.items() if "stubs" in policy["writes"]] == ["test-writer"]
    text = DEFAULT_TOML.read_text(encoding="utf-8")
    comment = text[text.index("# Role policies"):text.index("[roles.planner]")]
    assert "stubs = the active run's stubs directory, .plumbline/runs/<run-id>/stubs/" in comment and "outside the change" in comment


def test_the_roles_are_the_agents_that_end_with_a_record():
    assert set(default_pipeline()["roles"]) == set(pl.AGENT_RECORDS)


BAD_ROLES = [
    ("unknown-write-target", lambda d: d["roles"]["builder"]["writes"].append("everything"), "roles.builder: unknown write target 'everything' (known: record, tests, stubs, code)"),
    ("stubs-named-twice", lambda d: d["roles"]["test-writer"]["writes"].append("stubs"), "roles.test-writer: writes names 'stubs' more than once"),
    ("unknown-command", lambda d: d["roles"]["verifier"]["commands"].append("curl"), "roles.verifier: unknown command 'curl'"),
    ("unknown-role", lambda d: d["roles"].update(wizard={"writes": ["record"], "commands": []}), "roles.wizard: unknown role"),
    ("missing-role", lambda d: d["roles"].pop("detective"), "there is no policy for the agent 'detective'"),
    ("record-missing-from-writes", lambda d: d["roles"]["planner"].update(writes=["tests"]), "roles.planner: writes must include 'record'"),
    ("writes-not-a-list", lambda d: d["roles"]["planner"].update(writes="record"), "roles.planner: writes must be a list of strings"),
    ("commands-not-strings", lambda d: d["roles"]["planner"].update(commands=[1, 2]), "roles.planner: commands must be a list of strings"),
    ("commands-missing", lambda d: d["roles"]["planner"].pop("commands"), "roles.planner: commands must be a list of strings"),
    ("extra-key", lambda d: d["roles"]["planner"].update(reads=["all"]), "roles.planner: unexpected key 'reads'"),
    ("duplicate-command", lambda d: d["roles"]["defender"]["commands"].append("search"), "roles.defender: commands names 'search' more than once"),
    ("policy-not-a-table", lambda d: d["roles"].update(defender=["search"]), "roles.defender: must be a table"),
    ("a-write-target-as-a-command", lambda d: d["roles"]["defender"]["commands"].append("tests"), "roles.defender: unknown command 'tests'"),
    ("a-command-as-a-write-target", lambda d: d["roles"]["defender"]["writes"].append("git-read"), "roles.defender: unknown write target 'git-read'"),
]


@pytest.mark.parametrize("mutate,expected", [(m, e) for _, m, e in BAD_ROLES], ids=[i for i, _, _ in BAD_ROLES])
def test_a_bad_roles_table_is_an_error_naming_the_role(mutate, expected):
    errors, _ = check(mutate)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_an_unknown_write_target_and_an_unknown_command_are_both_reported():
    def mutate(d):
        d["roles"]["builder"]["writes"].append("elsewhere")
        d["roles"]["builder"]["commands"].append("rm")

    errors, _ = check(mutate)
    assert any("unknown write target 'elsewhere'" in e for e in errors) and any("unknown command 'rm'" in e for e in errors)


def test_a_role_without_commands_is_valid_it_just_has_no_bash():
    errors, _ = check(lambda d: d["roles"]["builder"].update(commands=[]))
    assert errors == []


def test_the_roles_table_is_optional_and_the_shipped_policies_stand_in_for_a_pipeline_without_one(repo):
    custom = default_pipeline()
    del custom["roles"]
    assert pl.validate_pipeline(custom)[0] == []
    text = DEFAULT_TOML.read_text(encoding="utf-8")
    write(repo / "old.toml", text[: text.index("# Role policies")])
    write(repo / "plumbline.toml", 'schema = 1\npipeline = "old.toml"\n')
    project = pl.load_project(repo)
    assert project.errors == [] and "roles" not in project.pipeline
    assert project.roles == SPEC_ROLES


def test_roles_are_checked_through_the_cli_too(run_cli, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(DEFAULT_TOML.read_text(encoding="utf-8").replace('commands = ["git-read", "search"]', 'commands = ["git-read", "sudo"]'), encoding="utf-8")
    result = run_cli("validate-pipeline", bad, cwd=tmp_path)
    assert result.returncode == 1
    assert "error: roles.defender: unknown command 'sudo'" in result.stdout


def test_a_repo_cannot_replace_the_role_policies_from_plumbline_toml(repo):
    write(repo / "plumbline.toml", 'schema = 1\n\n[roles.builder]\nwrites = ["tests"]\ncommands = ["search"]\n')
    project = pl.load_project(repo)
    assert any("$.roles: unexpected key" in e for e in project.errors)


# --- [commands] in plumbline.toml


def load(repo, text):
    write(repo / "plumbline.toml", text)
    return pl.load_project(repo)


def test_commands_accepts_a_prefix_or_a_list_of_prefixes(repo):
    project = load(repo, 'schema = 1\n\n[commands]\ntest = "python3 -m pytest"\nlint = ["ruff check", "ruff format --check"]\n')
    assert project.errors == []
    assert project.commands == {"test": ["python3 -m pytest"], "lint": ["ruff check", "ruff format --check"]}


def test_commands_are_empty_when_the_repo_sets_none(repo):
    assert load(repo, "schema = 1\n").commands == {}


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("[commands]\ntest = 5\n", "$.commands.test: expected string or array, got integer"),
        ('[commands]\ntest = ""\n', "$.commands.test: must be at least 1 characters long"),
        ("[commands]\ntest = []\n", "$.commands.test: needs at least 1 items"),
        ('[commands]\ntest = ["ok", ""]\n', "$.commands.test[1]: must be at least 1 characters long"),
        ('[commands]\ndeploy = "make deploy"\n', "$.commands.deploy: unexpected key"),
    ],
)
def test_a_bad_commands_table_is_reported_with_its_path(repo, text, fragment):
    project = load(repo, "schema = 1\n\n" + text)
    assert any(fragment in e for e in project.errors), project.errors


def test_the_config_template_shows_the_commands_as_comments(run_cli, repo):
    run_cli("init", cwd=repo)
    text = (repo / "plumbline.toml").read_text(encoding="utf-8")
    assert "# [commands]" in text and '# test = "python3 -m pytest"' in text
    assert pl.load_project(repo).commands == {}  # only comments: nothing is set until the repo uncomments them
    write(repo / "plumbline.toml", text.replace("# [commands]", "[commands]").replace('# test = ', "test = "))
    project = pl.load_project(repo)
    assert project.errors == [] and project.commands == {"test": ["python3 -m pytest"]}


def test_a_merged_pipeline_keeps_the_roles_of_the_shipped_one(repo):
    write(repo / "plumbline.toml", 'schema = 1\n\n[matrix.docs]\nstages = ["intake", "reduce"]\n')
    project = pl.load_project(repo)
    assert project.errors == [] and project.roles == SPEC_ROLES
    assert project.pipeline["roles"] == copy.deepcopy(SPEC_ROLES)
