"""merge-review: a review stage's record from its prosecutors, defenders and detective, with the survival rule."""
import json

import pytest

import plumbline as pl
from helpers import write
from rundata import RUN, adopt, intake_record, put, put_part, read, run_path
from samples import sample


@pytest.fixture
def unit(repo):
    """An adopted repository with a run of row code.S: its review stage has the lenses correctness and tests, 3 defenders, a survival threshold of 2.
    src/app.py holds the line the defenders quote, `return retry(url)`: a quote counts only when the change or the finding's file has it."""
    adopt(repo)
    put(repo, "intake", intake_record("code.S", repo))
    write(repo / "src" / "app.py", "def main():\n    return retry(url)\n")
    return repo


def finding(fid, lens="correctness", severity="BLOCKING"):
    return {
        "id": fid, "lens": lens, "file": "src/app.py", "line": 14, "claim": f"claim {fid}", "failure_scenario": "a concrete input",
        "rule": "AC-1", "evidence": "except OSError: continue", "outside_code": None, "severity": severity,
    }


def prosecutors(repo, by_lens, round_no=1):
    """`by_lens` maps a lens to the findings of its prosecutor."""
    for lens, findings in by_lens.items():
        put_part(repo, "review", f"prosecutor-{lens}", {"lens": lens, "findings": findings}, round_no)


def defense(fid, defender, verdict, quote="return retry(url)"):
    return {"finding_id": fid, "defender": defender, "verdict": verdict, "quote": quote, "reason": f"{defender} says {verdict}"}


def defender(repo, name, *defenses, round_no=1):
    put_part(repo, "review", name, {"defender": name, "defenses": list(defenses)}, round_no)


def defenders(repo, fid, verdicts, round_no=1):
    """Three defenders d1..d3 with the given verdicts on finding `fid`."""
    for n, verdict in enumerate(verdicts, 1):
        defender(repo, f"d{n}", defense(fid, f"d{n}", verdict), round_no=round_no)


def blank_prosecutors(repo, findings=None, round_no=1):
    prosecutors(repo, {"correctness": findings or [], "tests": []}, round_no)


def merge(run_cli, repo, *extra, stage="review"):
    return run_cli("merge-review", RUN, stage, *extra, cwd=repo)


def merged(repo, stage="review"):
    return read(repo, stage)


# --- the survival rule at 2 of 3


@pytest.mark.parametrize(
    "verdicts,survives",
    [
        (["conceded", "conceded", "conceded"], True),
        (["refuted", "conceded", "conceded"], True),   # one refutation: two defenders did not refute, so it stands
        (["refuted", "refuted", "conceded"], False),   # two refutations: only one did not refute
        (["conceded", "refuted", "refuted"], False),
        (["refuted", "refuted", "refuted"], False),
    ],
)
def test_a_blocking_finding_survives_when_at_least_two_of_three_defenders_did_not_refute_it(run_cli, unit, verdicts, survives):
    blank_prosecutors(unit, [finding("correctness-1")])
    defenders(unit, "correctness-1", verdicts)
    result = merge(run_cli, unit)
    assert result.returncode == 0, result.stdout + result.stderr
    record = merged(unit)
    assert record["survivors"] == (["correctness-1"] if survives else [])
    assert record["blockers_surviving"] == (1 if survives else 0)
    assert pl.check_record("review_record", record) == []


def test_a_finding_no_defender_mentions_survives(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    defender(unit, "d1", defense("correctness-1", "d1", "refuted"))  # d2 and d3 are silent
    assert merge(run_cli, unit).returncode == 0
    assert merged(unit)["survivors"] == ["correctness-1"]  # refuted by one of three


def test_silent_defenders_do_not_refute_so_two_refutations_still_defeat_a_finding(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    defender(unit, "d1", defense("correctness-1", "d1", "refuted"))
    defender(unit, "d2", defense("correctness-1", "d2", "refuted"))
    result = merge(run_cli, unit)
    assert result.returncode == 0
    assert merged(unit)["survivors"] == []
    assert "only 2 of 3 defenders reported" in result.stderr


def test_a_refutation_without_a_quote_does_not_count(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    defender(unit, "d1", defense("correctness-1", "d1", "refuted", quote=""))
    defender(unit, "d2", defense("correctness-1", "d2", "refuted", quote="   "))
    defender(unit, "d3", defense("correctness-1", "d3", "conceded"))
    result = merge(run_cli, unit)
    assert merged(unit)["survivors"] == ["correctness-1"]
    assert "defender 'd1' refuted 'correctness-1' without quoting code" in result.stderr


def test_the_rule_applies_to_every_finding_and_blockers_surviving_counts_only_the_blocking_ones(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1", severity="BLOCKING"), finding("correctness-2", severity="MAJOR"), finding("correctness-3", severity="MINOR")])
    for n in (1, 2, 3):  # refute the MINOR one twice, leave the others standing
        defender(unit, f"d{n}", *[defense(f"correctness-{k}", f"d{n}", "refuted" if (k == 3 and n < 3) else "conceded") for k in (1, 2, 3)])
    assert merge(run_cli, unit).returncode == 0
    record = merged(unit)
    assert record["survivors"] == ["correctness-1", "correctness-2"]
    assert record["blockers_surviving"] == 1


def test_a_stage_whose_threshold_is_three_of_three_needs_every_defender_to_hold_out(unit):
    project = pl.load_project(unit)
    review = next(s for s in project.pipeline["stage"] if s["id"] == "review")
    review["survive_if_unrefuted_by"] = 3
    blank_prosecutors(unit, [finding("correctness-1")])
    defenders(unit, "correctness-1", ["refuted", "conceded", "conceded"])
    record, problems, _ = pl.merge_review(project, RUN, "review")
    assert problems == [] and record["survivors"] == []  # one refutation leaves only two who did not refute


def test_without_a_threshold_a_majority_of_the_defenders_must_fail_to_refute(unit):
    project = pl.load_project(unit)
    review = next(s for s in project.pipeline["stage"] if s["id"] == "review")
    review["defenders"] = 2
    del review["survive_if_unrefuted_by"]
    blank_prosecutors(unit, [finding("correctness-1")])
    defender(unit, "d1", defense("correctness-1", "d1", "refuted"))
    defender(unit, "d2", defense("correctness-1", "d2", "conceded"))
    record, problems, _ = pl.merge_review(project, RUN, "review")
    assert problems == [] and record["survivors"] == []  # a majority of 2 is 2; only one failed to refute


# --- what the record holds


def test_the_record_holds_target_round_lenses_findings_defenses_survivors_and_gaps(run_cli, unit):
    prosecutors(unit, {"correctness": [finding("correctness-1")], "tests": [finding("tests-1", "tests", "MAJOR")]})
    defenders(unit, "correctness-1", ["conceded", "conceded", "refuted"])
    put_part(unit, "review", "detective", {"gaps": [{"id": "G-1", "kind": "missing_test", "detail": "no test for retries=0", "ac": None}]})
    result = merge(run_cli, unit)
    assert result.returncode == 0
    assert "wrote .plumbline/runs/r1/review.json: round 1, 2 findings, 2 surviving (1 blocking), 1 gap" in result.stdout
    record = merged(unit)
    assert (record["target"], record["round"]) == ("diff", 1)
    assert record["lenses"] == ["correctness", "tests"]  # the code.S row's lenses, not the stage's five
    assert [f["id"] for f in record["findings"]] == ["correctness-1", "tests-1"]
    assert [(d["finding_id"], d["defender"]) for d in record["defenses"]] == [("correctness-1", "d1"), ("correctness-1", "d2"), ("correctness-1", "d3")]
    assert record["survivors"] == ["correctness-1", "tests-1"]
    assert [g["id"] for g in record["gaps"]] == ["G-1"]
    assert record["blockers_surviving"] == 1


def test_without_a_detective_record_there_are_no_gaps_yet(run_cli, unit):
    blank_prosecutors(unit)
    assert merge(run_cli, unit).returncode == 0
    assert merged(unit)["gaps"] == []


def test_merging_again_once_the_detective_has_run_adds_the_gaps(run_cli, unit):
    blank_prosecutors(unit)
    merge(run_cli, unit)
    put_part(unit, "review", "detective", {"gaps": [{"id": "G-1", "kind": "edge_case", "detail": "retries=0", "ac": None}]})
    assert merge(run_cli, unit).returncode == 0
    assert len(merged(unit)["gaps"]) == 1


def test_a_stage_without_defenders_lets_every_finding_survive_and_ignores_defense_records(run_cli, repo):
    adopt(repo)
    put(repo, "intake", intake_record("code.M", repo))
    put_part(repo, "test-review", "prosecutor-tests", {"lens": "tests", "findings": [finding("tests-1", "tests", "BLOCKING")]})
    put_part(repo, "test-review", "d1", {"defender": "d1", "defenses": [defense("tests-1", "d1", "refuted")]})
    result = merge(run_cli, repo, stage="test-review")
    assert result.returncode == 0, result.stdout
    record = merged(repo, "test-review")
    assert (record["target"], record["lenses"], record["survivors"], record["blockers_surviving"]) == ("tests", ["tests"], ["tests-1"], 1)
    assert "this stage has no defenders" in result.stderr


def test_the_merged_record_feeds_the_gate_and_renders(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    defenders(unit, "correctness-1", ["conceded", "conceded", "conceded"])
    merge(run_cli, unit)
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1 and "1 blocker(s) survive: correctness-1" in gate.stdout
    rendered = run_cli("render", run_path(unit, RUN, "review.json"), cwd=unit)
    assert rendered.returncode == 0 and "Blockers surviving: 1" in rendered.stdout


# --- rounds


def test_the_latest_round_is_merged_unless_one_is_asked_for(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")], round_no=1)
    blank_prosecutors(unit, [], round_no=2)
    assert merge(run_cli, unit).returncode == 0
    assert (merged(unit)["round"], merged(unit)["findings"]) == (2, [])
    assert merge(run_cli, unit, "--round", "1").returncode == 0
    assert (merged(unit)["round"], len(merged(unit)["findings"])) == (1, 1)


def test_only_round_directories_named_by_a_plain_number_count(run_cli, unit):
    blank_prosecutors(unit, round_no=1)
    put_part(unit, "review", "prosecutor-correctness", {"lens": "correctness", "findings": []}, round_no=1)
    stray = run_path(unit, RUN, "review", "round-007")
    stray.mkdir()
    (stray / "x.json").write_text("{}", encoding="utf-8")
    assert merge(run_cli, unit).returncode == 0 and merged(unit)["round"] == 1
    zero = merge(run_cli, unit, "--round", "0")
    assert zero.returncode == 2 and "the round must be 1 or more" in zero.stderr


def test_a_finding_that_carries_another_lens_than_its_prosecutor_is_a_warning_not_an_error(run_cli, unit):
    prosecutors(unit, {"correctness": [finding("correctness-1", lens="tests")], "tests": []})
    result = merge(run_cli, unit)
    assert result.returncode == 0
    assert "finding 'correctness-1' carries lens 'tests' but was written by the 'correctness' prosecutor" in result.stderr


def test_a_round_that_does_not_exist_and_a_unit_without_records_could_not_be_merged(run_cli, unit):
    no_records = merge(run_cli, unit)
    assert no_records.returncode == 2 and "no per-agent records for stage 'review'" in no_records.stderr
    blank_prosecutors(unit)
    no_round = merge(run_cli, unit, "--round", "3")
    assert no_round.returncode == 2 and "round-3" in no_round.stderr


# --- what makes a merge impossible


def problems_of(run_cli, repo, *extra, stage="review"):
    result = merge(run_cli, repo, *extra, stage=stage)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot be merged" in result.stdout
    assert not run_path(repo, RUN, f"{stage}.json").exists()  # nothing was written
    return result.stdout


def test_a_missing_lens_is_a_problem(run_cli, unit):
    prosecutors(unit, {"correctness": []})
    assert "no findings_record for lens 'tests'" in problems_of(run_cli, unit)


def test_a_lens_the_round_does_not_have_is_a_problem(run_cli, unit):
    prosecutors(unit, {"correctness": [], "tests": [], "security": []})
    assert "lens 'security' is not one of this round's lenses (correctness, tests)" in problems_of(run_cli, unit)


def test_two_records_for_one_lens_are_a_problem(run_cli, unit):
    blank_prosecutors(unit)
    put_part(unit, "review", "prosecutor-correctness-again", {"lens": "correctness", "findings": []})
    assert "a second findings_record for lens 'correctness'" in problems_of(run_cli, unit)


def test_a_finding_id_used_by_two_prosecutors_is_a_problem_not_silently_renumbered(run_cli, unit):
    prosecutors(unit, {"correctness": [finding("F-1")], "tests": [finding("F-1", "tests")]})
    assert "finding id 'F-1' is also used in" in problems_of(run_cli, unit)


def test_a_defense_of_an_unknown_finding_is_a_problem(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    defender(unit, "d1", defense("ghost-9", "d1", "refuted"))
    assert "a defense of unknown finding 'ghost-9'" in problems_of(run_cli, unit)


def test_a_defender_defending_a_finding_twice_is_a_problem(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    defender(unit, "d1", defense("correctness-1", "d1", "refuted"), defense("correctness-1", "d1", "conceded"))
    assert "defender 'd1' defends finding 'correctness-1' more than once" in problems_of(run_cli, unit)


def test_a_defense_entry_naming_another_defender_is_a_problem(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    put_part(unit, "review", "d1", {"defender": "d1", "defenses": [defense("correctness-1", "d2", "refuted")]})
    assert "an entry names defender 'd2' in the record of defender 'd1'" in problems_of(run_cli, unit)


def test_more_defenders_than_the_stage_has_is_a_problem(run_cli, unit):
    blank_prosecutors(unit, [finding("correctness-1")])
    for n in (1, 2, 3, 4):
        defender(unit, f"d{n}", defense("correctness-1", f"d{n}", "conceded"))
    assert "4 defenders reported (d1, d2, d3, d4) but the stage has 3" in problems_of(run_cli, unit)


def test_two_detective_records_are_a_problem(run_cli, unit):
    blank_prosecutors(unit)
    put_part(unit, "review", "detective-1", {"gaps": []})
    put_part(unit, "review", "detective-2", {"gaps": []})
    assert "2 gaps_records" in problems_of(run_cli, unit)


def test_an_invalid_record_is_a_problem_naming_its_file_and_the_first_error(run_cli, unit):
    blank_prosecutors(unit)
    bad = sample("findings_record")
    bad["findings"][0]["severity"] = "SEVERE"
    put_part(unit, "review", "prosecutor-bad", bad)
    out = problems_of(run_cli, unit)
    assert "round-1/prosecutor-bad.json: not a valid findings_record: $.findings[0].severity" in out


def test_a_file_that_is_not_json_is_a_problem(run_cli, unit):
    blank_prosecutors(unit)
    put_part(unit, "review", "broken", "{nope")
    assert "round-1/broken.json: not valid JSON" in problems_of(run_cli, unit)


def test_every_problem_is_listed_not_just_the_first(run_cli, unit):
    prosecutors(unit, {"correctness": [finding("F-1")]})  # no tests prosecutor
    put_part(unit, "review", "broken", "{nope")
    out = problems_of(run_cli, unit)
    assert "no findings_record for lens 'tests'" in out and "not valid JSON" in out
    assert "cannot be merged (2 problems)" in out


# --- misuse


def test_only_a_review_stage_of_the_runs_row_can_be_merged(run_cli, unit):
    not_review = merge(run_cli, unit, stage="build")
    assert not_review.returncode == 2 and "stage 'build' is not a review stage" in not_review.stderr
    unknown = merge(run_cli, unit, stage="ghost")
    assert unknown.returncode == 2 and "no stage 'ghost'" in unknown.stderr
    outside_row = merge(run_cli, unit, stage="test-review")  # code.S has no test-review
    assert outside_row.returncode == 2 and "not part of row code.S" in outside_row.stderr


def test_a_run_without_a_usable_intake_record_could_not_be_merged(run_cli, repo):
    adopt(repo)
    put(repo, "verify", {})  # the run directory exists, the intake record does not
    result = merge(run_cli, repo)
    assert result.returncode == 2 and "intake record is not usable" in result.stderr


def test_the_merged_file_is_valid_json_with_a_trailing_newline(run_cli, unit):
    blank_prosecutors(unit)
    merge(run_cli, unit)
    text = run_path(unit, RUN, "review.json").read_text(encoding="utf-8")
    assert text.endswith("}\n") and json.loads(text)["blockers_surviving"] == 0
