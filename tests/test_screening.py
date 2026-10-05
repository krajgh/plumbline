"""Screening defenders: the full defender panel answers a round only when a prosecutor filed a BLOCKING finding; otherwise the stage's
`screen_defenders` screening defenders answer every finding, and `merge-review` keeps the step that a claim of BLOCKING asks for in the record."""
import json

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import REPO, default_pipeline, write
from hookdata import start_run, tool_payload
from rundata import RUN, adopt, intake_record, put, put_part, read, run_path
from samples import sample
from test_agents import agent
from test_manifests import between, frontmatter
from test_pipeline import check, stage
from test_readme import section

PANEL_FIX = "run the full panel for it in this round"


@pytest.fixture
def unit(repo):
    """An adopted repository with a run of row code.S: its review stage has 3 defenders, a threshold of 2 and 1 screening defender.
    src/app.py holds the line the defenders quote."""
    adopt(repo)
    put(repo, "intake", intake_record("code.S", repo))
    write(repo / "src" / "app.py", "def main():\n    return retry(url)\n")
    return repo


def finding(fid, severity="MINOR", lens="correctness"):
    return {
        "id": fid, "lens": lens, "file": "src/app.py", "line": 14, "claim": f"claim {fid}", "failure_scenario": "a concrete input",
        "rule": "AC-1", "evidence": "except OSError: continue", "outside_code": None, "severity": severity,
    }


def entry(fid, who, verdict="conceded", claim=None, quote="return retry(url)"):
    made = {"finding_id": fid, "defender": who, "verdict": verdict, "quote": quote, "reason": f"{who} says {verdict}"}
    if claim:
        made["severity_claim"] = claim
    return made


def file_findings(repo, findings, round_no=1):
    put_part(repo, "review", "prosecutor-correctness", {"lens": "correctness", "findings": findings}, round_no)
    put_part(repo, "review", "prosecutor-tests", {"lens": "tests", "findings": []}, round_no)


def screen(repo, *entries, k=1, round_no=1):
    """The record of screening defender `screen-<k>`."""
    put_part(repo, "review", f"screen-{k}", {"defender": f"screen-{k}", "defenses": list(entries)}, round_no)


def panel(repo, fid, outcomes, round_no=1):
    """The full panel, defender-1 to defender-3, on one finding. Each outcome is None (concedes), a severity (concedes and claims it) or "refuted"."""
    for n, outcome in enumerate(outcomes, 1):
        who = f"defender-{n}"
        verdict = "refuted" if outcome == "refuted" else "conceded"
        put_part(repo, "review", who, {"defender": who, "defenses": [entry(fid, who, verdict, None if outcome in (None, "refuted") else outcome)]}, round_no)


def merge(run_cli, repo, *extra):
    return run_cli("merge-review", RUN, "review", *extra, cwd=repo)


def record_of(repo):
    return read(repo, "review")


def by_id(repo):
    return {f["id"]: f for f in record_of(repo)["findings"]}


def review_stage(project):
    return next(s for s in project.pipeline["stage"] if s["id"] == "review")


# --- the pipeline


def test_the_default_review_stage_has_one_screening_defender_and_the_other_review_stages_have_none():
    stages = {s["id"]: s for s in default_pipeline()["stage"]}
    assert stages["review"]["screen_defenders"] == 1 and stages["review"]["defenders"] == 3
    assert "screen_defenders" not in stages["test-review"]  # it has no defenders at all


@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_screen_defenders_is_an_integer_from_0_up_to_defenders(value):
    assert check(lambda d: stage(d, "review").update(screen_defenders=value)) == ([], [])


REVIEW_AT = [s["id"] for s in default_pipeline()["stage"]].index("review")  # where the review stage stands in $.stage


# (id, mutation, text of an error)
BAD_SCREENING = [
    ("above-defenders", lambda d: stage(d, "review").update(screen_defenders=4), "stage 'review': screen_defenders cannot exceed defenders"),
    ("without-defenders", lambda d: (stage(d, "review").pop("defenders"), stage(d, "review").pop("survive_if_unrefuted_by")), "stage 'review': screen_defenders needs defenders"),
    ("negative", lambda d: stage(d, "review").update(screen_defenders=-1), f"$.stage[{REVIEW_AT}].screen_defenders"),
    ("not-an-integer", lambda d: stage(d, "review").update(screen_defenders="one"), f"$.stage[{REVIEW_AT}].screen_defenders: expected integer, got string"),
    ("a-float", lambda d: stage(d, "review").update(screen_defenders=1.5), f"$.stage[{REVIEW_AT}].screen_defenders"),
    ("a-boolean", lambda d: stage(d, "review").update(screen_defenders=True), f"$.stage[{REVIEW_AT}].screen_defenders"),
    ("on-an-agent-stage", lambda d: stage(d, "build").update(screen_defenders=1), "stage 'build': 'screen_defenders' only applies to review stages"),
]


@pytest.mark.parametrize("mutate,expected", [(m, e) for _, m, e in BAD_SCREENING], ids=[i for i, _, _ in BAD_SCREENING])
def test_a_bad_screen_defenders_is_an_error(mutate, expected):
    errors, _ = check(mutate)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_the_plan_carries_screen_defenders_for_each_review_stage(run_cli, repo):
    write(repo / "src" / "new_module.py", "x = 1\n" * 100)
    result = run_cli("plan", "--base", "main", "--run-id", "demo", cwd=repo)
    stages = {s["id"]: s for s in json.loads(result.stdout)["stages"]}
    assert stages["review"]["screen_defenders"] == 1 and stages["test-review"]["screen_defenders"] is None
    assert stages["plan"].get("screen_defenders") is None and "screen_defenders" not in stages["build"]  # only a review stage prints it


# --- the record


def test_panel_needed_is_an_optional_list_of_finding_ids_in_the_review_record():
    schema = pl.load_schema("review_record")
    assert schema["properties"]["panel_needed"]["type"] == "array" and "panel_needed" not in schema["required"]
    record = sample("review_record")
    assert record["panel_needed"] == [] and pl.check_record("review_record", record) == []
    record["panel_needed"] = ["F-2"]
    assert pl.check_record("review_record", record) == []
    record["panel_needed"] = [7]
    assert any(e.startswith("$.panel_needed[0]:") for e in pl.check_record("review_record", record))
    del record["panel_needed"]  # a record from 0.4.2
    assert pl.check_record("review_record", record) == []


# --- a round that only screening defenders answered


def test_a_finding_a_screening_defender_conceded_stands_and_the_round_needs_no_panel(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR"), finding("correctness-2", "MINOR")])
    screen(unit, entry("correctness-1", "screen-1"), entry("correctness-2", "screen-1"))
    result = merge(run_cli, unit)
    assert result.returncode == 0, result.stdout + result.stderr
    record = record_of(unit)
    assert record["survivors"] == ["correctness-1", "correctness-2"] and record["panel_needed"] == [] and record["blockers_surviving"] == 0
    assert [d["defender"] for d in record["defenses"]] == ["screen-1", "screen-1"]
    assert result.stderr == "" and "panel needed" not in result.stdout  # one screener of one: nothing is missing
    assert run_cli("gate", RUN, "review", cwd=unit).returncode == 0
    assert pl.check_record("review_record", record) == []


def test_a_finding_a_screening_defender_refuted_with_a_valid_quote_does_not_stand(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR"), finding("correctness-2", "MINOR")])
    screen(unit, entry("correctness-1", "screen-1", "refuted"), entry("correctness-2", "screen-1"))
    assert merge(run_cli, unit).returncode == 0
    assert record_of(unit)["survivors"] == ["correctness-2"]  # the panel's threshold of 2 does not apply: one screening defender is enough


def test_a_refutation_by_a_screening_defender_needs_a_valid_quote_like_any_other(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR"), finding("correctness-2", "MAJOR"), finding("correctness-3", "MAJOR")])
    screen(
        unit,
        entry("correctness-1", "screen-1", "refuted", quote=""),
        entry("correctness-2", "screen-1", "refuted", quote="x = 1"),  # too short
        entry("correctness-3", "screen-1", "refuted", quote="return somewhere_else(url)"),  # in neither the diff nor the file
    )
    result = merge(run_cli, unit)
    assert record_of(unit)["survivors"] == ["correctness-1", "correctness-2", "correctness-3"]
    assert "defender 'screen-1' refuted 'correctness-1' without quoting code, which does not count" in result.stderr
    assert "defender 'screen-1' refuted 'correctness-2' with a quote of fewer than 6 characters, which does not count" in result.stderr
    assert "defender 'screen-1' refuted 'correctness-3' with a quote that is in neither the change nor src/app.py, which does not count" in result.stderr


def test_a_finding_no_screening_defender_mentions_stands(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR"), finding("correctness-2", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1", "refuted"))  # silent on correctness-2
    assert merge(run_cli, unit).returncode == 0
    assert record_of(unit)["survivors"] == ["correctness-2"]


def test_a_screening_defenders_claim_of_major_raises_a_minor_finding_to_major(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    screen(unit, entry("correctness-1", "screen-1", claim="MAJOR"))
    result = merge(run_cli, unit)
    found = by_id(unit)["correctness-1"]
    assert (found["severity"], found["severity_raised_from"]) == ("MAJOR", "MINOR")
    assert record_of(unit)["blockers_surviving"] == 0 and record_of(unit)["panel_needed"] == []
    assert "severity raised: correctness-1 from MINOR to MAJOR (claimed by screen-1)" in result.stdout
    assert run_cli("gate", RUN, "review", cwd=unit).returncode == 0  # MAJOR is no blocker


def test_a_screening_defenders_claim_of_blocking_raises_nothing_and_asks_for_the_panel(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR"), finding("correctness-2", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1", claim="BLOCKING"), entry("correctness-2", "screen-1", claim="BLOCKING"))
    result = merge(run_cli, unit)
    assert result.returncode == 0
    record = record_of(unit)
    assert [f["severity"] for f in record["findings"]] == ["MINOR", "MAJOR"] and not any("severity_raised_from" in f for f in record["findings"])
    assert record["panel_needed"] == ["correctness-1", "correctness-2"] and record["blockers_surviving"] == 0
    assert record["survivors"] == ["correctness-1", "correctness-2"]
    assert "panel needed for correctness-1, correctness-2: run the full panel for them in this round, then run merge-review for the same round again" in result.stdout
    assert pl.check_record("review_record", record) == []


def test_the_gate_fails_while_a_finding_needs_the_panel_and_says_which(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR"), finding("correctness-2", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1", claim="BLOCKING"), entry("correctness-2", "screen-1"))
    merge(run_cli, unit)
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1
    assert f"  - the screening defender claims finding correctness-1 is BLOCKING; {PANEL_FIX}" in gate.stdout
    assert "correctness-2" not in gate.stdout and "blocker(s) survive" not in gate.stdout  # nothing else is wrong
    assert "round 1 of 3" in gate.stdout and "has used its rounds" not in gate.stdout


def test_a_failure_that_the_panel_settles_opens_no_round_and_uses_none_up_even_on_the_last_round(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")], round_no=3)
    screen(unit, entry("correctness-1", "screen-1", claim="BLOCKING"), round_no=3)
    assert merge(run_cli, unit).returncode == 0  # the highest round directory is round-3
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1, gate.stdout  # not 3: the stage has not run out of rounds, it has yet to hear the panel
    assert "round 3 of 3" in gate.stdout and "has used its rounds" not in gate.stdout and PANEL_FIX in gate.stdout
    assert not run_path(unit, RUN, "review", "round-4").exists()


def test_a_failure_with_the_panel_still_to_come_opens_no_next_round_even_when_a_blocker_stands(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "BLOCKING")])
    screen(unit, entry("correctness-1", "screen-1"))  # a finding filed BLOCKING, answered only by a screening defender
    merge(run_cli, unit)
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1 and "1 blocker(s) survive: correctness-1" in gate.stdout
    assert f"finding correctness-1 was filed BLOCKING and only a screening defender answered it; {PANEL_FIX}" in gate.stdout
    assert not run_path(unit, RUN, "review", "round-2").exists()  # the panel writes in this round: round-2 would take its records


def test_a_finding_filed_blocking_that_only_a_screening_defender_answered_cannot_be_ended_by_it(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "BLOCKING")])
    screen(unit, entry("correctness-1", "screen-1", "refuted"))  # a valid quote, but only the panel ends a BLOCKING finding
    result = merge(run_cli, unit)
    record = record_of(unit)
    assert record["survivors"] == ["correctness-1"] and record["panel_needed"] == ["correctness-1"] and record["blockers_surviving"] == 1
    assert "panel needed for correctness-1" in result.stdout


def test_a_claim_equal_to_or_below_the_filed_severity_is_ignored_with_a_warning_for_a_screening_defender_too(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR"), finding("correctness-2", "MINOR")])
    screen(unit, entry("correctness-1", "screen-1", claim="MAJOR"), entry("correctness-2", "screen-1", claim="MINOR"))
    result = merge(run_cli, unit)
    assert {f["severity"] for f in record_of(unit)["findings"]} == {"MAJOR", "MINOR"} and record_of(unit)["panel_needed"] == []
    assert "defender 'screen-1' claims MAJOR for 'correctness-1', which is not above the MAJOR the prosecutor filed, so the claim is ignored" in result.stderr
    assert "defender 'screen-1' claims MINOR for 'correctness-2', which is not above the MINOR the prosecutor filed, so the claim is ignored" in result.stderr


def test_a_screening_defender_that_refutes_a_finding_leaves_no_claim_to_act_on(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    screen(unit, entry("correctness-1", "screen-1", "refuted"))
    merge(run_cli, unit)
    record = record_of(unit)
    assert record["survivors"] == [] and record["panel_needed"] == []


# --- escalation: the panel's records cover a finding that needs it


def escalated(run_cli, repo, outcomes, filed="MINOR", claim="BLOCKING"):
    """A round whose screening defender claimed `claim` for a finding filed at `filed`, then the full panel answered with `outcomes`, merged again."""
    file_findings(repo, [finding("correctness-1", filed)])
    screen(repo, entry("correctness-1", "screen-1", claim=claim))
    first = merge(run_cli, repo)
    assert first.returncode == 0 and record_of(repo)["panel_needed"] == ["correctness-1"], first.stdout
    panel(repo, "correctness-1", outcomes)
    second = merge(run_cli, repo)
    assert second.returncode == 0, second.stdout + second.stderr
    return second


def test_a_panel_that_concedes_without_claims_settles_the_finding_at_its_filed_severity(run_cli, unit):
    escalated(run_cli, unit, [None, None, None])
    record = record_of(unit)
    assert record["panel_needed"] == [] and record["survivors"] == ["correctness-1"]
    assert by_id(unit)["correctness-1"]["severity"] == "MINOR"  # the screening defender's claim does not carry into the panel's decision
    assert run_cli("gate", RUN, "review", cwd=unit).returncode == 0


def test_a_panel_that_refutes_a_finding_ends_it_by_the_panels_threshold(run_cli, unit):
    escalated(run_cli, unit, ["refuted", "refuted", None])
    record = record_of(unit)
    assert record["panel_needed"] == [] and record["survivors"] == [] and record["blockers_surviving"] == 0
    assert run_cli("gate", RUN, "review", cwd=unit).returncode == 0


def test_one_refutation_in_three_does_not_end_a_finding_the_panel_answered(run_cli, unit):
    escalated(run_cli, unit, ["refuted", None, None])
    assert record_of(unit)["survivors"] == ["correctness-1"]  # at a threshold of 2, one refutation is not enough


def test_a_panel_that_claims_blocking_raises_the_finding_and_it_blocks(run_cli, unit):
    result = escalated(run_cli, unit, ["BLOCKING", "BLOCKING", None])
    record = record_of(unit)
    found = by_id(unit)["correctness-1"]
    assert (found["severity"], found["severity_raised_from"]) == ("BLOCKING", "MINOR")
    assert record["panel_needed"] == [] and record["blockers_surviving"] == 1
    assert "severity raised: correctness-1 from MINOR to BLOCKING (claimed by defender-1, defender-2)" in result.stdout
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1 and "1 blocker(s) survive: correctness-1" in gate.stdout and PANEL_FIX not in gate.stdout


def test_one_claim_of_blocking_in_the_panel_is_not_enough_at_a_threshold_of_two(run_cli, unit):
    escalated(run_cli, unit, ["BLOCKING", None, None])
    assert by_id(unit)["correctness-1"]["severity"] == "MINOR" and record_of(unit)["blockers_surviving"] == 0


def test_the_panel_decides_only_the_findings_it_covers_and_the_screening_defender_the_rest(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR"), finding("correctness-2", "MINOR"), finding("correctness-3", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1", claim="BLOCKING"), entry("correctness-2", "screen-1", claim="MAJOR"), entry("correctness-3", "screen-1", "refuted"))
    panel(unit, "correctness-1", [None, None, None])  # the panel answers the one finding that was sent to it
    assert merge(run_cli, unit).returncode == 0
    record = record_of(unit)
    assert record["panel_needed"] == [] and record["survivors"] == ["correctness-1", "correctness-2"]  # correctness-3 ended by the screening defender
    assert {fid: f["severity"] for fid, f in by_id(unit).items()} == {"correctness-1": "MINOR", "correctness-2": "MAJOR", "correctness-3": "MAJOR"}
    assert "severity_raised_from" in by_id(unit)["correctness-2"]  # the screening defender's claim of MAJOR still stands for what it alone answered


def test_a_finding_the_panel_has_yet_to_cover_stays_in_panel_needed(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR"), finding("correctness-2", "MINOR")])
    screen(unit, entry("correctness-1", "screen-1", claim="BLOCKING"), entry("correctness-2", "screen-1", claim="BLOCKING"))
    panel(unit, "correctness-1", [None, None, None])
    assert merge(run_cli, unit).returncode == 0
    assert record_of(unit)["panel_needed"] == ["correctness-2"]
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1 and "claims finding correctness-2" in gate.stdout and "correctness-1" not in gate.stdout


def test_a_full_panel_round_with_a_blocking_finding_has_no_panel_needed(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "BLOCKING"), finding("correctness-2", "MINOR")])
    for n in (1, 2, 3):
        who = f"defender-{n}"
        put_part(unit, "review", who, {"defender": who, "defenses": [entry("correctness-1", who), entry("correctness-2", who)]})
    result = merge(run_cli, unit)
    assert record_of(unit)["panel_needed"] == [] and record_of(unit)["blockers_surviving"] == 1 and result.stderr == ""


# --- a stage that screens with none, or does not screen at all


def test_with_screen_defenders_at_0_no_defender_runs_without_a_blocking_finding_and_every_finding_stands(unit):
    project = pl.load_project(unit)
    review_stage(project)["screen_defenders"] = 0
    file_findings(unit, [finding("correctness-1", "MAJOR"), finding("correctness-2", "MINOR")])
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert problems == [] and warnings == []  # nobody was expected, so nobody is missing
    assert record["survivors"] == ["correctness-1", "correctness-2"] and record["defenses"] == [] and record["panel_needed"] == []


def test_with_screen_defenders_at_0_a_blocking_finding_still_expects_the_panel(unit):
    project = pl.load_project(unit)
    review_stage(project)["screen_defenders"] = 0
    file_findings(unit, [finding("correctness-1", "BLOCKING")])
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert warnings == ["only 0 of 3 defenders reported; a missing defender did not refute anything"]
    assert record["survivors"] == ["correctness-1"] and record["blockers_surviving"] == 1


def test_a_stage_without_screen_defenders_expects_the_full_panel_in_every_round_as_before(unit):
    project = pl.load_project(unit)
    del review_stage(project)["screen_defenders"]
    file_findings(unit, [finding("correctness-1", "MINOR")])
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert warnings == ["only 0 of 3 defenders reported; a missing defender did not refute anything"]
    panel(unit, "correctness-1", ["refuted", "refuted", None])
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert problems == [] and warnings == [] and record["survivors"] == [] and record["panel_needed"] == []


def test_screen_records_are_ignored_with_a_warning_where_the_stage_has_no_screening_defenders(unit):
    project = pl.load_project(unit)
    review_stage(project)["screen_defenders"] = 0
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1", "refuted"))
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert problems == [] and "this stage has no screening defenders, so its screen records were ignored" in warnings
    assert record["survivors"] == ["correctness-1"] and record["defenses"] == []  # nothing the ignored record says counts


def test_a_missing_screening_defender_is_a_warning_and_too_many_are_a_problem(unit):
    project = pl.load_project(unit)
    review_stage(project)["screen_defenders"] = 2
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1"))
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert problems == [] and warnings == ["only 1 of 2 screening defenders reported; a missing screening defender did not refute anything"]
    screen(unit, entry("correctness-1", "screen-2"), k=2)
    screen(unit, entry("correctness-1", "screen-3"), k=3)
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert record is None and problems == ["3 screening defenders reported (screen-1, screen-2, screen-3) but the stage has 2"]


def test_a_screening_defender_and_a_panel_defender_may_not_share_a_name(unit):
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    put_part(unit, "review", "screen-1", {"defender": "defender-1", "defenses": [entry("correctness-1", "defender-1")]})  # a screen record whose defender goes by a panel defender's name
    panel(unit, "correctness-1", [None, None, None])
    record, problems, _ = pl.merge_review(pl.load_project(unit), RUN, "review")
    assert record is None and any("defender 'defender-1' defends finding 'correctness-1' more than once" in p for p in problems)


# --- what the ledger traces


def test_a_screen_record_is_a_part_of_the_merge_and_is_traced_to_the_defender_that_wrote_it(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    screen(unit, entry("correctness-1", "screen-1"))
    merge(run_cli, unit)
    merge_entry = next(e for e in pl.read_ledger(unit, RUN) if e["kind"] == "merge")
    assert ".plumbline/runs/r1/review/round-1/screen-1.json" in [part["path"] for part in merge_entry["parts"]]
    assert run_cli("gate", RUN, "review", cwd=unit).returncode == 0
    path = run_path(unit, RUN, "review", "round-1", "screen-1.json")
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1 and "round-1/screen-1.json changed after merge-review read it" in gate.stdout


def test_a_screen_record_by_a_hand_has_no_agent_behind_it(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    screen_data = {"defender": "screen-1", "defenses": [entry("correctness-1", "screen-1")]}
    put_part(unit, "review", "screen-1", screen_data, agent=False)
    result = merge(run_cli, unit)
    assert result.returncode == 1 and "screen-1.json has no entry from plumbline:defender; run the defender" in result.stdout


# --- the hook: a screening defender writes its own file

def write_by(repo, role, name):
    path = repo / ".plumbline" / "runs" / RUN / "review" / "round-1" / name
    return pre.decide(tool_payload(repo, "Write", {"file_path": str(path), "content": "{}"}, agent_type=f"plumbline:{role}"))


def test_a_defender_may_write_a_screen_record_in_the_current_round_and_no_other_review_role_may(repo):
    adopt(repo)
    start_run(repo)
    assert write_by(repo, "defender", "screen-1.json") is None and write_by(repo, "defender", "defender-1.json") is None
    for role in ("prosecutor", "detective"):
        assert write_by(repo, role, "screen-1.json"), role
    denied = write_by(repo, "defender", "detective.json")
    assert denied and "round-<n>/defender-<n>.json or screen-<k>.json" in denied  # the denial says what the defender writes
    assert write_by(repo, "defender", "screening.json")  # the file name is screen-<k>, not any name that starts alike


def test_the_review_file_names_of_the_hook_cover_the_screen_record():
    assert pre.REVIEW_FILE_NAMES["defender"] == ("defender-<n>.json", "screen-<k>.json")
    assert pl.SCREEN_RECORD.fullmatch("screen-1") and not pl.SCREEN_RECORD.fullmatch("defender-1") and not pl.SCREEN_RECORD.fullmatch("screening")


# --- the prompt, the skill and the README


def test_the_defender_prompt_names_the_screening_defender_and_the_findings_a_brief_may_name():
    body = agent("defender")[1]
    assert "or `screen-1` when your brief calls you a screening defender" in body
    assert "Your brief may name the findings to answer, as when the full panel answers only the findings a screening defender sent to it" in body
    assert "or for each finding your brief names when it names some" in body
    assert "A screening defender (`screen-<k>`) works the same way and answers alone" in body and "a `severity_claim` of `BLOCKING` sends the finding to the full panel" in body


def test_the_orchestrator_says_who_defends_and_what_a_claim_of_blocking_asks_for():
    body = frontmatter(REPO / "agents" / "orchestrator.md")[1]
    defenders = between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    for needed in (
        "**A BLOCKING finding was filed:** the full panel defends all the findings", "named `defender-1`, `defender-2`", "`.../defender-<k>.json`",
        "**No BLOCKING finding, and the stage has `screen_defenders`:** launch that many screening defenders instead", "named `screen-1`, `screen-2`", "`.../screen-<k>.json`",
        "a `severity_claim` of BLOCKING sends it to the full panel (step 3)", "**No BLOCKING finding, and `screen_defenders` is 0:** no defender runs, and every finding stands",
        "A stage that has no `screen_defenders` runs the full panel in every round",
    ):
        assert needed in defenders, needed
    merge_step = between(body, "3. `PLUMBLINE merge-review", "4. **Detective.**")
    for needed in (
        "it says \"panel needed\"", "the record's `panel_needed` lists those findings", "`gate` fails until the full panel has defended them", "in this round and not in the next",
        "tell it to answer only the findings of `panel_needed`", "Run `merge-review` for the same round again",
    ):
        assert needed in merge_step, needed
    assert "its `panel_needed` is empty, launch `plumbline:detective`" in between(body, "4. **Detective.**", "5. `PLUMBLINE gate")
    assert "goes back to no stage" in between(body, "## When a gate fails", "## A decision comes back") and "`gate` counts no round for it and opens none" in body


def test_the_readme_describes_the_screening_defenders_and_the_files_they_write():
    pipeline_file = section("The pipeline file")
    assert "`screen_defenders` (how many screening defenders answer a round in which no prosecutor filed a BLOCKING finding: an integer from 0 up to `defenders`, 1 in the default pipeline" in pipeline_file
    text = section("Runs, gates and the pass record")
    assert "`screen-<k>.json` (a screening defender's)" in text
    paragraph = text.split("**Screening defenders.**", 1)[1].split("\n\n", 1)[0]
    for needed in (
        "When any prosecutor filed a BLOCKING finding, the stage's `defenders` defend all the findings", "with `screen_defenders = 0` no defender runs and every finding stands",
        "A stage with no `screen_defenders` runs the full panel in every round", "it stands unless a screening defender refuted it with a valid quote (the panel's threshold is for the panel)",
        "a screening defender's `severity_claim` of MAJOR raises a MINOR finding to MAJOR", "a claim of BLOCKING raises nothing, and puts the finding in the record's `panel_needed` instead",
        "as does a finding that was filed BLOCKING and only a screening defender answered", "fails while `panel_needed` is not empty, with \"the screening defender claims finding X is BLOCKING; run the full panel for it in this round\"",
        "neither opens the next round for that failure nor counts it as a used round", "the severity claims included",
    ):
        assert needed in paragraph, needed
    assert "So does a gate that fails while a finding needs the full panel (`panel_needed`)" in text
    assert "`panel_needed` (the findings that need the full panel in this round)" in section("Records")
    assert "or, as a screening defender, `screen-<k>.json`" in section("Hooks")
