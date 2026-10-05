"""pass, override and status: what lets a commit be pushed, and how that is shown."""
import json
import os
import subprocess

import pytest

import plumbline as pl
from helpers import commit_all, git, write
from rundata import (
    CONTROLLED, HAIKU, RUN, SONNET, adopt, agent_row, assistant_record, begin, build_note_record, change_of, gate_stages, intake_record, ledger, put, read,
    review_record, run_path, spec_record, verify_record, write_code_s_run, write_docs_run, write_ledger, write_transcript,
)


@pytest.fixture
def ready(repo):
    """An adopted, committed repository holding a finished docs-row run, its tree clean. It declares a test command, so `gate` can run."""
    adopt(repo, commands={"test": CONTROLLED})
    write_docs_run(repo)
    return repo


def covered_by(repo, sha):
    return pl.coverage(repo, sha, pl.load_project(repo))


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def pass_file(repo, sha=None):
    return repo / ".plumbline" / "pass" / f"{sha or head(repo)}.json"


def override_file(repo, sha=None):
    return repo / ".plumbline" / "pass" / f"{sha or head(repo)}.override.json"


def do_pass(run_cli, repo, run_id=RUN):
    return run_cli("pass", run_id, cwd=repo)


# --- pass: what it refuses


def test_pass_refuses_a_dirty_tree_and_writes_nothing(run_cli, ready):
    write(ready / "README.md", "# demo\nchanged\n")
    result = do_pass(run_cli, ready)
    assert result.returncode == 1
    assert "the working tree is not clean apart from .plumbline/: README.md" in result.stdout
    assert "pass: refused for run 'r1'" in result.stdout and "nothing was written" in result.stdout
    assert not pass_file(ready).exists() and not run_path(ready, RUN, "reduce.json").exists()


def test_pass_counts_an_untracked_file_as_dirty_but_not_what_is_under_plumbline(run_cli, ready):
    write(ready / "notes.txt", "x\n")
    assert do_pass(run_cli, ready).returncode == 1
    (ready / "notes.txt").unlink()
    write(ready / ".plumbline" / "scratch.txt", "x\n")  # plumbline's own directory does not count, ignored or not
    assert do_pass(run_cli, ready).returncode == 0


def test_pass_does_not_count_the_plumbline_directory_even_when_it_is_not_ignored(run_cli, repo):
    write(repo / "plumbline.toml", "schema = 1\n")
    commit_all(repo, "adopt without ignoring")
    write_docs_run(repo)
    result = do_pass(run_cli, repo)
    assert result.returncode == 0, result.stdout


def test_dirty_paths_reads_renames_and_awkward_names_from_git_status(ready):
    git(ready, "mv", "README.md", "renamed readme.md")  # a staged rename: git lists the new name, then the old one
    write(ready / "dir with space" / "new file.txt", "x\n")
    write(ready / "src" / "app.py", "def main():\n    return 2\n")  # modified
    assert sorted(pl.dirty_paths(ready)) == ["dir with space/new file.txt", "renamed readme.md", "src/app.py"]
    write(ready / ".plumbline" / "note.txt", "x\n")
    assert ".plumbline/note.txt" not in pl.dirty_paths(ready)


def test_pass_refuses_when_a_gate_fails_and_says_which(run_cli, ready):
    put(ready, "verify", verify_record(green=False))
    result = do_pass(run_cli, ready)
    assert result.returncode == 1
    assert "error: stage 'verify' (gate verify_green): the agent's record: the verify record is not green (failing: AC-1)" in result.stdout
    assert not pass_file(ready).exists() and not run_path(ready, RUN, "reduce.json").exists()


def test_pass_refuses_when_a_stage_has_no_record_even_without_a_gate(run_cli, repo):
    adopt(repo)
    write_code_s_run(repo)
    run_path(repo, RUN, "build.json").unlink()
    result = do_pass(run_cli, repo)
    assert result.returncode == 1
    assert "error: stage 'build': .plumbline/runs/r1/build.json: no such file" in result.stdout


def test_pass_lists_every_problem(run_cli, ready):
    write(ready / "README.md", "changed\n")
    put(ready, "verify", verify_record(green=False, diff=change_of(ready)))
    put(ready, "review", review_record(blockers=1, diff=change_of(ready)))
    result = do_pass(run_cli, ready)
    assert "3 problems" in result.stdout


def test_pass_evaluates_the_gates_afresh_whatever_the_ledger_says(run_cli, ready):
    gate_stages(run_cli, ready, ["verify", "review"])  # both passed once
    put(ready, "verify", verify_record(green=False))  # and then verify changed
    assert do_pass(run_cli, ready).returncode == 1
    rows = [e for e in ledger(ready) if e["kind"] == "gate" and e["stage"] == "verify"]
    assert [r["passed"] for r in rows] == [True, False]  # the failed evaluation is in the ledger too


def test_pass_needs_an_adopted_repository(run_cli, repo):
    write_docs_run(repo)
    result = do_pass(run_cli, repo)
    assert result.returncode == 2 and "has not adopted plumbline" in result.stderr


def test_pass_for_a_run_that_does_not_exist_could_not_be_written(run_cli, ready):
    result = do_pass(run_cli, ready, "nope")
    assert result.returncode == 2 and "there is no run 'nope'" in result.stderr


def test_pass_in_a_repository_without_commits_could_not_be_written(run_cli, tmp_path):
    fresh = tmp_path / "fresh"
    assert subprocess.run(["git", "init", "-q", "-b", "main", str(fresh)], capture_output=True).returncode == 0
    write(fresh / "plumbline.toml", "schema = 1\n")
    put(fresh, "intake", intake_record("docs"))
    result = do_pass(run_cli, fresh)
    assert result.returncode == 2 and "no commits" in result.stderr


# --- pass: what it writes


def test_pass_writes_the_record_to_the_run_and_to_the_pass_directory(run_cli, ready):
    result = do_pass(run_cli, ready)
    assert result.returncode == 0, result.stdout + result.stderr
    sha = head(ready)
    assert f"pass recorded for {sha[:7]} (run r1, row docs)" in result.stdout
    assert "wrote .plumbline/runs/r1/reduce.json and .plumbline/pass/" in result.stdout
    in_pass_dir = json.loads(pass_file(ready).read_text(encoding="utf-8"))
    in_run = read(ready, "reduce")
    assert in_pass_dir == in_run
    assert pl.check_record("pass_record", in_pass_dir) == []
    assert (in_pass_dir["commit"], in_pass_dir["run_id"], in_pass_dir["row"], in_pass_dir["verdict"]) == (sha, "r1", "docs", "pass")


def test_the_pass_record_lists_every_stage_of_the_row_with_its_gate_and_result(run_cli, ready):
    do_pass(run_cli, ready)
    stages = read(ready, "reduce")["stages"]
    assert [(s["id"], s["gate"], s["passed"], s["rounds"]) for s in stages] == [
        ("intake", None, True, 1),
        ("verify", "verify_green", True, 1),
        ("review", "no_surviving_blockers", True, 1),
        ("reduce", "all_gates_passed", True, 1),
    ]
    assert [s["record"] for s in stages] == [f".plumbline/runs/r1/{n}.json" for n in ("intake", "verify", "review", "reduce")]


def test_pass_enters_each_gate_it_evaluated_in_the_ledger(run_cli, ready):
    do_pass(run_cli, ready)
    assert [(e["stage"], e["gate"], e["passed"]) for e in ledger(ready) if e["kind"] == "gate"] == [
        ("verify", "verify_green", True),
        ("review", "no_surviving_blockers", True),
    ]


def test_the_pass_record_carries_the_tokens_of_the_runs_agents(run_cli, ready, tmp_path):
    transcript = write_transcript(tmp_path / "agent.jsonl", [assistant_record("m1", SONNET, output=12, inp=3, cache_write=4, cache_read=500)])
    other = write_transcript(tmp_path / "agent2.jsonl", [assistant_record("m2", HAIKU, output=2, inp=1)])
    write_ledger(ready, [agent_row("a1", transcript=str(transcript)), agent_row("b2", transcript=str(other))])
    result = do_pass(run_cli, ready)
    assert result.returncode == 0
    assert read(ready, "reduce")["tokens"] == {
        "by_model": {
            HAIKU: {"output": 2, "fresh_input": 1, "cache_read": 0, "output_lower_bound": 0},
            SONNET: {"output": 12, "fresh_input": 7, "cache_read": 500, "output_lower_bound": 0},
        }
    }
    assert f"tokens {SONNET}: output 12, fresh input 7, cache read 500" in result.stdout


def test_the_pass_record_and_its_output_say_when_an_output_count_is_a_lower_bound(run_cli, ready, tmp_path):
    transcript = write_transcript(tmp_path / "agent.jsonl", [assistant_record("m1", SONNET, output=12, inp=3, cache_write=4, cache_read=500, stop_reason=None)])
    write_ledger(ready, [agent_row("a1", transcript=str(transcript))])
    result = do_pass(run_cli, ready)
    assert result.returncode == 0, result.stdout
    record = read(ready, "reduce")
    assert record["tokens"]["by_model"][SONNET] == {"output": 12, "fresh_input": 7, "cache_read": 500, "output_lower_bound": 1}
    assert f"the output tokens of {SONNET} are a lower bound: 1 message had no final usage entry" in " ".join(record["notes"])
    assert f"tokens {SONNET}: output at least 12, fresh input 7, cache read 500" in result.stdout
    assert pl.check_record("pass_record", record) == []


def test_rounds_come_from_the_review_record_and_from_the_agents_the_ledger_saw(run_cli, repo):
    adopt(repo)
    write_code_s_run(repo)  # one stop of each agent
    put(repo, "review", review_record(blockers=0, round_no=2, diff=change_of(repo)))
    put(repo, "build", build_note_record())  # the builder ran a second time
    result = do_pass(run_cli, repo)
    assert result.returncode == 0, result.stdout
    rounds = {s["id"]: s["rounds"] for s in read(repo, "reduce")["stages"]}
    assert rounds == {"intake": 1, "plan": 1, "tests": 1, "build": 2, "verify": 1, "review": 2, "reduce": 1}


def test_an_agent_that_stopped_several_times_is_one_round_in_the_pass_record_and_two_agents_are_two(run_cli, repo):
    adopt(repo)
    write_code_s_run(repo)
    [planner_stop] = [e for e in ledger(repo) if e["kind"] == "agent" and e["stage"] == "plan"]
    write_ledger(repo, [{k: v for k, v in planner_stop.items() if k != "at"}] * 3)  # the same agent, entered three times more (as 0.4.0 did for a report the harness asked for again)
    put(repo, "build", build_note_record())  # the builder ran twice: two agents
    result = do_pass(run_cli, repo)
    assert result.returncode == 0, result.stdout
    rounds = {s["id"]: s["rounds"] for s in read(repo, "reduce")["stages"]}
    assert rounds["plan"] == 1 and rounds["build"] == 2 and rounds["tests"] == 1


def test_the_pass_record_counts_the_agents_of_the_whole_run_though_the_gate_passed_in_between(run_cli, repo):
    adopt(repo)
    write_code_s_run(repo)
    put(repo, "plan", spec_record())  # a second planner
    gated = run_cli("gate", RUN, "plan", cwd=repo)
    assert gated.returncode == 0 and "round 2 of 2" in gated.stdout  # both planners are in the count of `gate`, and its gate passes: the next count starts again
    result = do_pass(run_cli, repo)
    assert result.returncode == 0, result.stdout
    assert {s["id"]: s["rounds"] for s in read(repo, "reduce")["stages"]}["plan"] == 2


def test_a_row_note_travels_into_the_pass_record_with_the_declared_and_the_measured_row(run_cli, repo):
    # the adoption commit changes plumbline.toml and .gitignore, which are config files: the config row is overridden too
    write(repo / "plumbline.toml", 'schema = 1\n\n[matrix.docs]\nstages = ["intake", "reduce"]\nnote = "Handled by the repo\'s own evals."\n\n[matrix.config]\nstages = ["intake", "reduce"]\n')
    write(repo / ".gitignore", ".plumbline/\n")
    commit_all(repo, "adopt")
    begin(repo, "docs")
    assert do_pass(run_cli, repo).returncode == 0
    record = read(repo, "reduce")
    assert record["notes"] == ["Handled by the repo's own evals.", "declared row docs, measured row config"]
    assert [s["id"] for s in record["stages"]] == ["intake", "reduce"]


def test_a_second_pass_for_the_same_commit_replaces_the_first(run_cli, ready):
    do_pass(run_cli, ready)
    first = pass_file(ready).read_text(encoding="utf-8")
    write_docs_run(ready, "r2")
    assert do_pass(run_cli, ready, "r2").returncode == 0
    assert json.loads(pass_file(ready).read_text(encoding="utf-8"))["run_id"] == "r2" and first != pass_file(ready).read_text(encoding="utf-8")


def test_the_pass_file_is_never_left_half_written(run_cli, ready):
    do_pass(run_cli, ready)
    assert sorted(p.name for p in pass_file(ready).parent.iterdir()) == [f"{head(ready)}.json"]  # no temporary file remains


# --- override


REASON = "The pipeline cannot run offline; a one-line typo fix."


def do_override(run_cli, repo, reason=REASON, *extra):
    return run_cli("override", "--reason", reason, *extra, cwd=repo)


def test_override_writes_the_override_record_for_head(run_cli, ready):
    result = do_override(run_cli, ready)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(override_file(ready).read_text(encoding="utf-8"))
    assert pl.check_record("override_record", record) == []
    assert (record["commit"], record["reason"], record["by"]) == (head(ready), REASON, "Test User")
    assert record["at"].endswith("Z") and len(record["at"]) == 20
    assert f"override recorded for {head(ready)[:7]}" in result.stdout


def test_override_needs_a_reason_of_twenty_characters(run_cli, ready):
    for short in ("skip", "x" * 19, " " * 30, ""):
        result = do_override(run_cli, ready, short)
        assert result.returncode == 1, short
        assert "the reason must be at least 20 characters" in result.stderr and "nothing was written" in result.stderr
        assert not override_file(ready).exists()
    assert do_override(run_cli, ready, "x" * 20).returncode == 0
    assert json.loads(override_file(ready).read_text(encoding="utf-8"))["reason"] == "x" * 20


def test_twenty_characters_are_counted_after_trimming(run_cli, ready):
    assert do_override(run_cli, ready, "  " + "y" * 19 + "  ").returncode == 1
    assert do_override(run_cli, ready, "  " + "y" * 20 + "  ").returncode == 0
    assert json.loads(override_file(ready).read_text(encoding="utf-8"))["reason"] == "y" * 20


def test_override_without_a_reason_is_a_usage_error(run_cli, ready):
    result = run_cli("override", cwd=ready)
    assert result.returncode == 2 and "--reason" in result.stderr
    assert not override_file(ready).exists()


def test_override_lists_the_stages_of_the_latest_run_that_did_not_pass(run_cli, ready):
    put(ready, "verify", verify_record(green=False))
    assert do_override(run_cli, ready).returncode == 0
    record = json.loads(override_file(ready).read_text(encoding="utf-8"))
    assert record["stages_skipped"] == ["verify"]  # intake and review are fine; reduce is not a stage that ran


def test_override_lists_a_named_run(run_cli, ready):
    write_docs_run(ready, "r2")
    os.remove(run_path(ready, "r2", "review.json"))
    assert do_override(run_cli, ready, REASON, "--run", "r1").returncode == 0
    assert json.loads(override_file(ready).read_text(encoding="utf-8"))["stages_skipped"] == []


def test_override_without_any_run_lists_every_stage_of_the_pipeline(run_cli, repo):
    adopt(repo)
    assert do_override(run_cli, repo).returncode == 0
    skipped = json.loads(override_file(repo).read_text(encoding="utf-8"))["stages_skipped"]
    assert skipped == ["intake", "plan", "spec-review", "tests", "test-review", "build", "verify", "review", "reduce"]


def test_override_with_an_unusable_latest_run_lists_every_stage_and_still_works(run_cli, repo):
    adopt(repo)
    put(repo, "verify", "{not json")  # a run with no intake record at all
    assert do_override(run_cli, repo).returncode == 0
    assert len(json.loads(override_file(repo).read_text(encoding="utf-8"))["stages_skipped"]) == 9


def test_override_for_a_run_that_does_not_exist_could_not_be_written(run_cli, ready):
    result = do_override(run_cli, ready, REASON, "--run", "nope")
    assert result.returncode == 2 and "there is no run 'nope'" in result.stderr


def test_override_never_overwrites_an_earlier_one(run_cli, ready):
    do_override(run_cli, ready)
    before = override_file(ready).read_text(encoding="utf-8")
    again = do_override(run_cli, ready, "A different reason, also long enough.")
    assert again.returncode == 1 and "already exists" in again.stderr
    assert override_file(ready).read_text(encoding="utf-8") == before


def test_override_needs_an_adopted_repository_but_not_a_clean_tree(run_cli, repo):
    assert do_override(run_cli, repo).returncode == 2
    adopt(repo)
    write(repo / "README.md", "dirty\n")
    assert do_override(run_cli, repo).returncode == 0


# --- what counts as covering HEAD


def test_a_pass_record_covers_only_the_commit_it_names(run_cli, ready):
    do_pass(run_cli, ready)
    assert covered_by(ready, head(ready))[0] == "pass"
    write(ready / "README.md", "next\n")
    commit_all(ready, "next")
    assert covered_by(ready, head(ready))[0] is None


def test_a_pass_record_that_names_another_commit_or_says_fail_covers_nothing(run_cli, ready):
    do_pass(run_cli, ready)
    record = read(ready, "reduce")
    sha = head(ready)
    pass_file(ready).write_text(json.dumps({**record, "commit": "0" * 40}), encoding="utf-8")
    assert covered_by(ready, sha) == (None, f".plumbline/pass/{sha}.json is for another commit")
    pass_file(ready).write_text(json.dumps({**record, "verdict": "fail"}), encoding="utf-8")
    assert covered_by(ready, sha)[0] is None
    pass_file(ready).write_text("{not json", encoding="utf-8")
    assert covered_by(ready, sha)[0] is None
    pass_file(ready).write_text(json.dumps({"commit": sha}), encoding="utf-8")
    assert covered_by(ready, sha)[0] is None


def test_an_override_record_covers_its_commit_and_an_invalid_one_does_not(run_cli, ready):
    sha = head(ready)
    do_override(run_cli, ready)
    assert covered_by(ready, sha) == ("override", REASON)
    record = json.loads(override_file(ready).read_text(encoding="utf-8"))
    override_file(ready).write_text(json.dumps({**record, "reason": "short"}), encoding="utf-8")
    assert covered_by(ready, sha)[0] is None
    override_file(ready).write_text(json.dumps({**record, "commit": "1" * 40}), encoding="utf-8")
    assert covered_by(ready, sha)[0] is None


def test_coverage_never_takes_a_path_from_something_that_is_not_a_commit_id(ready):
    assert covered_by(ready, "../../etc/passwd")[0] is None


# --- status


def status(run_cli, repo, *extra):
    result = run_cli("status", *extra, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_status_of_a_repository_that_has_not_adopted_plumbline(run_cli, repo):
    out = status(run_cli, repo)
    assert "not adopted" in out and "/plumbline:init adopts it" in out and "latest run: none" in out


def test_status_of_an_adopted_repository_without_runs(run_cli, repo):
    adopt(repo)
    out = status(run_cli, repo)
    assert "adopted in" in out and "(pipeline 'default', graft off)" in out
    assert f"HEAD {head(repo)[:7]}: NOT covered (no pass or override record)" in out
    assert "latest run: none" in out


def test_status_shows_the_latest_runs_stages_and_gates(run_cli, repo):
    adopt(repo)
    write_docs_run(repo)
    put(repo, "verify", verify_record(green=False))
    out = status(run_cli, repo)
    assert "run r1: row docs, 4 stages" in out
    lines = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith("  ") and not line.startswith("      ")}
    assert lines["intake"][1:] == ["change_class", "recorded", "-"]
    assert lines["verify"][1:] == ["verify_record", "FAIL", "verify_green"]
    assert lines["review"][1:] == ["review_record", "pass", "no_surviving_blockers"]
    assert lines["reduce"][1:] == ["pass_record", "pending", "all_gates_passed"]
    assert "the verify record is not green (failing: AC-1)" in out


def test_status_shows_missing_and_invalid_records(run_cli, repo):
    adopt(repo)
    put(repo, "intake", intake_record("code.S"))
    put(repo, "plan", "{not json")
    out = status(run_cli, repo)
    states = {line.split()[0]: line.split()[2] for line in out.splitlines() if line.startswith("  ") and not line.startswith("      ")}
    assert states["plan"] == "invalid" and states["tests"] == "missing" and states["build"] == "missing"


def test_status_says_when_head_is_covered_by_a_pass(run_cli, ready):
    do_pass(run_cli, ready)
    out = status(run_cli, ready)
    assert f"HEAD {head(ready)[:7]}: covered by a pass record (run r1)" in out
    reduce_line = next(line for line in out.splitlines() if line.startswith("  reduce"))
    assert reduce_line.split()[2] == "recorded"


def test_status_says_when_head_is_covered_by_an_override(run_cli, ready):
    do_override(run_cli, ready)
    assert f"HEAD {head(ready)[:7]}: covered by an override ({REASON})" in status(run_cli, ready)


def test_status_writes_nothing(run_cli, ready):
    before = {p: p.read_bytes() for p in (ready / ".plumbline").rglob("*") if p.is_file()}
    status(run_cli, ready)
    assert {p: p.read_bytes() for p in (ready / ".plumbline").rglob("*") if p.is_file()} == before


def test_status_follows_the_newest_run_unless_one_is_named(run_cli, ready):
    write_docs_run(ready, "r2")
    for path in run_path(ready, "r1").iterdir():
        os.utime(path, (1_000_000_000, 1_000_000_000))  # r1 is older
    os.utime(run_path(ready, "r1"), (1_000_000_000, 1_000_000_000))
    assert "run r2:" in status(run_cli, ready)
    assert "run r1:" in status(run_cli, ready, "--run", "r1")


def test_status_of_a_run_whose_intake_cannot_be_read_says_so(run_cli, repo):
    adopt(repo)
    put(repo, "verify", verify_record())
    assert "run r1: the run's intake record is not usable" in status(run_cli, repo)


def test_status_outside_a_git_repository_could_not_run(run_cli, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("status", cwd=plain, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert result.returncode == 2 and "not inside a git repository" in result.stderr

