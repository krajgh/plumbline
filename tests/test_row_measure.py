"""The row is an estimate until it is measured: `pass` and `check-diff --run` measure the change again, from the run's merge base to the
files as they are, and refuse a run whose row lacks a stage the measured row selects (PF-ROW, C-01), or a row that ends before reduce (C-02).
A refactor leaves the tests as they are (C-14)."""
import json

import pytest

import plumbline as pl
from helpers import commit_all, git, numbered, write
from rundata import (
    CONTROLLED, RUN, adopt_base, begin, build_note_record, change_of, intake_record, now_hash, put, read, review_record, run_entry, spec_record,
    verify_now, verify_record,
)


@pytest.fixture
def adopted(repo):
    adopt_base(repo, commands={"test": CONTROLLED})  # the change is only what follows
    return repo


def plan(run_cli, repo, intent="feature", row="code.S", *extra):
    result = run_cli("plan", "--intent", intent, "--row", row, "--run-id", RUN, *extra, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def finish_review_only(run_cli, repo):
    """verify and review of a review-only run, through the CLI, with the change as it is."""
    verify_now(repo)
    assert run_cli("gate", RUN, "verify", cwd=repo).returncode == 0
    put(repo, "review", review_record(diff=now_hash(repo)))
    assert run_cli("gate", RUN, "review", cwd=repo).returncode == 0


def pass_of(run_cli, repo):
    return run_cli("pass", RUN, cwd=repo)


# --- C-01, PF-ROW: a declared row that the change outgrew


def test_a_change_of_1800_lines_declared_code_s_measures_as_l_and_gets_no_pass(run_cli, adopted):  # C-01: the reviewer's reproduction
    plan(run_cli, adopted, "review-only", "code.S")
    write(adopted / "src" / "big.py", "".join(f"def f{i}():\n    return {i}\n\n" for i in range(600)))  # about 1,800 changed lines
    commit_all(adopted, "the big change")
    verify_now(adopted)
    assert run_cli("gate", RUN, "verify", cwd=adopted).returncode == 0
    put(adopted, "review", review_record(diff=now_hash(adopted)))
    result = pass_of(run_cli, adopted)
    assert result.returncode == 1
    assert "this row ends before reduce: split the change (it measures as code.L, 1800 changed lines, and nothing is built at that size)" in result.stdout
    assert "The files that contribute most: src/big.py (1800 lines)." in result.stdout
    assert "the change" in result.stdout and not (adopted / ".plumbline" / "pass").exists()


def test_a_source_change_declared_as_docs_is_refused_with_the_stages_it_lacks(run_cli, adopted):  # C-01, PF-ROW: exp_rowdecl2
    plan(run_cli, adopted, "feature", "docs")
    write(adopted / "src" / "app.py", "def main():\n    return 'a code change, declared as docs'\n")
    commit_all(adopted, "code, declared as docs")
    result = pass_of(run_cli, adopted)
    assert result.returncode == 1
    assert "the change measures as code.S, which selects plan, tests, build; run 'r1' (row docs) has no such stages: start a new run for this row (`plan --intent feature --row code.S`)" in result.stdout


def test_a_change_that_measures_as_m_needs_the_spec_review_and_the_test_review_a_code_s_run_lacks(run_cli, adopted):  # C-01
    plan(run_cli, adopted, "feature", "code.S")
    write(adopted / "src" / "new_module.py", numbered(100))
    commit_all(adopted, "100 lines")
    result = pass_of(run_cli, adopted)
    assert result.returncode == 1
    assert "the change measures as code.M, which selects spec-review, test-review; run 'r1' (row code.S) has no such stages: start a new run for this row (`plan --intent feature --row code.M`)" in result.stdout


def test_the_row_is_measured_against_the_intent_so_a_review_only_run_needs_only_its_own_stages(run_cli, adopted):
    plan(run_cli, adopted, "review-only", "code.M")
    write(adopted / "src" / "new_module.py", numbered(100))
    commit_all(adopted, "100 lines")
    finish_review_only(run_cli, adopted)
    result = pass_of(run_cli, adopted)
    assert result.returncode == 0, result.stdout  # code.M without plan, tests, test-review and build is what a review-only run has


def test_a_declared_row_larger_than_the_measured_one_is_no_problem(run_cli, adopted):
    plan(run_cli, adopted, "review-only", "code.M")
    write(adopted / "src" / "new_module.py", numbered(10))  # measures as code.S
    commit_all(adopted, "10 lines")
    finish_review_only(run_cli, adopted)
    result = pass_of(run_cli, adopted)
    assert result.returncode == 0, result.stdout
    assert "declared row code.M, measured row code.S" in read(adopted, "reduce")["notes"]


def test_the_declared_and_the_measured_row_are_in_the_pass_notes(run_cli, adopted):
    plan(run_cli, adopted, "review-only", "code.S")
    write(adopted / "src" / "new_module.py", numbered(10))
    commit_all(adopted, "10 lines")
    finish_review_only(run_cli, adopted)
    assert pass_of(run_cli, adopted).returncode == 0
    assert read(adopted, "reduce")["notes"][0] == "declared row code.S, measured row code.S"


def test_a_change_is_measured_from_the_runs_merge_base_not_from_the_default_base(run_cli, adopted):
    write(adopted / "src" / "early.py", numbered(300))
    early = commit_all(adopted, "an earlier change, before the run began")
    write(adopted / "src" / "small.py", numbered(5))
    commit_all(adopted, "the small change of the run")
    begin(adopted, "code.S", merge_base=early)  # the run began after the early change
    project = pl.load_project(adopted)
    measured, problems = pl.measure_row(project, pl.load_run(project, RUN))
    assert problems == [] and measured["row"] == "code.S" and measured["lines"] == 5
    assert pl.classify(adopted, project.pipeline, "main")["row"] == "code.M"  # from the default base, the early lines count too


def test_pass_says_when_the_change_could_not_be_measured_again(run_cli, adopted):
    begin(adopted, "docs", merge_base="89abcdef0123456789abcdef0123456789abcdef")
    result = pass_of(run_cli, adopted)
    assert result.returncode == 1
    assert "the change could not be measured again: base '89abcdef0123456789abcdef0123456789abcdef' is not a commit in this repository" in result.stdout


def test_nothing_changed_means_nothing_to_measure_and_the_pass_says_so(run_cli, adopted):
    begin(adopted, "docs")  # the docs row: intake, verify, review, reduce
    diff = change_of(adopted)
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff)
    put(adopted, "review", review_record(diff=diff))
    result = pass_of(run_cli, adopted)
    assert result.returncode == 0, result.stdout
    assert "declared row docs, measured row none (nothing has changed)" in read(adopted, "reduce")["notes"]


# --- C-02: a row that ends before reduce gets no pass


def test_a_run_of_a_row_without_reduce_gets_no_pass(run_cli, adopted):  # C-02: exp_L
    write(adopted / "src" / "big.py", numbered(500))
    plan(run_cli, adopted, "feature", "code.L")
    put(adopted, "plan", spec_record() | {"split_proposal": ["one change", "another change"]})
    assert run_cli("gate", RUN, "plan", cwd=adopted).returncode == 0
    commit_all(adopted, "the planner's split, and the big change committed all the same")
    result = pass_of(run_cli, adopted)
    assert result.returncode == 1
    assert "this row ends before reduce: split the change (row code.L has no reduce stage, so there is nothing to pass at this size)" in result.stdout
    assert not (adopted / ".plumbline" / "pass").exists()


def test_a_row_of_the_repository_that_ends_before_reduce_gets_no_pass_either(run_cli, repo):
    adopt_base(repo)
    write(repo / "plumbline.toml", (repo / "plumbline.toml").read_text() + '\n[matrix.docs]\nstages = ["intake"]\n')
    commit_all(repo, "docs stop at intake")
    write(repo / "docs" / "guide.md", numbered(3))
    commit_all(repo, "docs")
    begin(repo, "docs")
    result = pass_of(run_cli, repo)
    assert result.returncode == 1 and "this row ends before reduce: split the change (row docs has no reduce stage" in result.stdout


# --- check-diff --run reports the row, and a refactor's tests


def check_diff(run_cli, repo):
    result = run_cli("check-diff", "--run", RUN, cwd=repo)
    return result, json.loads(result.stdout)


def test_check_diff_of_a_run_reports_the_declared_and_the_measured_row(run_cli, adopted):
    plan(run_cli, adopted, "feature", "code.S")
    write(adopted / "src" / "new_module.py", numbered(10))
    result, data = check_diff(run_cli, adopted)
    assert result.returncode == 0, result.stdout
    assert data["row"] == {"declared": "code.S", "measured": "code.S", "missing_stages": []}
    assert list(data) == ["merge_base", "diff_sha256", "checks", "problems", "row"]


def test_check_diff_of_a_run_names_the_stages_a_larger_measured_row_selects_and_exits_1(run_cli, adopted):  # C-01
    plan(run_cli, adopted, "feature", "code.S")
    write(adopted / "src" / "new_module.py", numbered(100))
    result, data = check_diff(run_cli, adopted)
    assert result.returncode == 1
    assert data["row"] == {"declared": "code.S", "measured": "code.M", "missing_stages": ["spec-review", "test-review"]}
    assert any("the change measures as code.M, which selects spec-review, test-review" in p for p in data["problems"])
    assert data["checks"] == {"symlinks": True, "abs_paths": True, "secrets": True}  # the verifier's record copies these three, and only these


def test_check_diff_of_a_run_says_a_size_l_change_ends_before_reduce(run_cli, adopted):
    plan(run_cli, adopted, "feature", "code.S")
    write(adopted / "src" / "big.py", numbered(500))
    result, data = check_diff(run_cli, adopted)
    assert result.returncode == 1 and data["row"]["measured"] == "code.L"
    assert any("this row ends before reduce: split the change (it measures as code.L" in p for p in data["problems"])


# --- a size problem names the files that contribute most


def test_a_change_that_outgrew_its_row_names_the_five_files_that_contribute_most_untracked_ones_included(run_cli, adopted):
    plan(run_cli, adopted, "feature", "code.S")
    for name, count in (("a", 60), ("b", 50), ("c", 40), ("d", 30), ("e", 25), ("f", 20), ("g", 10)):  # 235 changed lines, none of it committed
        write(adopted / "src" / f"big_{name}.py", numbered(count))
    result, data = check_diff(run_cli, adopted)
    assert result.returncode == 1 and data["row"]["measured"] == "code.M"
    [problem] = [p for p in data["problems"] if "the change measures as code.M" in p]
    assert problem.endswith(
        "(`plan --intent feature --row code.M`). The files that contribute most: src/big_a.py (60 lines), src/big_b.py (50 lines), src/big_c.py (40 lines), "
        "src/big_d.py (30 lines), src/big_e.py (25 lines) and 2 files more."
    )


def test_a_size_l_change_names_its_biggest_files_in_the_check_and_in_the_refusal_of_pass(run_cli, adopted):
    plan(run_cli, adopted, "review-only", "code.S")
    write(adopted / "src" / "big.py", numbered(450))
    write(adopted / "src" / "small.py", numbered(3))
    result, data = check_diff(run_cli, adopted)
    [problem] = [p for p in data["problems"] if p.startswith("this row ends before reduce")]
    assert problem == (
        "this row ends before reduce: split the change (it measures as code.L, 453 changed lines, and nothing is built at that size)."
        " The files that contribute most: src/big.py (450 lines), src/small.py (3 lines)."
    )
    commit_all(adopted, "the big change")
    verify_now(adopted)
    assert run_cli("gate", RUN, "verify", cwd=adopted).returncode == 0
    put(adopted, "review", review_record(diff=now_hash(adopted)))
    refused = pass_of(run_cli, adopted)
    assert refused.returncode == 1 and problem in refused.stdout


def test_a_modified_file_counts_its_added_and_removed_lines(run_cli, adopted):
    plan(run_cli, adopted, "feature", "code.S")
    write(adopted / "src" / "app.py", numbered(70))  # the base holds two lines of it: 70 added, 2 removed
    write(adopted / "src" / "extra.py", numbered(40))
    _result, data = check_diff(run_cli, adopted)
    [problem] = [p for p in data["problems"] if "the change measures as code.M" in p]
    assert problem.endswith("The files that contribute most: src/app.py (72 lines), src/extra.py (40 lines).")


def test_generated_files_and_stubs_in_the_run_are_not_named_because_they_do_not_count(run_cli, adopted):
    plan(run_cli, adopted, "feature", "code.S")
    write(adopted / "uv.lock", numbered(300))  # a generated file
    write(adopted / ".plumbline" / "runs" / RUN / "stubs" / "newmod.py", numbered(300))  # a stub, in the run
    write(adopted / "src" / "new_module.py", numbered(100))
    _result, data = check_diff(run_cli, adopted)
    [problem] = [p for p in data["problems"] if "the change measures as code.M" in p]
    assert problem.endswith("The files that contribute most: src/new_module.py (100 lines).")
    assert "uv.lock" not in problem and "newmod" not in problem


def record_of(*files):
    """A change_class record with these (path, added, removed, generated) files."""
    return {"files": [{"path": path, "type": "code", "added": added, "removed": removed, "generated": generated, "symlink": False} for path, added, removed, generated in files]}


def test_biggest_files_orders_by_changed_lines_breaks_ties_by_path_and_names_at_most_five():
    files = [(f"src/{name}.py", count, 0, False) for name, count in (("z", 5), ("a", 5), ("m", 9), ("b", 1), ("c", 7), ("d", 6), ("e", 2))]
    assert pl.biggest_files(record_of(*files)) == (
        " The files that contribute most: src/m.py (9 lines), src/c.py (7 lines), src/d.py (6 lines), src/a.py (5 lines), src/z.py (5 lines) and 2 files more."
    )
    assert pl.biggest_files(record_of(*files), limit=7).endswith("src/e.py (2 lines), src/b.py (1 line).")
    six = record_of(*files[:2], ("x", 1, 0, False), ("y", 1, 0, False), ("w", 1, 0, False), ("v", 1, 0, False))
    assert pl.biggest_files(six).endswith("and 1 file more.")


def test_biggest_files_leaves_out_generated_files_and_files_with_no_counted_lines():
    record = record_of(("uv.lock", 300, 0, True), ("logo.png", 0, 0, False), ("src/a.py", 1, 1, False))
    assert pl.biggest_files(record) == " The files that contribute most: src/a.py (2 lines)."
    assert pl.biggest_files(record_of(("uv.lock", 300, 0, True), ("logo.png", 0, 0, False))) == ""
    assert pl.biggest_files(record_of()) == ""


def test_check_diff_of_a_run_of_a_row_without_reduce_says_so(run_cli, adopted):
    write(adopted / "src" / "big.py", numbered(500))
    plan(run_cli, adopted, "feature", "code.L")
    result, data = check_diff(run_cli, adopted)
    assert result.returncode == 1 and any("row code.L has no reduce stage" in p for p in data["problems"])


def test_check_diff_without_a_run_has_no_row(run_cli, adopted):
    write(adopted / "src" / "new_module.py", numbered(10))
    result = run_cli("check-diff", "--base", "main", cwd=adopted)
    assert "row" not in json.loads(result.stdout)


def test_check_diff_of_a_run_with_nothing_changed_has_no_measured_row(run_cli, adopted):
    plan(run_cli, adopted, "feature", "code.S")
    result, data = check_diff(run_cli, adopted)
    assert result.returncode == 0 and data["row"] == {"declared": "code.S", "measured": None, "missing_stages": []}


# --- C-14: a refactor leaves the tests as they are


@pytest.fixture
def refactoring(adopted):
    """A repository with an existing test, committed on the base branch."""
    write(adopted / "tests" / "test_app.py", "from src.app import main\n\n\ndef test_main():\n    assert main() == 1\n")
    commit_all(adopted, "an existing test")
    git(adopted, "branch", "-f", "main", "HEAD")
    return adopted


def start_refactor(run_cli, repo):
    return plan(run_cli, repo, "refactor", "code.S")


def test_check_diff_reports_a_changed_test_file_under_a_refactor_and_exits_1(run_cli, refactoring):  # C-14: exp_refactor_tests
    write(refactoring / "src" / "app.py", "def main():\n    return 2  # behaviour changed\n")
    start_refactor(run_cli, refactoring)
    write(refactoring / "tests" / "test_app.py", "from src.app import main\n\n\ndef test_main():\n    assert main() == 2  # the test edited to match\n")
    result, data = check_diff(run_cli, refactoring)
    assert result.returncode == 1
    assert "the intent refactor leaves the tests as they are, but the change touches tests/test_app.py" in data["problems"]
    assert data["checks"] == {"symlinks": True, "abs_paths": True, "secrets": True}


def test_check_diff_of_a_refactor_that_leaves_the_tests_alone_is_clean(run_cli, refactoring):
    start_refactor(run_cli, refactoring)
    write(refactoring / "src" / "app.py", "def main():\n    return 1  # restructured\n")
    result, data = check_diff(run_cli, refactoring)
    assert result.returncode == 0 and data["problems"] == []


def test_check_diff_of_another_intent_may_change_a_test(run_cli, refactoring):
    plan(run_cli, refactoring, "review-only", "code.S")
    write(refactoring / "tests" / "test_app.py", "from src.app import main\n\n\ndef test_main():\n    assert main() == 2\n")
    result, data = check_diff(run_cli, refactoring)
    assert result.returncode == 0 and data["problems"] == []


def test_a_refactor_whose_diff_edits_a_test_fails_the_verify_gate_and_gets_no_pass(run_cli, refactoring):  # C-14
    start_refactor(run_cli, refactoring)
    write(refactoring / "src" / "app.py", "def main():\n    return 2\n")
    write(refactoring / "tests" / "test_app.py", "from src.app import main\n\n\ndef test_main():\n    assert main() == 2\n")
    put(refactoring, "build", build_note_record())
    verify_now(refactoring)
    result = run_cli("gate", RUN, "verify", cwd=refactoring)
    assert result.returncode == 1 and "the intent refactor leaves the tests as they are, but the change touches tests/test_app.py" in result.stdout
    commit_all(refactoring, "the test edited to match")
    assert pass_of(run_cli, refactoring).returncode == 1


def test_a_refactor_that_leaves_the_tests_alone_passes_the_verify_gate(run_cli, refactoring):
    start_refactor(run_cli, refactoring)
    write(refactoring / "src" / "app.py", "def main():\n    return 1  # restructured\n")
    put(refactoring, "build", build_note_record())
    verify_now(refactoring)
    assert run_cli("gate", RUN, "verify", cwd=refactoring).returncode == 0


def test_tests_touched_problems_lists_every_test_file(refactoring):
    write(refactoring / "tests" / "test_a.py", "x = 1\n")
    write(refactoring / "web" / "b.test.ts", "x\n")
    write(refactoring / "src" / "c.py", "x = 1\n")
    put(refactoring, "intake", intake_record("code.S", refactoring, intent="refactor"))
    project = pl.load_project(refactoring)
    run = pl.load_run(project, RUN)
    [problem] = pl.tests_touched_problems(project, run)
    assert problem == "the intent refactor leaves the tests as they are, but the change touches tests/test_a.py, web/b.test.ts"
