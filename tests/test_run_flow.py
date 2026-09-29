"""The protocol /plumbline:run follows, driven through the CLI with records written the way the agents write them:
start the run, gate each stage, verify with check-diff, review with the survival rule and the detective, commit, pass, push."""
import json

import pytest

import plumbline as pl
from helpers import commit_all, git, write
from hookdata import bash_payload, denial
from rundata import RUN, adopt, build_note_record, put, put_part, read, review_record, spec_record, verify_record, written_tests_record


@pytest.fixture
def adopted(repo):
    adopt(repo)
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


def finding(fid, lens, severity="BLOCKING"):
    return {
        "id": fid, "lens": lens, "file": "src/retry.py", "line": 3, "claim": f"claim {fid}", "failure_scenario": "a concrete input",
        "rule": "AC-1", "evidence": "return None", "outside_code": None, "severity": severity,
    }


def review_round(run_cli, repo, findings_by_lens, refuting=()):
    """One round of the review unit: the prosecutors' records, three defenders (those in `refuting` refute every finding), then merge-review."""
    for lens, findings in findings_by_lens.items():
        put_part(repo, "review", f"prosecutor-{lens}", {"lens": lens, "findings": findings})
    all_findings = [f for findings in findings_by_lens.values() for f in findings]
    for k in (1, 2, 3):
        name = f"defender-{k}"
        verdict = "refuted" if k in refuting else "conceded"
        defenses = [{"finding_id": f["id"], "defender": name, "verdict": verdict, "quote": "return retry(url)" if verdict == "refuted" else "", "reason": "read the code"} for f in all_findings]
        put_part(repo, "review", name, {"defender": name, "defenses": defenses})
    return cli(run_cli, repo, "merge-review", RUN, "review")


def test_a_feature_run_goes_from_plan_to_a_pass_and_the_push_is_let_through(run_cli, run_pre, adopted):
    plan = start(run_cli, adopted)
    assert [s["id"] for s in plan["stages"]] == ["intake", "plan", "tests", "build", "verify", "review", "reduce"]
    # plan: the planner's spec
    put(adopted, "plan", spec_record())
    assert gate(run_cli, adopted, "plan").returncode == 0
    # tests: the test-writer's tests record
    put(adopted, "tests", written_tests_record())
    assert gate(run_cli, adopted, "tests").returncode == 0
    # build: the builder writes the source (no gate)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    # verify: check-diff, then the record with the hash of the change as it is
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    # review: a finding two of three defenders refute does not stand; the detective then adds its gaps
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
    put(adopted, "tests", written_tests_record())
    write(adopted / "src" / "retry.py", "def retry(url):\n    return None\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted, green=False)
    failed = gate(run_cli, adopted, "verify")
    assert failed.returncode == 1 and "the verify record is not green (failing: AC-1)" in failed.stdout
    dry_run = json.loads(cli(run_cli, adopted, "plan", "--row", "code.S").stdout)  # a plan without --intent starts nothing
    assert next(s for s in dry_run["stages"] if s["id"] == "verify")["on_fail"] == "build"
    # the builder is sent the failing criteria (the verify record: an AC id and a kind of error, never assertion text) and rebuilds
    assert pl.check_record("verify_record", read(adopted, "verify")) == []
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    verify_now(run_cli, adopted)
    assert gate(run_cli, adopted, "verify").returncode == 0
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert gate(run_cli, adopted, "review").returncode == 0
    assert gate(run_cli, adopted, "plan").returncode == 0 and gate(run_cli, adopted, "tests").returncode == 0
    commit_all(adopted, "add retry")
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0
    rounds = {s["id"]: s["rounds"] for s in read(adopted, "reduce")["stages"]}
    assert rounds["review"] == 1


def test_a_surviving_blocker_fails_the_review_gate_and_the_next_round_after_a_rebuild_passes(run_cli, adopted):
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record())
    write(adopted / "src" / "retry.py", "def retry(url):\n    return None\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    review_round(run_cli, adopted, {"correctness": [finding("correctness-1", "correctness")], "tests": []}, refuting=(1,))  # one refutation: the blocker stands
    blocked = gate(run_cli, adopted, "review")
    assert blocked.returncode == 1 and "1 blocker(s) survive: correctness-1" in blocked.stdout
    # the run goes back to build with the finding; the change is edited, so verify and the review run again, as round 2
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    verify_now(run_cli, adopted)
    put_part(adopted, "review", "prosecutor-correctness", {"lens": "correctness", "findings": []}, round_no=2)
    put_part(adopted, "review", "prosecutor-tests", {"lens": "tests", "findings": []}, round_no=2)
    for k in (1, 2, 3):
        put_part(adopted, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []}, round_no=2)
    assert cli(run_cli, adopted, "merge-review", RUN, "review", "--round", "2").returncode == 0
    assert read(adopted, "review")["round"] == 2 and gate(run_cli, adopted, "review").returncode == 0
    for stage in ("plan", "tests", "verify"):
        assert gate(run_cli, adopted, stage).returncode == 0
    commit_all(adopted, "add retry")
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0
    assert {s["id"]: s["rounds"] for s in read(adopted, "reduce")["stages"]}["review"] == 2  # the review took two rounds


def test_an_edit_after_the_review_fails_pass_and_a_rerun_from_verify_passes(run_cli, adopted):
    start(run_cli, adopted)
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record())
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url.strip()\n")  # a last-minute edit, after the review
    commit_all(adopted, "add retry")
    refused = cli(run_cli, adopted, "pass", RUN)
    assert refused.returncode == 1 and "the change was edited after that record was made, so run the pipeline again from verify" in refused.stdout
    verify_now(run_cli, adopted)  # the change is as committed
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0


def test_a_fix_run_needs_its_tests_to_fail_first_and_then_passes_like_any_other(run_cli, adopted):
    spec = spec_record(planned=("AC-1",))
    spec["acceptance_criteria"] = spec["acceptance_criteria"][:1]
    write(adopted / ".plumbline" / "bug-spec.json", json.dumps(spec))
    plan = start(run_cli, adopted, "fix", "--spec", ".plumbline/bug-spec.json")
    assert [s["id"] for s in plan["stages"]] == ["intake", "tests", "build", "verify", "review", "reduce"]  # code.S has no test-review
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=False))
    assert gate(run_cli, adopted, "tests").returncode == 1  # a test that already passes reproduces nothing
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=True))
    assert gate(run_cli, adopted, "tests").returncode == 0
    write(adopted / "src" / "app.py", "def main():\n    return 2\n")
    put(adopted, "build", build_note_record())
    verify_now(run_cli, adopted)
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
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
    assert review_round(run_cli, adopted, {"correctness": [], "tests": []}).returncode == 0
    assert cli(run_cli, adopted, "pass", RUN).returncode == 0


def test_git_history_and_the_working_tree_are_untouched_by_everything_but_the_agents_and_the_commit(run_cli, adopted):
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")
    before = git(adopted, "status", "--porcelain")
    head = git(adopted, "rev-parse", "HEAD")
    start(run_cli, adopted, "review-only")
    verify_now(run_cli, adopted)
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
    assert gate(run_cli, adopted, "review").returncode == 0
