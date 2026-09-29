"""Provenance: a stage's record counts only when the ledger traces it. An agent's record needs the entry of the right plumbline agent, valid,
with the record's hash as it is now; a review needs the entry `merge-review` wrote and, for each part, the entry of its agent. What is
written by hand, or edited after the agent stopped, does not count (PF-FORGE, SS-HANDFIX, AG-ROLE)."""
import json

import pytest

import plumbline as pl
from helpers import git, write
from hookdata import bash_payload, denial
from rundata import (
    CONTROLLED, RUN, adopt, agent_stopped, begin, build_note_record, change_of, genuine_pass, intake_record, ledger, now_hash, put,
    put_part, read, review_record, run_entry, run_path, spec_record, verify_now, verify_record, write_code_s_run, write_docs_run,
    written_tests_record,
)


@pytest.fixture
def adopted(repo):
    adopt(repo, commands={"test": CONTROLLED})
    return repo


def do_pass(run_cli, repo):
    return run_cli("pass", RUN, cwd=repo)


def gate(run_cli, repo, stage):
    return run_cli("gate", RUN, stage, cwd=repo)


def push_is_denied(run_pre, repo):
    return denial(run_pre(bash_payload(repo, "git push origin feature"), repo))


# --- PF-FORGE: hand-written records, schema-valid and green, do not pass


def test_a_hand_written_green_verify_and_clean_review_do_not_get_a_pass(run_cli, run_pre, adopted):  # PF-FORGE
    """The reviewer's reproduction: write verify.json and review.json with the Write tool, commit, `pass`."""
    diff = change_of(adopted)
    begin(adopted, "docs")
    put(adopted, "verify", verify_record(diff=diff), agent=False)  # a valid, green record nobody ran
    put(adopted, "review", review_record(diff=diff), agent=False)  # a valid, clean review no prosecutor made
    run_entry(adopted, "verify", diff)  # even with a measured run of the commands: the record has no author
    assert pl.check_record("verify_record", read(adopted, "verify")) == [] and pl.check_record("review_record", read(adopted, "review")) == []
    result = do_pass(run_cli, adopted)
    assert result.returncode == 1
    assert "stage 'verify' (gate verify_green): its record has no entry from plumbline:verifier; run the verifier" in result.stdout
    assert "stage 'review' (gate no_surviving_blockers): its record has no entry from merge-review; run `plumbline.py merge-review r1 review`" in result.stdout
    assert not (adopted / ".plumbline" / "pass").exists()
    assert push_is_denied(run_pre, adopted)


def test_the_gate_of_a_hand_written_record_fails_and_says_which_agent_to_run(run_cli, adopted):
    begin(adopted)
    put(adopted, "verify", verify_record(diff=now_hash(adopted)), agent=False)
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 1
    assert "its record has no entry from plumbline:verifier; run the verifier" in result.stdout


def edited(record, key, value):
    record[key] = value
    return record


@pytest.mark.parametrize(
    "stage,role,make",
    [
        ("plan", "planner", lambda: edited(spec_record(), "goal", "A goal put in afterwards.")),
        ("tests", "test-writer", lambda: edited(written_tests_record(), "files_written", ["tests/test_app.py", "tests/extra.py"])),
        ("build", "builder", lambda: edited(build_note_record(), "summary", "A summary put in afterwards.")),
        ("verify", "verifier", lambda: edited(verify_record(), "failing_acs", [])),
    ],
)
def test_every_agent_stage_needs_the_entry_of_its_own_agent_in_all_gates_passed(run_cli, adopted, stage, role, make):
    write_code_s_run(adopted)
    put(adopted, stage, make(), agent=False)  # the record rewritten by hand: the entry of the agent no longer holds its hash
    problems = pl.check_all_gates_passed(pl.load_project(adopted), RUN, {"id": "reduce"})
    assert any(f"stage '{stage}': its record changed after plumbline:{role} stopped; run the {role} again" in p for p in problems), problems


# --- an agent's record edited after its stop does not count


def test_a_record_edited_after_its_agent_stopped_fails_the_gate(run_cli, adopted):
    begin(adopted)
    path = verify_now(adopted)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["commands"][0]["summary"] = "13 passed, plus a word"  # still a valid record, and still green
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 1
    assert "its record changed after plumbline:verifier stopped; run the verifier again" in result.stdout


def test_a_planners_spec_edited_after_it_stopped_fails_its_gate(run_cli, adopted):
    path = put(adopted, "plan", spec_record())
    record = spec_record()
    record["goal"] = "A goal that suits the change better."
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    assert "its record changed after plumbline:planner stopped; run the planner again" in gate(run_cli, adopted, "plan").stdout


def test_a_stage_without_a_gate_is_traced_too_at_pass(run_cli, repo):
    adopt(repo)
    write_code_s_run(repo)
    path = run_path(repo, RUN, "build.json")
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    result = do_pass(run_cli, repo)
    assert result.returncode == 1 and "stage 'build': its record changed after plumbline:builder stopped; run the builder again" in result.stdout


# --- SS-HANDFIX: a record the hook marked invalid is not made valid by editing it


def test_a_record_the_agent_left_invalid_is_not_mended_by_hand(run_cli, adopted):  # SS-HANDFIX
    begin(adopted)
    path = run_path(adopted, RUN, "verify.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    agent_stopped(adopted, "verify", path, "verifier", "verify_record", valid=False)  # blocked three times, let go, marked invalid
    put(adopted, "verify", verify_record(diff=now_hash(adopted)), agent=False)  # the main session then writes a valid record by hand
    entry = [e for e in ledger(adopted) if e["kind"] == "agent"][-1]
    assert entry["valid"] is False
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 1
    assert "plumbline:verifier's last stop left its record invalid; run the verifier again" in result.stdout


def test_the_latest_entry_of_a_stage_counts_not_an_earlier_valid_one(run_cli, adopted):
    begin(adopted)
    path = verify_now(adopted)  # a valid stop
    agent_stopped(adopted, "verify", path, "verifier", "verify_record", valid=False)  # and a later stop that left the record invalid
    result = gate(run_cli, adopted, "verify")
    assert result.returncode == 1 and "plumbline:verifier's last stop left its record invalid; run the verifier again" in result.stdout


# --- AG-ROLE: work done by another kind of agent does not pass as a stage's record


def test_a_record_written_by_another_kind_of_agent_does_not_count(run_cli, adopted):  # AG-ROLE
    """The reviewer's route: give the builder's brief to a general-purpose agent, which no hook holds to the role's rules."""
    write_code_s_run(adopted)
    path = run_path(adopted, RUN, "build.json")
    agent_stopped(adopted, "build", path, "builder", "build_note")
    pl.append_ledger(
        adopted, RUN,
        {"kind": "agent", "agent_id": "g1", "agent_type": "general-purpose", "stage": "build", "record": ".plumbline/runs/r1/build.json", "record_sha256": pl.file_sha256(path), "valid": True},
    )
    problems = pl.check_all_gates_passed(pl.load_project(adopted), RUN, {"id": "reduce"})
    assert any("stage 'build': the latest entry for its record is from general-purpose, not plumbline:builder; run the builder" in p for p in problems), problems


def test_a_planner_entry_does_not_vouch_for_a_verifiers_record(run_cli, adopted):
    begin(adopted)
    path = put(adopted, "verify", verify_record(diff=now_hash(adopted)), agent=False)
    agent_stopped(adopted, "verify", path, "planner", "spec")
    assert "the latest entry for its record is from plumbline:planner, not plumbline:verifier" in gate(run_cli, adopted, "verify").stdout


# --- the review: merge-review's entry, and each part's own agent


def review_parts(repo, findings=None, round_no=1):
    for lens in ("correctness", "tests"):
        put_part(repo, "review", f"prosecutor-{lens}", {"lens": lens, "findings": (findings or {}).get(lens, [])}, round_no)
    for k in (1, 2, 3):
        put_part(repo, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []}, round_no)


@pytest.fixture
def reviewed(adopted):
    """A code.S run with the review's parts on disk (each with its agent's entry), not yet merged."""
    begin(adopted, "code.S")
    write(adopted / "src" / "app.py", "def main():\n    return 2\n")
    review_parts(adopted)
    return adopted


def merge(run_cli, repo, *extra):
    return run_cli("merge-review", RUN, "review", *extra, cwd=repo)


def test_a_review_record_written_by_hand_has_no_merge_entry_and_fails_the_gate(run_cli, reviewed):
    put(reviewed, "review", review_record(diff=now_hash(reviewed)), agent=False)
    result = gate(run_cli, reviewed, "review")
    assert result.returncode == 1 and "its record has no entry from merge-review; run `plumbline.py merge-review r1 review`" in result.stdout


def test_merge_review_writes_an_entry_with_the_records_hash_the_round_and_the_parts(run_cli, reviewed):
    assert merge(run_cli, reviewed).returncode == 0
    [entry] = [e for e in ledger(reviewed) if e["kind"] == "merge"]
    assert (entry["stage"], entry["round"], entry["record"]) == ("review", 1, ".plumbline/runs/r1/review.json")
    assert entry["record_sha256"] == pl.file_sha256(run_path(reviewed, RUN, "review.json"))
    # the parts in the order the survival rule reads them: the prosecutors (one per lens), the defenders, the detective
    paths = [f".plumbline/runs/r1/review/round-1/{name}.json" for name in ("prosecutor-correctness", "prosecutor-tests", "defender-1", "defender-2", "defender-3")]
    assert entry["parts"] == [{"path": path, "sha256": pl.file_sha256(reviewed / path)} for path in paths]
    assert gate(run_cli, reviewed, "review").returncode == 0


def test_a_review_record_edited_after_merge_review_fails_the_gate(run_cli, reviewed):
    merge(run_cli, reviewed)
    path = run_path(reviewed, RUN, "review.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["gaps"] = [{"id": "G-1", "kind": "edge_case", "detail": "added by hand", "ac": None}]
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    result = gate(run_cli, reviewed, "review")
    assert result.returncode == 1 and "its record changed after merge-review wrote it; run `plumbline.py merge-review r1 review` again" in result.stdout


def test_a_part_edited_after_the_merge_fails_the_gate(run_cli, reviewed):
    merge(run_cli, reviewed)
    part = run_path(reviewed, RUN, "review", "round-1", "defender-1.json")
    record = json.loads(part.read_text(encoding="utf-8"))
    record["defenses"] = [{"finding_id": "x", "defender": "defender-1", "verdict": "refuted", "quote": "return 2 return 2", "reason": "added"}]
    part.write_text(json.dumps(record), encoding="utf-8")
    result = gate(run_cli, reviewed, "review")
    assert result.returncode == 1 and ".plumbline/runs/r1/review/round-1/defender-1.json changed after merge-review read it" in result.stdout


def test_merge_review_refuses_a_part_no_agent_left(run_cli, reviewed):
    hand_written = json.dumps({"defender": "defender-1", "defenses": [], "diff_sha256": now_hash(reviewed)})  # valid, but not the bytes the agent left
    put_part(reviewed, "review", "defender-1", hand_written, agent=False)
    result = merge(run_cli, reviewed)
    assert result.returncode == 1
    assert ".plumbline/runs/r1/review/round-1/defender-1.json changed after plumbline:defender stopped; run the defender again" in result.stdout
    assert not run_path(reviewed, RUN, "review.json").exists()


def test_merge_review_refuses_a_part_that_has_no_entry_at_all(run_cli, adopted):
    begin(adopted, "code.S")
    for lens in ("correctness", "tests"):
        put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []}, agent=(lens == "correctness"))
    result = merge(run_cli, adopted)
    assert result.returncode == 1 and "prosecutor-tests.json has no entry from plumbline:prosecutor; run the prosecutor" in result.stdout


def test_a_part_of_the_wrong_kind_of_agent_is_refused(run_cli, adopted):
    begin(adopted, "code.S")
    for lens in ("correctness", "tests"):
        path = put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []}, agent=False)
        agent_stopped(adopted, "review", path, "detective" if lens == "tests" else "prosecutor", "findings_record")
    result = merge(run_cli, adopted)
    assert result.returncode == 1 and "the latest entry for .plumbline/runs/r1/review/round-1/prosecutor-tests.json is from plumbline:detective, not plumbline:prosecutor" in result.stdout


def test_a_part_the_hook_marked_invalid_is_refused(run_cli, adopted):
    begin(adopted, "code.S")
    for lens in ("correctness", "tests"):
        path = put_part(adopted, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []}, agent=False)
        agent_stopped(adopted, "review", path, "prosecutor", "findings_record", valid=lens != "tests")
    result = merge(run_cli, adopted)
    assert result.returncode == 1 and "plumbline:prosecutor's last stop left .plumbline/runs/r1/review/round-1/prosecutor-tests.json invalid" in result.stdout


def test_a_stage_without_defenders_needs_no_defender_entries(run_cli, adopted):
    begin(adopted, "code.M")
    put_part(adopted, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    put_part(adopted, "test-review", "defender-1", {"defender": "defender-1", "defenses": []}, agent=False)  # ignored by this stage: nothing vouches for it
    assert run_cli("merge-review", RUN, "test-review", cwd=adopted).returncode == 0


def test_the_detectives_part_is_traced_like_the_others(run_cli, reviewed):
    put_part(reviewed, "review", "detective", {"gaps": []}, agent=False)
    result = merge(run_cli, reviewed)
    assert result.returncode == 1 and "detective.json has no entry from plumbline:detective; run the detective" in result.stdout
    put_part(reviewed, "review", "detective", {"gaps": []})
    assert merge(run_cli, reviewed).returncode == 0


def test_all_gates_passed_repeats_the_review_provenance(run_cli, adopted):
    write_docs_run(adopted)
    for stage in ("verify", "review"):
        assert gate(run_cli, adopted, stage).returncode == 0
    assert gate(run_cli, adopted, "reduce").returncode == 0
    path = run_path(adopted, RUN, "review.json")
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    result = gate(run_cli, adopted, "reduce")
    assert result.returncode == 1 and "stage 'review': its record changed after merge-review wrote it" in result.stdout


# --- supplied records and the intake record are traced too


def spec_file(repo):
    write(repo / ".plumbline" / "supplied-spec.json", json.dumps(spec_record()))
    return ".plumbline/supplied-spec.json"


def test_a_supplied_record_edited_after_it_was_supplied_fails(run_cli, adopted):
    result = run_cli("plan", "--intent", "spec-supplied", "--spec", spec_file(adopted), "--row", "code.S", "--run-id", RUN, cwd=adopted)
    assert result.returncode == 0, result.stderr
    assert gate(run_cli, adopted, "plan").returncode == 0
    path = run_path(adopted, RUN, "plan.json")
    record = spec_record()
    record["goal"] = "Edited afterwards."
    path.write_text(json.dumps(record), encoding="utf-8")
    result = gate(run_cli, adopted, "plan")
    assert result.returncode == 1 and "its record changed after `plan --intent` supplied it" in result.stdout


def test_a_supplied_stage_with_no_supplied_entry_fails(run_cli, adopted):
    run_cli("plan", "--intent", "spec-supplied", "--spec", spec_file(adopted), "--row", "code.S", "--run-id", RUN, cwd=adopted)
    ledger_path = run_path(adopted, RUN, "ledger.jsonl")
    rows = [line for line in ledger_path.read_text(encoding="utf-8").splitlines() if '"kind": "supplied"' not in line]
    ledger_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert "its record has no entry from `plan --intent`, which supplies it" in gate(run_cli, adopted, "plan").stdout


def test_an_intake_record_edited_after_the_run_began_is_refused(run_cli, adopted):
    """The intake record names the row, the intent and the merge base every later check measures from."""
    write(adopted / "src" / "new.py", "x = 1\n")
    assert run_cli("plan", "--intent", "feature", "--row", "code.S", "--run-id", RUN, cwd=adopted).returncode == 0
    path = run_path(adopted, RUN, "intake.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["intent"] = "review-only"  # skip plan, tests and build after the fact
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    for command in (("gate", RUN, "reduce"), ("pass", RUN), ("merge-review", RUN, "review"), ("check-diff", "--run", RUN)):
        result = run_cli(*command, cwd=adopted)
        assert result.returncode == 2, command
        assert "the run's intake record changed after `plan --intent` wrote it" in result.stderr, command
    assert "the run's intake record changed after `plan --intent` wrote it" in run_cli("status", cwd=adopted).stdout


def test_a_merge_base_moved_in_the_intake_record_is_refused(run_cli, adopted):
    write(adopted / "src" / "new.py", "x = 1\n")
    run_cli("plan", "--intent", "feature", "--row", "code.S", "--run-id", RUN, cwd=adopted)
    path = run_path(adopted, RUN, "intake.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["merge_base"] = git(adopted, "rev-parse", "HEAD").strip()  # a smaller change to hash and to review
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    result = run_cli("check-diff", "--run", RUN, cwd=adopted)
    assert result.returncode == 2 and "changed after `plan --intent` wrote it" in result.stderr


def test_an_intake_record_with_no_ledger_entry_is_refused(run_cli, adopted):
    begin(adopted)
    put(adopted, "intake", intake_record("docs", adopted), agent=False)
    ledger_path = run_path(adopted, RUN, "ledger.jsonl")
    ledger_path.write_text("", encoding="utf-8")
    result = gate(run_cli, adopted, "reduce")
    assert result.returncode == 2 and "the run's intake record has no entry from `plan --intent` in the ledger" in result.stderr


def test_plan_intent_enters_the_intake_record_before_the_file_exists_so_that_a_failed_start_can_be_redone(run_cli, adopted, monkeypatch):
    write(adopted / "src" / "new.py", "x = 1\n")
    assert run_cli("plan", "--intent", "feature", "--row", "code.S", "--run-id", RUN, cwd=adopted).returncode == 0
    [entry] = [e for e in ledger(adopted) if e["kind"] == "intake"]
    assert entry["record_sha256"] == pl.file_sha256(run_path(adopted, RUN, "intake.json"))
    assert (entry["intent"], entry["row"]) == ("feature", "code.S") and entry["merge_base"] == read(adopted, "intake")["merge_base"]


# --- the push gate and status apply it too


def test_a_newer_invalid_stop_of_the_verifier_after_a_pass_uncovers_head(run_pre, adopted):
    genuine_pass(adopted)
    assert push_is_denied(run_pre, adopted) is None
    agent_stopped(adopted, "verify", run_path(adopted, RUN, "verify.json"), "verifier", "verify_record", valid=False)
    reason = push_is_denied(run_pre, adopted)
    assert reason and "stage 'verify': plumbline:verifier's last stop left its record invalid; run the verifier again" in reason


def test_status_shows_a_record_nobody_wrote_as_failing_and_says_why(run_cli, adopted):
    begin(adopted)
    put(adopted, "verify", verify_record(diff=now_hash(adopted)), agent=False)
    out = run_cli("status", cwd=adopted).stdout
    line = next(line for line in out.splitlines() if line.startswith("  verify"))
    assert line.split()[2] == "FAIL"
    assert "its record has no entry from plumbline:verifier; run the verifier" in out


def test_a_genuine_run_still_passes_with_every_record_traced(run_cli, adopted):
    write_docs_run(adopted)
    result = do_pass(run_cli, adopted)
    assert result.returncode == 0, result.stdout
