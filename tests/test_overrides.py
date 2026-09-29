"""Repo overrides: a plumbline.toml merged over the shipped pipeline."""
import json

import plumbline as pl
from helpers import DEFAULT_TOML, commit_all, default_pipeline, numbered, write

VOICE = """\
schema = 1
pipeline = "default"

precedence = ["voice", "code", "config", "tests", "docs"]

[graft]
enabled = false

[[type]]
id = "voice"
paths = ["voice/**"]

[matrix.voice]
stages = ["intake", "reduce"]
note = "Voice and prompt changes go through the repo's own model evals."
"""


def adopt(repo, text=VOICE):
    write(repo / "plumbline.toml", text)
    return repo


def test_a_voice_type_first_in_order_merges_and_validates(repo):
    project = pl.load_project(adopt(repo))
    assert project.adopted
    assert project.errors == []
    merged = project.pipeline
    assert [t["id"] for t in merged["type"]][:2] == ["voice", "docs"]  # repo types come first
    assert merged["precedence"] == ["voice", "code", "config", "tests", "docs"]
    assert merged["matrix"]["voice"]["stages"] == ["intake", "reduce"]
    assert "code" in merged["matrix"]  # the pipeline's own rows stay
    assert project.graft_enabled is False


def test_merging_leaves_the_shipped_pipeline_untouched(repo):
    pl.load_project(adopt(repo))
    assert [t["id"] for t in default_pipeline()["type"]] == ["docs", "tests", "config", "code"]
    assert "voice" not in default_pipeline()["matrix"]


def test_validate_pipeline_project_accepts_the_override(run_cli, repo):
    adopt(repo)
    result = run_cli("validate-pipeline", "--project", repo, cwd=repo)
    assert result.returncode == 0, result.stdout
    assert "valid (0 errors" in result.stdout


def test_a_voice_change_takes_the_voice_row_even_beside_code(repo):
    adopt(repo)
    commit_all(repo, "adopt")
    write(repo / "voice" / "prompt.txt", "Speak plainly.\n")
    write(repo / "src" / "extra.py", numbered(10))
    project = pl.load_project(repo)
    record = pl.classify(repo, project.pipeline, "main")
    assert record["types"] == ["voice", "code", "config"]  # plumbline.toml itself is config
    assert record["row"] == "voice"


def test_the_voice_plan_is_intake_and_reduce_with_the_note(run_cli, repo):
    adopt(repo)
    commit_all(repo, "adopt")
    write(repo / "voice" / "prompt.txt", "Speak plainly.\n")
    result = run_cli("plan", "--run-id", "demo", cwd=repo)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["row"] == "voice"
    assert [s["id"] for s in plan["stages"]] == ["intake", "reduce"]
    assert "model evals" in plan["note"]


def test_an_override_row_replaces_the_pipelines_row(repo):
    text = 'schema = 1\n\n[matrix.docs]\nstages = ["intake", "reduce"]\n'
    project = pl.load_project(adopt(repo, text))
    assert project.errors == []
    assert project.pipeline["matrix"]["docs"] == {"stages": ["intake", "reduce"]}


def test_the_voice_row_draws_no_notes_because_reduce_reads_verify_and_review_optionally(repo):
    project = pl.load_project(adopt(repo))
    assert project.errors == []
    assert [n for n in project.notes if "row 'voice'" in n] == []


def test_an_override_precedence_replaces_the_pipelines(repo):
    text = 'schema = 1\nprecedence = ["docs", "code", "config", "tests"]\n'
    project = pl.load_project(adopt(repo, text))
    assert project.errors == []
    commit_all(repo, "adopt")
    write(repo / "docs" / "guide.md", "hello\n")
    write(repo / "src" / "extra.py", "x = 1\n")
    assert pl.classify(repo, project.pipeline, "main")["row"] == "docs"


def test_the_pipeline_key_may_be_a_repo_relative_path(repo):
    custom = DEFAULT_TOML.read_text().replace('name = "default"', 'name = "house"', 1)
    write(repo / "pipelines" / "house.toml", custom)
    project = pl.load_project(adopt(repo, 'schema = 1\npipeline = "pipelines/house.toml"\n'))
    assert project.errors == []
    assert project.pipeline["name"] == "house"


def test_graft_enabled_is_read_from_the_config(repo):
    project = pl.load_project(adopt(repo, "schema = 1\n\n[graft]\nenabled = true\n"))
    assert project.errors == []
    assert project.graft_enabled is True


def broken(repo, text):
    return pl.load_project(adopt(repo, text)).errors


def test_project_errors_name_the_problem_in_the_override(repo):
    # a new type whose precedence entry was forgotten
    errors = broken(repo, VOICE.replace('precedence = ["voice", "code", "config", "tests", "docs"]\n', ""))
    assert any("precedence is missing type 'voice'" in e for e in errors)

    # a row that names a stage the pipeline does not have
    errors = broken(repo, VOICE.replace('stages = ["intake", "reduce"]', 'stages = ["intake", "ghost"]'))
    assert any("row 'voice': unknown stage 'ghost'" in e for e in errors)

    # a row that reorders the stages
    errors = broken(repo, VOICE.replace('stages = ["intake", "reduce"]', 'stages = ["reduce", "intake"]'))
    assert any("row 'voice': stages must appear in the pipeline's definition order" in e for e in errors)

    # a type without a row
    errors = broken(repo, VOICE.replace("[matrix.voice]", "[matrix.speech]"))
    assert any("type 'voice' has no matrix row" in e for e in errors)
    assert any("matrix row 'speech' does not belong to any type" in e for e in errors)

    # a misspelt table, and a key above the tables that landed inside [graft]
    errors = broken(repo, VOICE.replace("[graft]", "[grfat]"))
    assert any("plumbline.toml: $.grfat: unexpected key" in e for e in errors)
    errors = broken(repo, VOICE.replace('precedence = ["voice", "code", "config", "tests", "docs"]\n', "").replace("enabled = false", 'enabled = false\nprecedence = ["voice", "code", "config", "tests", "docs"]'))
    assert any("plumbline.toml: $.graft.precedence: unexpected key" in e for e in errors)

    # the wrong schema, an unknown pipeline, and TOML that does not parse
    assert any("$.schema: must be 1" in e for e in broken(repo, "schema = 2\n"))
    assert any("pipeline 'nope' is not shipped with plumbline" in e for e in broken(repo, 'schema = 1\npipeline = "nope"\n'))
    assert any("plumbline.toml: invalid TOML" in e for e in broken(repo, "schema = = 1\n"))


def test_a_pipeline_path_may_not_leave_the_repository(repo):
    assert any("points outside the repository" in e for e in broken(repo, 'schema = 1\npipeline = "../elsewhere.toml"\n'))
    assert any("relative to the repository" in e for e in broken(repo, 'schema = 1\npipeline = "/etc/elsewhere.toml"\n'))


def test_a_pipeline_file_that_is_missing_is_reported(repo):
    assert any("file not found" in e for e in broken(repo, 'schema = 1\npipeline = "pipelines/none.toml"\n'))


def test_validate_pipeline_project_reports_errors_in_the_override(run_cli, repo):
    adopt(repo, VOICE.replace('stages = ["intake", "reduce"]', 'stages = ["intake", "ghost"]'))
    result = run_cli("validate-pipeline", "--project", repo, cwd=repo)
    assert result.returncode == 1
    assert "error: row 'voice': unknown stage 'ghost'" in result.stdout


def test_validate_pipeline_project_without_a_config_validates_the_pipeline_alone(run_cli, repo):
    result = run_cli("validate-pipeline", "--project", repo, cwd=repo)
    assert result.returncode == 0
    assert "note: no plumbline.toml" in result.stdout


def test_validate_pipeline_project_from_a_subdirectory_finds_the_top_level(run_cli, repo):
    adopt(repo)
    result = run_cli("validate-pipeline", "--project", repo / "src", cwd=repo)
    assert result.returncode == 0
    assert "for " + str(repo.resolve()) in result.stdout
