"""The protocol /plumbline:run follows, driven through the CLI with records written the way the agents write them (and the
ledger told what the hook tells it): start the run, gate each stage, verify with check-diff, review with the survival rule and the
detective, commit, pass, push. The gates of the tests stage and of the verify stage run the repository's test command, whose exit
status these tests set."""
import json

import pytest

import plumbline as pl
from helpers import commit_all, git, write
from hookdata import bash_payload, denial
from rundata import (
    CONTROLLED, RUN, adopt_base, build_note_record, put, put_part, read, set_exit_code, spec_record, verify_record, write_test_file,
    written_tests_record,
)


@pytest.fixture
def adopted(repo):
    adopt_base(repo, commands={"test": CONTROLLED})  # adopted long ago: the base branch has plumbline.toml, and the change is only what follows
    return repo


def cli(run_cli, repo, *args):
    return run_cli(*args, cwd=repo)


def start(run_cli, repo, intent="feature", *extra):
    result = cli(run_cli, repo, "plan", "--run-id", RUN, "--intent", intent, "--row", "code.S", *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def gate(run_cli, repo, stage):
    return cli(run_cli, repo, "gate", RUN, stage)


def verify_now(run_cli, repo, green=True):
    """The verifier's work: check-diff for the hash of the change as it is, and a verify record that carries it."""
    result = cli(run_cli, repo, "check-diff", "--run", RUN)
    assert result.returncode == 0, result.stdout + result.stderr
    put(repo, "verify", verify_record(green=green, diff=json.loads(result.stdout)["diff_sha256"]))


def finding(fid, lens, severity="BLOCKING", file="src/retry.py"):
    return {
        "id": fid, "lens": lens, "file": file, "line": 3, "claim": f"claim {fid}", "failure_scenario": "a concrete input",
        "rule": "AC-1", "evidence": "return url", "outside_code": None, "severity": severity,
    }


def review_round(run_cli, repo, findings_by_lens, refuting=()):
    """One round of the review unit: the prosecutors' records, three defenders (those in `refuting` refute every finding, quoting the
    code), then merge-review."""
    for lens, findings in findings_by_lens.items():
        put_part(repo, "review", f"prosecutor-{lens}", {"lens": lens, "findings": findings})
    all_findings = [f for findings in findings_by_lens.values() for f in findings]
    for k in (1, 2, 3):
        name = f"defender-{k}"
        verdict = "refuted" if k in refuting else "conceded"
        defenses = [{"finding_id": f["id"], "defender": name, "verdict": verdict, "quote": "return url" if verdict == "refuted" else "", "reason": "read the code"} for f in all_findings]
        put_part(repo, "review", name, {"defender": name, "defenses": defenses})
    return cli(run_cli, repo, "merge-review", RUN, "review")


def the_tests_are_written(run_cli, repo):
    """The test-writer's stage: the test file, its record, and the gate, which needs the test command to fail on the stubs."""
    write_test_file(repo)
    put(repo, "tests", written_tests_record())
    set_exit_code(repo, 1)
    result = gate(run_cli, repo, "tests")
    set_exit_code(repo, 0)
    assert result.returncode == 0, result.stdout
    return result


def test_a_feature_run_goes_from_plan_to_a_pass_and_the_push_is_let_through(run_cli, run_pre, adopted):
    plan = start(run_cli, adopted)
    assert [s["id"] for s in plan["stages"]] == ["intake", "plan", "tests", "build", "verify", "review", "reduce"]
    # plan: the planner's spec
    put(adopted, "plan", spec_record())
    assert gate(run_cli, adopted, "plan").returncode == 0
    # tests: the test-writer's tests record, and the test command failing on the stubs
    the_tests_are_written(run_cli, adopted)
    # build: the builder writes the source (no gate)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    # verify: check-diff, then the record with the hash of the change as it is; the gate runs the test command itself
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    # review: a finding two of three defenders refute (quoting the code) does not stand; the detective then adds its gaps
    result = review_round(run_cli, adopted, {"correctness": [finding("correctness-1", "correctness")], "tests": []}, refuting=(1, 2))
    assert result.returncode == 0, result.stdout
    assert read(adopted, "review")["blockers_surviving"] == 0 and read(adopted, "review")["survivors"] == []
    put_part(adopted, "review", "detective", {"gaps": [{"id": "G-1", "kind": "edge_case", "detail": "retries=0", "ac": None}]})
    assert cli(run_cli, adopted, "merge-review", RUN, "review").returncode == 0
    assert read(adopted, "review")["gaps"][0]["id"] == "G-1"
    assert gate(run_cli, adopted, "review").returncode == 0
    # reduce: the main session commits, then pass
    commit_all(adopted, "add retry")
    result = cli(run_cli, adopted, "pass", RUN)
    assert result.returncode == 0, result.stdout
    assert "covered by a pass record (run r1)" in cli(run_cli, adopted, "status").stdout
    assert denial(run_pre(bash_payload(adopted, "git push origin feature"), adopted)) is None


def test_a_red_verify_sends_the_run_back_to_build_and_the_second_round_passes(run_cli, adopted):
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    the_tests_are_written(run_cli, adopted)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return None\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted, green=False)
    failed = gate(run_cli, adopted, "verify")
    assert failed.returncode == 1 and "the verify record is not green (failing: AC-1)" in failed.stdout and "round 1 of 3" in failed.stdout
    dry_run = json.loads(cli(run_cli, adopted, "plan", "--row", "code.S").stdout)  # a plan without --intent starts nothing
    assert next(s for s in dry_run["stages"] if s["id"] == "verify")["on_fail"] == "build"
    # the builder is sent the failing criteria (the verify record: an AC id and a kind of error, never assertion text) and rebuilds
    assert pl.check_record("verify_record", read(adopted, "verify")) == []
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    second = gate(run_cli, adopted, "verify")
    assert second.returncode == 0 and "round 2 of 3" in second.stdout
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert gate(run_cli, adopted, "review").returncode == 0
    assert gate(run_cli, adopted, "plan").returncode == 0 and gate(run_cli, adopted, "tests").returncode == 0  # the test-writer's record still holds
    commit_all(adopted, "add retry")
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0
    rounds = {s["id"]: s["rounds"] for s in read(adopted, "reduce")["stages"]}
    assert rounds["review"] == 1 and rounds["verify"] == 2 and rounds["build"] == 2


def test_a_surviving_blocker_fails_the_review_gate_and_the_next_round_after_a_rebuild_passes(run_cli, adopted):
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    the_tests_are_written(run_cli, adopted)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    review_round(run_cli, adopted, {"correctness": [finding("correctness-1", "correctness")], "tests": []}, refuting=(1,))  # one refutation: the blocker stands
    blocked = gate(run_cli, adopted, "review")
    assert blocked.returncode == 1 and "1 blocker(s) survive: correctness-1" in blocked.stdout
    # the run goes back to build with the finding; the change is edited, so verify and the review run again, as round 2
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url.strip()\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    for lens in ("correctness", "tests"):
        put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []}, round_no=2)
    for k in (1, 2, 3):
        put_part(adopted, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []}, round_no=2)
    assert cli(run_cli, adopted, "merge-review", RUN, "review", "--round", "2").returncode == 0
    assert read(adopted, "review")["round"] == 2 and gate(run_cli, adopted, "review").returncode == 0
    for stage in ("plan", "tests"):
        assert gate(run_cli, adopted, stage).returncode == 0
    commit_all(adopted, "add retry")
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0
    assert {s["id"]: s["rounds"] for s in read(adopted, "reduce")["stages"]}["review"] == 2  # the review took two rounds


def test_an_edit_after_the_review_fails_pass_and_a_rerun_from_verify_passes(run_cli, adopted):
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    the_tests_are_written(run_cli, adopted)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert gate(run_cli, adopted, "review").returncode == 0
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url.strip()\n")  # a last-minute edit, after the review
    commit_all(adopted, "add retry")
    refused = cli(run_cli, adopted, "pass", RUN)
    assert refused.returncode == 1 and "the change was edited after that record was made, so run the pipeline again from verify" in refused.stdout
    verify_now(run_cli, adopted)  # the change is as committed
    assert gate(run_cli, adopted, "verify").returncode == 0
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0  # the agents read the change as committed
    assert gate(run_cli, adopted, "review").returncode == 0
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0


def test_a_fix_run_needs_its_tests_to_fail_first_and_then_passes_like_any_other(run_cli, adopted):
    spec = spec_record(planned=("AC-1",))
    spec["acceptance_criteria"] = spec["acceptance_criteria"][:1]
    write(adopted / ".plumbline" / "bug-spec.json", json.dumps(spec))
    plan = start(run_cli, adopted, "fix", "--spec", ".plumbline/bug-spec.json")
    assert [s["id"] for s in plan["stages"]] == ["intake", "tests", "build", "verify", "review", "reduce"]  # code.S has no test-review
    write_test_file(adopted, covering=("AC-1",))
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=False))
    set_exit_code(adopted, 1)
    assert gate(run_cli, adopted, "tests").returncode == 1  # a test that already passes reproduces nothing
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=True))
    assert gate(run_cli, adopted, "tests").returncode == 0
    set_exit_code(adopted, 0)
    write(adopted / "src" / "app.py", "def main():\n    return 2\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert gate(run_cli, adopted, "review").returncode == 0
    commit_all(adopted, "fix main")
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0
    record = read(adopted, "reduce")
    assert [(s["id"], s["rounds"]) for s in record["stages"]][:2] == [("intake", 1), ("plan", 0)]


def test_a_review_only_run_needs_only_verify_and_review(run_cli, adopted):
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    commit_all(adopted, "the change already exists")
    plan = start(run_cli, adopted, "review-only")
    assert [s["id"] for s in plan["stages"]] == ["intake", "verify", "review", "reduce"]
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert gate(run_cli, adopted, "review").returncode == 0
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0


def test_git_history_and_the_working_tree_are_untouched_by_everything_but_the_agents_and_the_commit(run_cli, adopted):
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    before = git(adopted, "status", "--porcelain")
    head = git(adopted, "rev-parse", "HEAD")
    start(run_cli, adopted, "review-only")
    verify_now(run_cli, adopted)
    gate(run_cli, adopted, "verify")
    review_round(run_cli, adopted, {"correctness": [], "tests": []})
    assert git(adopted, "status", "--porcelain") == before and git(adopted, "rev-parse", "HEAD") == head
    assert git(adopted, "diff", "--cached", "--name-only") == ""  # the hashing stages nothing


def test_a_round_in_which_no_prosecutor_filed_a_finding_needs_no_defenders(run_cli, adopted):
    # /plumbline:run skips the defenders then: there is nothing to answer
    start(run_cli, adopted)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    for lens in ("correctness", "tests"):
        put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []})
    result = cli(run_cli, adopted, "merge-review", RUN, "review")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warning" not in result.stderr
    assert read(adopted, "review")["blockers_surviving"] == 0 and read(adopted, "review")["defenses"] == []
    # the change is verified against the row that has a review, and the review gate needs the change to be the one the round saw
    assert gate(run_cli, adopted, "review").returncode == 0


def test_a_stage_that_keeps_failing_stops_the_run_with_exit_3_after_its_rounds(run_cli, adopted):  # C-08
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    the_tests_are_written(run_cli, adopted)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return None\n")
    put(adopted, "build", build_note_record())
    codes = []
    for _ in range(3):
        verify_now(run_cli, adopted, green=False)
        codes.append(gate(run_cli, adopted, "verify").returncode)
        put(adopted, "build", build_note_record())
    assert codes == [1, 1, 3]


def test_a_tests_lens_blocker_goes_to_the_test_writer_and_the_run_carries_on_after_the_tests_are_revised(run_cli, adopted):  # C-06, C-07
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    the_tests_are_written(run_cli, adopted)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    result = review_round(
        run_cli, adopted,
        {"correctness": [], "tests": [finding("tests-1", "tests", file="tests/test_app.py")]},
    )
    assert result.returncode == 0, result.stdout
    assert "surviving findings for the test-writer" in result.stdout and "surviving findings for the builder" not in result.stdout
    assert read(adopted, "review")["routes"] == {"builder": [], "test-writer": ["tests-1"]}
    assert gate(run_cli, adopted, "review").returncode == 1
    # the test-writer strengthens the tests; the code exists by now, so the tests are not expected to fail on stubs
    write(adopted / "tests" / "test_app.py", (adopted / "tests" / "test_app.py").read_text() + "def test_the_edge():\n    assert False\n")
    put(adopted, "tests", written_tests_record(ran=False, all_failed=False))
    revised = gate(run_cli, adopted, "tests")
    assert revised.returncode == 0, revised.stdout
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    for lens in ("correctness", "tests"):
        put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []}, round_no=2)
    for k in (1, 2, 3):
        put_part(adopted, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []}, round_no=2)
    assert cli(run_cli, adopted, "merge-review", RUN, "review", "--round", "2").returncode == 0
    assert gate(run_cli, adopted, "review").returncode == 0
    commit_all(adopted, "add retry")
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0
