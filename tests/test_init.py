"""init: adopt plumbline in a repository, changing two files and committing nothing."""
import tomllib

import plumbline as pl
from helpers import git, write


def config(repo):
    return tomllib.loads((repo / "plumbline.toml").read_text(encoding="utf-8"))


def gitignore_lines(repo):
    return (repo / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_init_creates_plumbline_toml_and_ignores_the_run_directory(run_cli, repo):
    result = run_cli("init", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert config(repo) == {"schema": 1, "pipeline": "default", "graft": {"enabled": False}}
    assert gitignore_lines(repo) == [".plumbline/"]
    assert "wrote plumbline.toml (pipeline 'default', graft off)" in result.stdout
    assert "created .gitignore with .plumbline/" in result.stdout


def test_init_says_what_to_commit_and_commits_nothing(run_cli, repo):
    head = git(repo, "rev-parse", "HEAD")
    result = run_cli("init", cwd=repo)
    assert "Commit plumbline.toml and .gitignore; plumbline commits nothing itself." in result.stdout
    assert git(repo, "rev-parse", "HEAD") == head
    status = git(repo, "status", "--porcelain")
    assert "?? .gitignore" in status and "?? plumbline.toml" in status


def test_the_config_it_writes_validates_and_is_read_as_adopted(run_cli, repo):
    run_cli("init", cwd=repo)
    project = pl.load_project(repo)
    assert (project.adopted, project.errors, project.graft_enabled) == (True, [], False)
    checked = run_cli("validate-pipeline", "--project", repo, cwd=repo)
    assert checked.returncode == 0, checked.stdout


def test_init_appends_to_an_existing_gitignore_keeping_its_content(run_cli, repo):
    write(repo / ".gitignore", "node_modules/\n*.log")  # no trailing newline
    result = run_cli("init", cwd=repo)
    assert gitignore_lines(repo) == ["node_modules/", "*.log", ".plumbline/"]
    assert "appended to .gitignore: .plumbline/" in result.stdout


def test_init_leaves_a_gitignore_that_already_ignores_the_run_directory_alone(run_cli, repo):
    original = "build/\n.plumbline/\ndist/\n"
    write(repo / ".gitignore", original)
    result = run_cli("init", cwd=repo)
    assert (repo / ".gitignore").read_text() == original
    assert ".gitignore already ignores .plumbline/ (unchanged)" in result.stdout
    assert "Commit plumbline.toml;" in result.stdout  # only the file that changed


def test_init_recognises_the_other_ways_of_writing_the_entry(run_cli, repo):
    for entry in (".plumbline", "/.plumbline/", "/.plumbline", "  .plumbline/  "):
        (repo / "plumbline.toml").unlink(missing_ok=True)
        write(repo / ".gitignore", f"a\n{entry}\nb\n")
        run_cli("init", cwd=repo)
        assert (repo / ".gitignore").read_text() == f"a\n{entry}\nb\n", entry


def test_init_is_idempotent_on_gitignore(run_cli, repo):
    run_cli("init", cwd=repo)
    (repo / "plumbline.toml").unlink()  # adopt again from scratch
    second = run_cli("init", cwd=repo)
    assert second.returncode == 0
    assert gitignore_lines(repo) == [".plumbline/"]  # still exactly one entry
    assert ".gitignore already ignores .plumbline/ (unchanged)" in second.stdout


def test_init_refuses_to_overwrite_plumbline_toml_and_changes_nothing(run_cli, repo):
    write(repo / "plumbline.toml", "schema = 1\n# mine\n")
    result = run_cli("init", cwd=repo)
    assert result.returncode == 1
    assert "already exists" in result.stderr and "Nothing was changed" in result.stderr
    assert (repo / "plumbline.toml").read_text() == "schema = 1\n# mine\n"
    assert not (repo / ".gitignore").exists()  # not even the gitignore was touched


def test_init_refuses_even_with_graft(run_cli, repo):
    write(repo / "plumbline.toml", "schema = 1\n")
    assert run_cli("init", "--graft", cwd=repo).returncode == 1
    assert (repo / "plumbline.toml").read_text() == "schema = 1\n"


def test_graft_sets_enabled_to_true(run_cli, repo):
    result = run_cli("init", "--graft", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert config(repo)["graft"] == {"enabled": True}
    assert "graft on" in result.stdout
    assert pl.load_project(repo).graft_enabled is True


def test_init_from_a_subdirectory_writes_at_the_top_level(run_cli, repo):
    result = run_cli("init", cwd=repo / "src")
    assert result.returncode == 0, result.stderr
    assert (repo / "plumbline.toml").is_file()
    assert not (repo / "src" / "plumbline.toml").exists()
    assert (repo / ".gitignore").is_file()


def test_project_flag_selects_the_repository(run_cli, repo, tmp_path):
    result = run_cli("init", "--project", repo, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (repo / "plumbline.toml").is_file()


def test_init_outside_a_git_repository_writes_in_the_directory_it_is_given(run_cli, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("init", cwd=plain, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert (plain / "plumbline.toml").is_file()
    assert gitignore_lines(plain) == [".plumbline/"]


def test_the_template_shows_the_overrides_as_comments_with_precedence_above_the_tables():
    text = pl.config_template(False)
    assert text.index("# precedence") < text.index("[graft]")  # a top-level key must precede every table
    assert "# [[type]]" in text and "# [matrix.voice]" in text
    assert tomllib.loads(text) == {"schema": 1, "pipeline": "default", "graft": {"enabled": False}}


def test_a_repo_can_uncomment_the_template_overrides_and_they_validate(run_cli, repo):
    text = pl.config_template(False)
    for line in ("# precedence", "# [[type]]", "# id = ", "# paths = ", "# [matrix.voice]", "# stages = "):
        text = text.replace(line, line[2:])
    write(repo / "plumbline.toml", text)
    project = pl.load_project(repo)
    assert project.errors == []
    assert project.pipeline["type"][0]["id"] == "voice"
