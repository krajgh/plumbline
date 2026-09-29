"""classify: the diff from the merge base to the working tree, as a change_class record."""
import json
import os

import pytest

import plumbline as pl
from helpers import commit_all, git, numbered, write


def classify(repo, base="main"):
    project = pl.load_project(repo)
    assert project.errors == []
    record = pl.classify(repo, project.pipeline, base)
    assert pl.check_record("change_class", record) == []  # every record must validate
    return record


def test_docs_only_change_takes_the_docs_row(repo):
    write(repo / "docs" / "guide.md", numbered(5))
    record = classify(repo)
    assert record["types"] == ["docs"]
    assert record["row"] == "docs"  # a flat row carries no size


def test_a_30_line_code_change_is_code_s(repo):
    write(repo / "src" / "new_module.py", numbered(30))
    record = classify(repo)
    assert (record["lines"], record["size"], record["row"]) == (30, "S", "code.S")


def test_a_100_line_code_change_is_code_m(repo):
    write(repo / "src" / "new_module.py", numbered(100))
    record = classify(repo)
    assert (record["lines"], record["size"], record["row"]) == (100, "M", "code.M")


def test_size_boundaries(repo):
    sizes = pl.load_project(repo).pipeline["sizes"]
    assert [pl.size_for(n, sizes) for n in (0, 50, 51, 400, 401)] == ["S", "S", "M", "M", "L"]


def test_a_401_line_change_is_code_l(repo):
    write(repo / "src" / "big.py", numbered(401))
    assert classify(repo)["row"] == "code.L"


def test_added_and_removed_lines_both_count(repo):
    write(repo / "src" / "app.py", numbered(20, "new"))  # replaces two lines with twenty
    record = classify(repo)
    [entry] = [f for f in record["files"] if f["path"] == "src/app.py"]
    assert (entry["added"], entry["removed"]) == (20, 2)
    assert record["lines"] == 22


def test_code_plus_docs_follows_precedence_and_code_wins(repo):
    write(repo / "src" / "new_module.py", numbered(10))
    write(repo / "docs" / "guide.md", numbered(10))
    record = classify(repo)
    assert record["types"] == ["code", "docs"]  # in precedence order
    assert record["row"] == "code.S"


@pytest.mark.parametrize(
    "path,expected_row",
    [
        ("config/settings.yaml", "config"),
        ("tests/test_thing.py", "tests"),
        ("notes/todo.txt", "docs"),
        ("web/app.test.ts", "tests"),
        ("Dockerfile", "config"),
    ],
)
def test_each_file_type_takes_its_own_row(repo, path, expected_row):
    write(repo / path, numbered(3))
    assert classify(repo)["row"] == expected_row


def test_config_beats_tests_beats_docs(repo):
    write(repo / "tests" / "test_a.py", numbered(3))
    write(repo / "docs" / "a.md", numbered(3))
    assert classify(repo)["types"] == ["tests", "docs"]
    write(repo / "ci" / "settings.yaml", numbered(3))
    assert classify(repo)["types"] == ["config", "tests", "docs"]
    assert classify(repo)["row"] == "config"


def test_generated_paths_are_excluded_from_the_line_count(repo):
    write(repo / "uv.lock", numbered(500))
    write(repo / "graft" / "cards" / "app.md", numbered(300))
    write(repo / "src" / "new_module.py", numbered(10))
    record = classify(repo)
    assert record["lines"] == 10
    assert record["row"] == "code.S"
    generated = {f["path"] for f in record["files"] if f["generated"]}
    assert generated == {"uv.lock", "graft/cards/app.md"}
    assert next(f for f in record["files"] if f["path"] == "uv.lock")["added"] == 500  # still listed


def test_an_untracked_file_that_is_not_ignored_counts(repo):
    write(repo / "src" / "untracked.py", numbered(60))
    record = classify(repo)
    assert record["lines"] == 60
    assert record["row"] == "code.M"
    assert any("untracked" in n for n in record["notes"])
    assert git(repo, "status", "--porcelain").startswith("??")  # and it is still untracked


def test_an_ignored_file_does_not_count(repo):
    git(repo, "checkout", "-q", "main")
    write(repo / ".gitignore", "scratch/\n*.log\n")
    commit_all(repo, "ignore, on the base")
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--ff-only", "main")
    write(repo / "scratch" / "big.py", numbered(500))
    write(repo / "run.log", numbered(500))
    write(repo / "src" / "new_module.py", numbered(5))
    record = classify(repo)
    assert record["lines"] == 5
    assert [f["path"] for f in record["files"]] == ["src/new_module.py"]


def test_the_run_directory_is_never_part_of_the_change(repo):
    write(repo / ".plumbline" / "runs" / "r1" / "intake.json", "{}\n")
    write(repo / "src" / "new_module.py", numbered(5))
    assert [f["path"] for f in classify(repo)["files"]] == ["src/new_module.py"]


def test_committed_and_uncommitted_changes_are_both_included(repo):
    write(repo / "src" / "committed.py", numbered(20))
    commit_all(repo, "committed")
    write(repo / "src" / "uncommitted.py", numbered(15))
    git(repo, "add", "src/uncommitted.py")  # staged, not committed
    write(repo / "src" / "app.py", "def main():\n    return 2\n")  # modified, not staged
    record = classify(repo)
    assert {f["path"] for f in record["files"]} == {"src/committed.py", "src/uncommitted.py", "src/app.py"}
    assert record["lines"] == 20 + 15 + 2


def test_changes_on_the_base_branch_itself_are_measured_against_the_working_tree(repo):
    git(repo, "checkout", "-q", "main")
    write(repo / "src" / "wip.py", numbered(7))
    record = classify(repo)
    assert record["head"] == record["merge_base"]
    assert record["lines"] == 7


def test_the_merge_base_not_the_base_tip_is_the_start_of_the_diff(repo):
    write(repo / "src" / "feature_work.py", numbered(10))
    commit_all(repo, "feature work")
    git(repo, "checkout", "-q", "main")
    write(repo / "src" / "main_work.py", numbered(300))
    commit_all(repo, "main moved on")
    git(repo, "checkout", "-q", "feature")
    record = classify(repo)
    assert [f["path"] for f in record["files"]] == ["src/feature_work.py"]
    assert record["merge_base"] != git(repo, "rev-parse", "main").strip()


def test_a_committed_symlink_is_flagged(repo):
    os.symlink("app.py", repo / "src" / "link.py")
    commit_all(repo, "add a symlink")
    record = classify(repo)
    assert record["symlinks"] == ["src/link.py"]
    entry = next(f for f in record["files"] if f["path"] == "src/link.py")
    assert entry["symlink"] is True
    assert next(f for f in record["files"] if f["path"] == "src/link.py")["added"] == 1


def test_an_untracked_symlink_is_flagged_and_never_followed(repo, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text(numbered(1000), encoding="utf-8")
    os.symlink(secret, repo / "src" / "peek.txt")
    record = classify(repo)
    assert record["symlinks"] == ["src/peek.txt"]
    assert record["lines"] == 1  # the link itself, not the 1000 lines it points at


def test_a_symlink_that_was_deleted_is_not_flagged(repo):
    git(repo, "checkout", "-q", "main")
    os.symlink("app.py", repo / "src" / "link.py")
    commit_all(repo, "main has a symlink")
    git(repo, "checkout", "-q", "-b", "cleanup")
    git(repo, "rm", "-q", "src/link.py")
    record = classify(repo)
    assert record["symlinks"] == []
    entry = next(f for f in record["files"] if f["path"] == "src/link.py")
    assert (entry["removed"], entry["symlink"]) == (1, False)


def test_binary_files_count_as_zero_lines_with_a_note(repo):
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary\x00")
    write(repo / "src" / "new_module.py", numbered(4))
    record = classify(repo)
    assert record["lines"] == 4
    assert any("binary files count as 0 lines: assets/logo.png" in n for n in record["notes"])


def test_a_file_without_a_trailing_newline_counts_its_last_line(repo):
    write(repo / "src" / "tail.py", "one\ntwo\nthree")
    assert classify(repo)["lines"] == 3


def test_no_changes_is_an_error_not_an_empty_record(repo):
    project = pl.load_project(repo)
    with pytest.raises(pl.PlumblineError, match="nothing to classify"):
        pl.classify(repo, project.pipeline, "main")


def test_a_repository_with_no_commits_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    with pytest.raises(pl.PlumblineError, match="no commits"):
        pl.classify(empty, pl.load_project(empty).pipeline, "main")


def test_an_unknown_base_is_an_error(repo):
    project = pl.load_project(repo)
    with pytest.raises(pl.PlumblineError, match="not a commit"):
        pl.classify(repo, project.pipeline, "no-such-branch")
    with pytest.raises(pl.PlumblineError, match="not a commit"):
        pl.classify(repo, project.pipeline, "--upload-pack=x")


def test_records_are_deterministic_and_sorted(repo):
    write(repo / "src" / "b.py", numbered(2))
    write(repo / "src" / "a.py", numbered(2))
    assert [f["path"] for f in classify(repo)["files"]] == ["src/a.py", "src/b.py"]


# --- the default base: the remote's default branch, then origin/main, then main


@pytest.fixture
def remote(repo, tmp_path):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    return bare


def test_default_base_falls_back_to_main_without_a_remote(repo):
    ref, note = pl.default_base(repo)
    assert ref == "main"
    assert "fell back to 'main'" in note


def test_default_base_falls_back_to_origin_main_when_the_remote_head_is_unknown(repo, remote):
    ref, note = pl.default_base(repo)
    assert ref == "origin/main"
    assert "fell back to 'origin/main'" in note


def test_default_base_prefers_the_remotes_default_branch(repo, remote):
    git(repo, "branch", "develop", "main")
    git(repo, "push", "-q", "origin", "develop")
    git(repo, "remote", "set-head", "origin", "develop")
    assert pl.default_base(repo) == ("origin/develop", None)


def test_classify_without_base_uses_the_default_and_says_so(run_cli, repo):
    write(repo / "src" / "new_module.py", numbered(3))
    result = run_cli("classify", cwd=repo)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["base"] == "main"
    assert any("fell back to 'main'" in n for n in record["notes"])


def test_no_base_at_all_is_an_error(tmp_path):
    odd = tmp_path / "odd"
    odd.mkdir()
    git(odd, "init", "-q", "-b", "trunk")
    write(odd / "a.txt", "x\n")
    commit_all(odd, "first")
    with pytest.raises(pl.PlumblineError, match="no base found"):
        pl.default_base(odd)


# --- the command


def test_classify_out_writes_a_valid_record(run_cli, repo):
    write(repo / "src" / "new_module.py", numbered(100))
    out = repo / ".plumbline" / "runs" / "r1" / "intake.json"
    result = run_cli("classify", "--base", "main", "--out", out, cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "row code.M" in result.stdout
    assert pl.check_record("change_class", json.loads(out.read_text())) == []
    checked = run_cli("check-record", "change_class", out, cwd=repo)
    assert checked.returncode == 0


def test_classify_prints_json_without_out(run_cli, repo):
    write(repo / "src" / "new_module.py", numbered(30))
    result = run_cli("classify", "--base", "main", cwd=repo)
    assert result.returncode == 0
    assert json.loads(result.stdout)["row"] == "code.S"


def test_classify_outside_a_git_repository_exits_2(run_cli, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("classify", cwd=plain, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert result.returncode == 2
    assert "not inside a git repository" in result.stderr


def test_classify_with_an_invalid_config_exits_2_and_lists_the_errors(run_cli, repo):
    write(repo / "plumbline.toml", "schema = 2\n")
    write(repo / "src" / "new_module.py", numbered(3))
    result = run_cli("classify", cwd=repo)
    assert result.returncode == 2
    assert "$.schema: must be 1" in result.stderr


def test_project_flag_selects_the_repository(run_cli, repo, tmp_path):
    write(repo / "src" / "new_module.py", numbered(3))
    result = run_cli("classify", "--project", repo, "--base", "main", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["row"] == "code.S"
