"""CLI hygiene: no abbreviated options (OV-ABBR), git without optional locks and a real index left as it was (C-19), nested repositories treated the
same in `classify` and in the hash (C-20), and a project that `coverage` cannot do without (C-25)."""
import hashlib
import itertools
import json
import os
import subprocess

import pytest

import plumbline as pl
from helpers import commit_all, git, numbered, write
from rundata import CONTROLLED, RUN, adopt, adopt_base, begin, genuine_pass, put, review_record, verify_now, write_docs_run

REASON = "The pipeline cannot run offline; a one-line typo fix."


# --- OV-ABBR: an option is spelled out


def subparsers():
    parser = pl.build_parser()
    action = next(a for a in parser._actions if getattr(a, "choices", None))
    return parser, action.choices


def test_every_parser_of_the_cli_refuses_abbreviations_of_its_options():
    parser, commands = subparsers()
    assert parser.allow_abbrev is False
    assert len(commands) == 14 and all(sub.allow_abbrev is False for sub in commands.values())


def test_an_abbreviated_reason_writes_no_override(run_cli, repo):  # OV-ABBR: the reviewer's reproduction
    adopt(repo)
    for spelled in ("--rea", "--reas", "--r", "--reaso"):
        result = run_cli("override", spelled, REASON, cwd=repo)
        assert result.returncode == 2, spelled
        assert "unrecognized arguments" in result.stderr or "required: --reason" in result.stderr
    assert not (repo / ".plumbline" / "pass").exists()
    assert run_cli("override", "--reason", REASON, cwd=repo).returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        ("status", "--proj", "."), ("status", "--ru", "r1"), ("gate", "r1", "verify", "--proj", "."), ("plan", "--int", "feature"), ("plan", "--ro", "code.S"),
        ("classify", "--ba", "main"), ("classify", "--inte", "fix"), ("check-diff", "--ru", "r1"), ("merge-review", "r1", "review", "--rou", "1"),
        ("override", "--run", "r1", "--rea", REASON), ("validate-pipeline", "--proj", "."), ("init", "--gra"), ("render", "x.json", "--ty", "spec"), ("open", "--al"), ("open", "--js"),
    ],
)
def test_no_option_of_any_command_can_be_shortened(run_cli, repo, command):
    result = run_cli(*command, cwd=repo)
    assert result.returncode == 2, result.stderr
    assert "unrecognized arguments" in result.stderr or "the following arguments are required" in result.stderr, result.stderr


def test_the_full_spelling_still_works_where_the_abbreviation_was_refused(run_cli, repo):
    adopt(repo)
    assert run_cli("status", "--project", ".", cwd=repo).returncode == 0
    assert run_cli("validate-pipeline", "--project", ".", cwd=repo).returncode == 0


# --- C-19: git runs without optional locks, and a measured change leaves the real index as it was


def test_every_git_command_of_the_cli_passes_no_optional_locks(repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pl.subprocess, "run", spy)
    pl._git(repo, "rev-parse", "HEAD")
    pl.git_toplevel(repo)
    with pl.index_copy(repo) as index:
        pl._git_index(repo, index, "ls-files")
    assert len(seen) == 4 and all(argv[:3] == ["git", "--no-optional-locks", "-C"] for argv in seen)


def index_state(repo):
    path = repo / ".git" / "index"
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


_clock = itertools.count(2_000_000_000, 7)


def stat_dirty(repo):
    """Make the index's stat data stale for some files without changing what they hold: git would refresh the index."""
    for name in ("README.md", "src/app.py"):
        stamp = next(_clock)
        os.utime(repo / name, (stamp, stamp))


def index_change_by(repo, *git_args):
    """Whether one git command rewrites the index, from an index that is up to date and whose stat data is then made stale."""
    git(repo, "status", "--porcelain")
    stat_dirty(repo)
    before = index_state(repo)
    git(repo, *git_args)
    return index_state(repo) != before


def test_a_plain_git_diff_rewrites_the_index_and_no_optional_locks_keeps_git_status_from_it(repo):
    """The control: the reviewer's finding stands for git itself (git 2.43: `--no-optional-locks` does not reach `git diff`), so the tests
    below would fail were plumbline to run a plain `git diff` on the real index. That is why its commands work on a copy of the index."""
    if not index_change_by(repo, "diff", "--raw"):
        pytest.skip("this git does not rewrite the index on `git diff`: the tests below have nothing to protect")
    assert index_change_by(repo, "status", "--porcelain") is True
    assert index_change_by(repo, "--no-optional-locks", "status", "--porcelain") is False


def test_the_commands_leave_the_real_index_byte_for_byte_as_it_was(run_cli, repo):  # C-19: exp_index_touch
    adopt_base(repo, commands={"test": CONTROLLED})
    write(repo / "src" / "new.py", numbered(5))
    assert run_cli("plan", "--intent", "review-only", "--row", "code.S", "--run-id", RUN, cwd=repo).returncode == 0
    git(repo, "status", "--porcelain")  # bring the index up to date, then make it stale without changing content
    stat_dirty(repo)
    before = index_state(repo)
    commands = [
        ("check-diff", "--run", RUN), ("check-diff", "--base", "main"), ("classify", "--base", "main"), ("plan", "--base", "main", "--row", "code.S"),
        ("status",), ("tokens", RUN), ("gate", RUN, "verify"),
    ]
    for command in commands:
        stat_dirty(repo)
        run_cli(*command, cwd=repo)
        assert index_state(repo) == before, command


def test_pass_and_merge_review_leave_the_real_index_as_it_was_too(run_cli, repo):
    adopt(repo, commands={"test": CONTROLLED})
    write_docs_run(repo)
    git(repo, "status", "--porcelain")
    stat_dirty(repo)
    before = index_state(repo)
    assert run_cli("pass", RUN, cwd=repo).returncode == 0
    assert index_state(repo) == before


def test_measuring_the_change_stages_nothing_and_writes_no_file_in_the_repository(run_cli, repo):
    adopt_base(repo)
    write(repo / "src" / "new.py", numbered(5))
    run_cli("plan", "--intent", "review-only", "--row", "code.S", "--run-id", RUN, cwd=repo)
    status = git(repo, "status", "--porcelain=v1")
    run_cli("check-diff", "--run", RUN, cwd=repo)
    assert git(repo, "status", "--porcelain=v1") == status and git(repo, "diff", "--cached", "--name-only") == ""


# --- C-20: a nested repository is no part of the change, in classify and in the hash


def nested(repo, name="vendor/lib", commit=False):
    path = repo / name
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write(path / "file.txt", "inside\n")
    if commit:
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "nested")
    return path


def test_an_untracked_nested_repository_with_no_commit_does_not_stop_check_diff_or_the_hash(run_cli, repo):  # C-20: exp-nested
    write(repo / "src" / "new.py", numbered(3))
    before = pl.change_hash(repo, git(repo, "merge-base", "main", "HEAD").strip())
    nested(repo)
    result = run_cli("check-diff", "--base", "main", cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["diff_sha256"] == before  # the nested repository is not in the change


def test_an_untracked_nested_repository_with_a_commit_is_no_gitlink_in_the_hash(repo):  # C-20
    merge_base = git(repo, "merge-base", "main", "HEAD").strip()
    before = pl.change_hash(repo, merge_base)
    nested(repo, commit=True)
    assert pl.change_hash(repo, merge_base) == before


def test_classify_and_the_hash_agree_that_a_nested_repository_is_skipped(repo):
    write(repo / "src" / "new.py", numbered(3))
    nested(repo)
    record = pl.classify(repo, pl.load_project(repo).pipeline, "main")
    assert [f["path"] for f in record["files"]] == ["src/new.py"]
    assert "skipped the untracked directory vendor/lib/ (a nested repository?)" in record["notes"]
    assert pl.nested_repositories(repo) == ["vendor/lib/"]


def test_merge_review_and_gate_work_beside_a_nested_repository(run_cli, repo):
    adopt(repo)
    begin(repo, "docs")
    nested(repo)
    verify_now(repo)
    put(repo, "review", review_record(diff=pl.change_hash(repo, git(repo, "merge-base", "main", "HEAD").strip())))
    assert run_cli("check-diff", "--run", RUN, cwd=repo).returncode == 0


def test_a_nested_repository_the_repository_ignores_is_not_listed(repo):
    write(repo / ".gitignore", "vendor/\n")
    commit_all(repo, "ignore vendor")
    nested(repo)
    assert pl.nested_repositories(repo) == []


def test_a_nested_repository_with_an_awkward_name_is_skipped_literally(repo):
    write(repo / "src" / "new.py", numbered(3))
    merge_base = git(repo, "merge-base", "main", "HEAD").strip()
    before = pl.change_hash(repo, merge_base)
    nested(repo, "we ird[dir]*/lib")
    assert pl.change_hash(repo, merge_base) == before


def test_a_git_failure_of_the_hash_is_reported_not_raised_as_a_traceback(run_cli, repo):
    result = run_cli("check-diff", "--base", "main", cwd=repo, GIT_DIR=str(repo / "nowhere"))
    assert result.returncode == 2 and "Traceback" not in result.stderr


# --- C-25: coverage cannot do without the project


def test_coverage_needs_the_project(repo):
    adopt(repo)
    genuine_pass(repo)
    with pytest.raises(TypeError):
        pl.coverage(repo, pl.head_sha(repo))
    assert pl.coverage(repo, pl.head_sha(repo), pl.load_project(repo))[0] == "pass"
