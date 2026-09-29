"""The reviewed change is the pushed change: diff_sha256, `check-diff`, `pass` refusing a change edited after its review,
and a push gate that does not take a pass file at its word."""
import hashlib
import json
import os
import re

import pytest

import plumbline as pl
from helpers import commit_all, git, numbered, write
from hookdata import bash_payload, denial
from rundata import (
    FAILING, RUN, adopt, begin, build_note_record, change_of, genuine_pass, intake_record, ledger, put, put_part, read, review_record,
    run_entry, run_path, spec_record, verify_record, write_docs_run, write_test_file, written_tests_record,
)
from samples import sample

HOME_PATH = "/ho" + "me/someone/project/file.txt"  # built from pieces, so that no file of this repository holds one
KEY = "sk-" + "ant-" + "a" * 24


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def merge_base(repo):
    return git(repo, "merge-base", "main", "HEAD").strip()


def now(repo):
    """The hash of the change as the files are now, uncommitted work and untracked files included: what `check-diff` prints."""
    return pl.change_hash(repo, merge_base(repo))


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


# --- the hash


def test_a_change_hashes_the_same_before_and_after_it_is_committed(repo):
    write(repo / "src" / "app.py", "def main():\n    return 2\n")  # modified
    write(repo / "src" / "extra.py", "x = 1\n")  # untracked
    (repo / "README.md").unlink()  # deleted
    before = now(repo)
    assert re.fullmatch(r"[0-9a-f]{64}", before)
    commit_all(repo, "the change")
    assert pl.change_hash(repo, merge_base(repo), pl.head_tree(repo)) == before
    assert now(repo) == before  # and the files as they are still hash to it


def test_a_change_hashes_differently_when_any_file_of_it_differs(repo):
    write(repo / "src" / "app.py", "def main():\n    return 2\n")
    first = now(repo)
    write(repo / "src" / "app.py", "def main():\n    return 3\n")
    second = now(repo)
    write(repo / "src" / "new.py", "x = 1\n")
    third = now(repo)
    (repo / "src" / "new.py").unlink()
    assert len({first, second, third}) == 3 and now(repo) == second


def test_untracked_files_are_part_of_the_change_and_ignored_ones_are_not(repo):
    base = now(repo)
    write(repo / "src" / "new.py", "x = 1\n")
    with_new = now(repo)
    assert with_new != base
    write(repo / ".gitignore", "build/\n")
    commit_all(repo, "ignore build")
    ignoring = now(repo)
    write(repo / "build" / "out.bin", "artifact\n")
    assert now(repo) == ignoring  # an ignored file is not part of any change


def test_the_run_directory_is_never_part_of_the_change_whether_or_not_it_is_ignored(repo):
    write(repo / "src" / "app.py", "def main():\n    return 2\n")
    base = now(repo)
    write(repo / ".plumbline" / "runs" / "r1" / "verify.json", "{}")  # not ignored here: git sees it as untracked
    assert now(repo) == base
    commit_all(repo, "commit everything, run files included")  # a repository that committed .plumbline/ by mistake
    write(repo / ".plumbline" / "runs" / "r1" / "verify.json", '{"changed": true}')
    assert now(repo) == pl.change_hash(repo, merge_base(repo), pl.head_tree(repo)) == base


def test_an_edit_of_the_same_size_made_right_after_the_last_index_write_is_still_in_the_hash(repo):
    # git compares timestamps to the second, so a file of the same size rewritten within the second of the commit looks
    # unchanged to `git add` from a copy of the index whose own timestamp is a second later. The hash reads every tracked file.
    import time

    write(repo / "src" / "app.py", "def main():\n    return 2\n")  # the same size as the committed file
    write(repo / ".gitignore", "*.lock\n")
    write(repo / "uv.lock", "v1\n")
    git(repo, "add", "-f", ".gitignore", "uv.lock")
    commit_all(repo, "a tracked file that .gitignore names")
    write(repo / "src" / "app.py", "def main():\n    return 3\n")  # rewritten straight after the commit
    write(repo / "uv.lock", "v2\n")  # tracked, ignored, and the same size
    time.sleep(1.2)  # the copy of the index is made in a later second
    before = now(repo)
    commit_all(repo, "as it was")
    assert before == pl.change_hash(repo, merge_base(repo), pl.head_tree(repo))
    assert git(repo, "show", "HEAD:src/app.py").strip().endswith("return 3") and git(repo, "show", "HEAD:uv.lock") == "v2\n"


def test_a_tracked_file_that_gitignore_names_stays_part_of_the_change(repo):
    write(repo / ".gitignore", "*.lock\n")
    write(repo / "uv.lock", "v1\n")
    git(repo, "add", "-f", ".gitignore", "uv.lock")
    commit_all(repo, "tracked although ignored")
    first = now(repo)
    write(repo / "uv.lock", "a longer v2\n")
    assert now(repo) != first  # an ignored file is not part of a change unless git tracks it


def test_the_hash_stages_nothing_and_changes_no_file(repo):
    write(repo / "src" / "new.py", "x = 1\n")
    write(repo / "src" / "app.py", "def main():\n    return 2\n")
    status = git(repo, "status", "--porcelain=v1")
    index = (repo / ".git" / "index").read_bytes()
    now(repo)
    assert git(repo, "status", "--porcelain=v1") == status and (repo / ".git" / "index").read_bytes() == index
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_a_mode_change_and_a_deletion_change_the_hash_and_the_hash_of_nothing_is_the_hash_of_the_empty_diff(repo):
    assert now(repo) == hashlib.sha256(b"").hexdigest()
    os.chmod(repo / "src" / "app.py", 0o755)
    mode = now(repo)
    assert mode != hashlib.sha256(b"").hexdigest()
    os.chmod(repo / "src" / "app.py", 0o644)
    (repo / "src" / "app.py").unlink()
    assert now(repo) not in (mode, hashlib.sha256(b"").hexdigest())


def test_the_hash_is_of_the_change_from_the_given_merge_base_not_from_head(repo):
    write(repo / "src" / "one.py", "one = 1\n")
    first = commit_all(repo, "one")
    write(repo / "src" / "two.py", "two = 2\n")
    commit_all(repo, "two")
    from_main = pl.change_hash(repo, merge_base(repo), pl.head_tree(repo))
    from_first = pl.change_hash(repo, first, pl.head_tree(repo))
    assert from_main != from_first


def test_an_unknown_merge_base_is_an_error_not_a_hash(repo):
    with pytest.raises(pl.PlumblineError, match="cannot hash the change from 89abcdef0123"):
        pl.change_hash(repo, "89abcdef0123456789abcdef0123456789abcdef")


# --- check-diff


def check_diff(run_cli, repo, *extra):
    result = run_cli("check-diff", "--base", "main", *extra, cwd=repo)
    return result, (json.loads(result.stdout) if result.stdout.strip().startswith("{") else None)


def test_check_diff_prints_the_checks_and_the_hash_of_a_clean_change_and_exits_0(run_cli, repo):
    write(repo / "src" / "new.py", "x = 1\n")
    result, data = check_diff(run_cli, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(data) == ["merge_base", "diff_sha256", "checks", "problems"]
    assert data["checks"] == {"symlinks": True, "abs_paths": True, "secrets": True} and data["problems"] == []
    assert data["merge_base"] == merge_base(repo) and data["diff_sha256"] == now(repo)


def test_check_diff_finds_a_symlink_a_home_path_and_a_secret_and_exits_1(run_cli, repo):
    os.symlink("README.md", repo / "alias")
    write(repo / "notes.md", f"see {HOME_PATH}\nkey {KEY}\n")
    result, data = check_diff(run_cli, repo)
    assert result.returncode == 1
    assert data["checks"] == {"symlinks": False, "abs_paths": False, "secrets": False}
    assert "adds a symlink: alias" in data["problems"]
    assert "adds an absolute home path: notes.md:1" in data["problems"]
    assert "adds a key-shaped secret: notes.md:2 (the value is not shown)" in data["problems"]
    assert KEY not in result.stdout  # the value is never echoed


def test_check_diff_reports_which_check_failed_and_leaves_the_others_true(run_cli, repo):
    write(repo / "notes.md", f"{HOME_PATH}\n")
    _, data = check_diff(run_cli, repo)
    assert data["checks"] == {"symlinks": True, "abs_paths": False, "secrets": True}


def test_check_diff_covers_the_whole_change_committed_staged_and_untracked(run_cli, repo):
    write(repo / "committed.md", f"{HOME_PATH}\n")
    commit_all(repo, "earlier on this branch")
    write(repo / "staged.md", f"{KEY}\n")
    git(repo, "add", "staged.md")
    os.symlink("README.md", repo / "untracked-link")
    _, data = check_diff(run_cli, repo)
    assert {"adds an absolute home path: committed.md:1", "adds a key-shaped secret: staged.md:1 (the value is not shown)", "adds a symlink: untracked-link"} <= set(data["problems"])


def test_check_diff_leaves_out_what_is_removed_and_the_run_directory(run_cli, repo):
    write(repo / "src" / "old.py", f"{HOME_PATH}\nkeep\n")
    commit_all(repo, "old")
    git(repo, "checkout", "-q", "-b", "next")  # the base is now this branch's start
    write(repo / "src" / "old.py", "keep\n")  # removes the path
    write(repo / ".plumbline" / "runs" / "r1" / "ledger.jsonl", f'{{"transcript": "{HOME_PATH}"}}\n')
    result = run_cli("check-diff", "--base", "feature", cwd=repo)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["problems"] == []


def test_check_diff_of_a_run_measures_from_where_the_run_began(run_cli, adopted):
    first = commit_all(adopted, "an earlier commit") if write(adopted / "early.md", "early\n") else None
    write(adopted / "late.md", "late\n")
    commit_all(adopted, "a later commit")
    put(adopted, "intake", intake_record("docs", adopted, merge_base=first))
    result = run_cli("check-diff", "--run", RUN, cwd=adopted)
    data = json.loads(result.stdout)
    assert data["merge_base"] == first and data["diff_sha256"] == pl.change_hash(adopted, first)
    assert data["diff_sha256"] != now(adopted)  # the default measures from the merge base with main


def test_check_diff_of_a_run_that_does_not_exist_could_not_run(run_cli, adopted):
    result = run_cli("check-diff", "--run", "nope", cwd=adopted)
    assert result.returncode == 2 and "there is no run 'nope'" in result.stderr


def test_check_diff_works_where_plumbline_is_not_adopted_and_needs_a_repository(run_cli, repo, tmp_path):
    write(repo / "src" / "new.py", "x = 1\n")
    assert run_cli("check-diff", "--base", "main", cwd=repo).returncode == 0
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("check-diff", cwd=plain, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert result.returncode == 2 and "not inside a git repository" in result.stderr


def test_check_diff_with_an_unknown_base_could_not_run(run_cli, repo):
    write(repo / "src" / "new.py", "x = 1\n")
    result = run_cli("check-diff", "--base", "no-such-branch", cwd=repo)
    assert result.returncode == 2 and "not a commit" in result.stderr


# --- the records carry the hash


def test_merge_review_stamps_the_review_record_with_the_hash_of_the_change_it_reviewed(run_cli, adopted):
    put(adopted, "intake", intake_record("code.S", adopted))
    write(adopted / "src" / "app.py", "def main():\n    return 2\n")  # uncommitted: part of the change all the same
    for lens in ("correctness", "tests"):
        put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []})  # each part carries the hash of the change it saw
    for k in (1, 2, 3):
        put_part(adopted, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []})
    expected = pl.change_hash(adopted, merge_base(adopted))
    assert run_cli("merge-review", RUN, "review", cwd=adopted).returncode == 0
    record = read(adopted, "review")
    assert record["diff_sha256"] == expected and pl.check_record("review_record", record) == []


def test_a_verify_or_review_record_without_the_hash_is_invalid():
    for name in ("verify_record", "review_record"):
        record = sample(name)
        del record["diff_sha256"]
        assert pl.check_record(name, record) == ["$.diff_sha256: missing required key"]
        record["diff_sha256"] = "abc"
        assert any("does not match ^[0-9a-f]{64}$" in e for e in pl.check_record(name, record))
        record["diff_sha256"] = "A" * 64
        assert pl.check_record(name, record)  # lower-case hex only


# --- pass refuses a change edited after it was reviewed


@pytest.fixture
def ready(adopted):
    write_docs_run(adopted)
    return adopted


def test_pass_accepts_the_change_that_was_verified_and_reviewed(run_cli, ready):
    assert run_cli("pass", RUN, cwd=ready).returncode == 0


def test_a_change_verified_before_it_is_committed_passes_once_it_is_committed_as_it_was(run_cli, adopted):
    write(adopted / "src" / "app.py", "def main():\n    return 2\n")
    write(adopted / "src" / "helper.py", "def helper():\n    return 1\n")  # untracked while it is verified
    diff = now(adopted)  # what the verifier's check-diff printed
    put(adopted, "intake", intake_record("code.S", adopted, intent="review-only"))  # a change that exists, reviewed as it is
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff)
    put(adopted, "review", review_record(diff=diff))
    commit_all(adopted, "the change, as it was verified")
    result = run_cli("pass", RUN, cwd=adopted)
    assert result.returncode == 0, result.stdout


def test_pass_refuses_when_the_change_was_edited_after_verify_and_review(run_cli, ready):
    write(ready / "README.md", "# demo\nedited after the review\n")  # an edit after the records were made
    commit_all(ready, "edited after review")
    result = run_cli("pass", RUN, cwd=ready)
    assert result.returncode == 1
    for stage in ("verify", "review"):
        assert re.search(rf"error: stage '{stage}': its record covers the change [0-9a-f]{{12}}, but the change of HEAD from the merge base hashes to [0-9a-f]{{12}}", result.stdout)
    assert "the change was edited after that record was made, so run the pipeline again from verify" in result.stdout
    assert "refused for run 'r1'" in result.stdout and "nothing was written" in result.stdout
    assert not (ready / ".plumbline" / "pass").exists()


def test_pass_refuses_when_only_the_review_is_stale(run_cli, ready):
    put(ready, "review", review_record(diff=hashlib.sha256(b"an earlier change").hexdigest()))
    result = run_cli("pass", RUN, cwd=ready)
    assert result.returncode == 1 and "stage 'review': its record covers the change" in result.stdout and "stage 'verify'" not in result.stdout


def test_pass_refuses_when_a_file_is_added_after_review_and_committed(run_cli, ready):
    write(ready / "notes.md", "not reviewed\n")
    commit_all(ready, "one more file")
    result = run_cli("pass", RUN, cwd=ready)
    assert result.returncode == 1 and "its record covers the change" in result.stdout


def test_rerunning_verify_and_review_on_the_edited_change_lets_it_pass(run_cli, ready):
    write(ready / "README.md", "# demo\nedited\n")
    commit_all(ready, "edited")
    assert run_cli("pass", RUN, cwd=ready).returncode == 1
    diff = change_of(ready)
    put(ready, "verify", verify_record(diff=diff))
    run_entry(ready, "verify", diff)  # the commands ran again, on the edited change
    put(ready, "review", review_record(diff=diff))
    assert run_cli("pass", RUN, cwd=ready).returncode == 0


def test_the_stale_change_is_reported_only_once_every_gate_passes(run_cli, ready):
    write(ready / "README.md", "# demo\nedited\n")
    commit_all(ready, "edited")
    put(ready, "verify", verify_record(green=False, diff=hashlib.sha256(b"x").hexdigest()))
    result = run_cli("pass", RUN, cwd=ready)
    assert "the verify record is not green" in result.stdout and "its record covers the change" not in result.stdout


def test_the_review_of_the_tests_is_not_compared_with_the_change_of_head(run_cli, adopted):
    write_test_file(adopted)
    commit_all(adopted, "the tests")
    diff = change_of(adopted)
    put(adopted, "intake", intake_record("code.M", adopted))
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record())
    run_entry(adopted, "tests", None, exit_code=1, cmd=FAILING)
    put(adopted, "test-review", review_record(target="tests", diff=hashlib.sha256(b"the change before the build").hexdigest()))
    put(adopted, "build", build_note_record())
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff)
    put(adopted, "review", review_record(diff=diff))
    result = run_cli("pass", RUN, cwd=adopted)
    assert result.returncode == 0, result.stdout


def test_a_run_without_verify_or_review_needs_no_hash(run_cli, adopted):
    write(adopted / "plumbline.toml", 'schema = 1\n\n[matrix.docs]\nstages = ["intake", "reduce"]\n\n[matrix.config]\nstages = ["intake", "reduce"]\n')
    commit_all(adopted, "docs and config go straight to reduce")
    begin(adopted, "docs")
    assert run_cli("pass", RUN, cwd=adopted).returncode == 0


def test_a_merge_base_that_is_gone_is_reported_not_crashed_on(run_cli, ready):
    put(ready, "intake", intake_record("docs", ready, merge_base="89abcdef0123456789abcdef0123456789abcdef"))
    result = run_cli("pass", RUN, cwd=ready)
    assert result.returncode == 1
    assert "the change could not be measured again: base '89abcdef0123456789abcdef0123456789abcdef' is not a commit in this repository" in result.stdout


# --- the ledger pins what pass wrote


def test_pass_enters_in_the_ledger_the_hash_of_every_record_the_pass_record_lists_and_of_itself(run_cli, ready):
    assert run_cli("pass", RUN, cwd=ready).returncode == 0
    [entry] = [e for e in ledger(ready) if e["kind"] == "pass"]
    pass_file = ready / ".plumbline" / "pass" / f"{head(ready)}.json"
    assert entry["commit"] == head(ready) and entry["pass_record"] == f".plumbline/pass/{head(ready)}.json"
    assert entry["pass_sha256"] == pl.file_sha256(pass_file)
    listed = [s["record"] for s in read(ready, "reduce")["stages"]]
    assert sorted(entry["records"]) == sorted(listed)
    assert entry["records"][".plumbline/runs/r1/reduce.json"] == pl.file_sha256(run_path(ready, RUN, "reduce.json"))  # the run's copy, too
    assert all(entry["records"][path] == pl.file_sha256(ready / path) for path in listed)


# --- the push gate does not take the pass file at its word


def push(run_pre, repo):
    return denial(run_pre(bash_payload(repo, "git push origin feature"), repo))


@pytest.fixture
def passed(adopted):
    genuine_pass(adopted)
    return adopted


def test_a_genuine_pass_lets_the_push_through(run_pre, passed):
    assert push(run_pre, passed) is None


def test_a_pass_file_written_by_hand_is_not_enough(run_pre, adopted):
    record = sample("pass_record")
    record.update(commit=head(adopted), run_id="made-up")
    pl.write_json_atomic(adopted / ".plumbline" / "pass" / f"{head(adopted)}.json", record)
    reason = push(run_pre, adopted)
    assert reason and "does not hold up: its run cannot be read" in reason and "there is no run 'made-up'" in reason


def test_a_pass_file_copied_next_to_a_real_run_is_not_enough_without_the_ledger_entry(run_pre, ready):
    record = {**sample("pass_record"), "commit": head(ready), "run_id": RUN, "row": "docs", "stages": []}
    pl.write_json_atomic(ready / ".plumbline" / "pass" / f"{head(ready)}.json", record)
    reason = push(run_pre, ready)
    assert reason and "the ledger of run r1 does not show `pass` writing it" in reason


def test_a_record_altered_after_the_pass_stops_the_push(run_pre, passed):
    path = run_path(passed, RUN, "verify.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["commands"][0]["summary"] = "13 passed (edited afterwards)"  # still a valid record, and still green
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    reason = push(run_pre, passed)
    assert reason and "records altered after the pass: .plumbline/runs/r1/verify.json" in reason


def test_a_record_without_a_gate_altered_after_the_pass_stops_the_push_too(run_pre, passed):
    intake = read(passed, "intake")
    intake["notes"] = ["written afterwards"]
    put(passed, "intake", intake)
    assert "records altered after the pass: .plumbline/runs/r1/intake.json" in push(run_pre, passed)


def test_a_record_that_no_longer_passes_its_gate_stops_the_push_and_the_gate_is_named(run_pre, passed):
    put(passed, "verify", verify_record(green=False, diff=change_of(passed)))
    reason = push(run_pre, passed)
    assert reason and "records altered after the pass" in reason and "all_gates_passed: stage 'verify'" in reason


def test_a_deleted_record_stops_the_push(run_pre, passed):
    run_path(passed, RUN, "review.json").unlink()
    reason = push(run_pre, passed)
    assert reason and "records altered after the pass: .plumbline/runs/r1/review.json" in reason


def test_the_pass_record_itself_altered_after_it_was_written_stops_the_push(run_pre, passed):
    pass_file = passed / ".plumbline" / "pass" / f"{head(passed)}.json"
    record = json.loads(pass_file.read_text(encoding="utf-8"))
    record["notes"] = ["nothing to see"]
    pass_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    assert "the pass record was altered after `pass` wrote it" in push(run_pre, passed)


def test_a_pass_record_that_names_another_run_is_not_believed(run_pre, passed):
    write_docs_run(passed, "r2")  # another finished run, never passed
    pass_file = passed / ".plumbline" / "pass" / f"{head(passed)}.json"
    record = json.loads(pass_file.read_text(encoding="utf-8"))
    record["run_id"] = "r2"
    pass_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    reason = push(run_pre, passed)
    assert reason and "the ledger of run r2 does not show `pass` writing it" in reason


def test_a_run_that_was_removed_stops_the_push(run_pre, passed):
    import shutil

    shutil.rmtree(run_path(passed, RUN))
    assert "does not hold up: its run cannot be read" in push(run_pre, passed)


def test_an_override_record_still_covers_its_commit_without_any_run(run_pre, adopted):
    record = sample("override_record")
    record["commit"] = head(adopted)
    pl.write_json_atomic(adopted / ".plumbline" / "pass" / f"{head(adopted)}.override.json", record)
    assert push(run_pre, adopted) is None


def test_a_pass_that_does_not_hold_falls_back_to_an_override_when_there_is_one(run_pre, passed):
    run_path(passed, RUN, "review.json").unlink()
    assert push(run_pre, passed)
    record = sample("override_record")
    record["commit"] = head(passed)
    pl.write_json_atomic(passed / ".plumbline" / "pass" / f"{head(passed)}.override.json", record)
    assert push(run_pre, passed) is None


def test_a_second_pass_for_the_same_commit_pins_its_own_run(run_pre, run_cli, passed):
    write_docs_run(passed, "r2")
    assert run_cli("pass", "r2", cwd=passed).returncode == 0
    assert push(run_pre, passed) is None
    run_path(passed, "r1", "verify.json").unlink()  # the first run's records no longer matter: the pass record names r2
    assert push(run_pre, passed) is None
    run_path(passed, "r2", "verify.json").unlink()
    assert push(run_pre, passed)


def test_status_says_when_the_pass_does_not_hold_up(run_cli, passed):
    ok = run_cli("status", cwd=passed).stdout
    assert f"HEAD {head(passed)[:7]}: covered by a pass record (run r1)" in ok
    run_path(passed, RUN, "review.json").unlink()
    out = run_cli("status", cwd=passed).stdout
    assert f"HEAD {head(passed)[:7]}: NOT covered" in out and "does not hold up: records altered after the pass" in out


def test_the_coverage_helper_needs_the_project_so_that_no_caller_trusts_the_pass_file_alone(passed):  # C-25
    run_path(passed, RUN, "review.json").unlink()
    with pytest.raises(TypeError):
        pl.coverage(passed, head(passed))
    assert pl.coverage(passed, head(passed), pl.load_project(passed))[0] is None


def test_a_git_push_in_another_repository_is_judged_by_that_repositorys_pass(run_pre, passed, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    payload = bash_payload(other, f"git -C {passed} push")
    assert denial(run_pre(payload, other, GIT_CEILING_DIRECTORIES=str(tmp_path))) is None
    run_path(passed, RUN, "review.json").unlink()
    assert denial(run_pre(payload, other, GIT_CEILING_DIRECTORIES=str(tmp_path)))


def test_a_cd_into_a_directory_that_does_not_exist_leaves_the_push_where_it_was_gated(run_pre, adopted):
    # `cd nowhere; git push` pushes from the repository: the cd fails, and the push runs anyway
    assert push_command(run_pre, adopted, "cd nowhere; git push")
    assert push_command(run_pre, adopted, "cd nowhere/deeper && git push")
    assert push_command(run_pre, adopted, "git -C nowhere push")


def push_command(run_pre, repo, command):
    return denial(run_pre(bash_payload(repo, command), repo))
