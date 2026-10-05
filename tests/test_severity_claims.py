"""A defender can claim a higher severity: a typed `severity_claim` on a conceded verdict, and the rule by which `merge-review` raises a finding
when enough defenders claim the same, or more."""
import json
import re
from types import SimpleNamespace

import pytest

import plumbline as pl
from helpers import REPO, write
from hookdata import stop_payload
from rundata import RUN, adopt, intake_record, put, put_part, read, run_path
from samples import sample
from test_agents import SEVERITY_CLAUSES, agent
from test_manifests import between, frontmatter
from test_readme import README, section


@pytest.fixture
def unit(repo):
    """An adopted repository with a run of row code.S: its review stage has 3 defenders and a survival threshold of 2.
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


def entry(fid, who, verdict="conceded", claim=None):
    """One defender's verdict on a finding; a conceded one may carry the severity the defender claims."""
    made = {"finding_id": fid, "defender": who, "verdict": verdict, "quote": "return retry(url)", "reason": f"{who} says {verdict}"}
    if claim:
        made["severity_claim"] = claim
    return made


def file_findings(repo, findings):
    put_part(repo, "review", "prosecutor-correctness", {"lens": "correctness", "findings": findings})
    put_part(repo, "review", "prosecutor-tests", {"lens": "tests", "findings": []})


def defend(repo, fid, outcomes):
    """Three defenders d1..d3. Each outcome is None (concedes), a severity (concedes and claims it) or "refuted"."""
    for n, outcome in enumerate(outcomes, 1):
        who = f"d{n}"
        verdict = "refuted" if outcome == "refuted" else "conceded"
        put_part(repo, "review", who, {"defender": who, "defenses": [entry(fid, who, verdict, None if outcome in (None, "refuted") else outcome)]})


def merge(run_cli, repo, stage="review"):
    return run_cli("merge-review", RUN, stage, cwd=repo)


def merged_finding(repo, fid="correctness-1"):
    return next(f for f in read(repo, "review")["findings"] if f["id"] == fid)


# --- the record


def test_a_conceded_verdict_may_carry_a_severity_claim_in_the_defense_record_and_in_the_review_record():
    for name in ("defense_record", "review_record"):
        schema = pl.load_schema(name)["properties"]["defenses"]["items"]
        assert schema["properties"]["severity_claim"]["enum"] == ["BLOCKING", "MAJOR", "MINOR"]
        assert "severity_claim" not in schema["required"]  # optional: a record from 0.4.2 has none
    record = sample("defense_record")
    record["defenses"][0]["severity_claim"] = "BLOCKING"
    assert pl.check_record("defense_record", record) == []
    record["defenses"][0]["severity_claim"] = "SEVERE"
    assert any(e.startswith("$.defenses[0].severity_claim:") and "is not one of" in e for e in pl.check_record("defense_record", record))


def test_a_severity_claim_on_a_refuted_verdict_is_not_valid():
    record = sample("defense_record")
    assert record["defenses"][1]["verdict"] == "refuted"
    record["defenses"][1]["severity_claim"] = "BLOCKING"
    assert pl.check_record("defense_record", record) == ["$.defenses[1].severity_claim: only a conceded verdict carries a severity claim (this one is refuted)"]
    review = sample("review_record")
    review["defenses"][1]["severity_claim"] = "MAJOR"  # the merged record holds the same rule
    assert pl.check_record("review_record", review) == ["$.defenses[1].severity_claim: only a conceded verdict carries a severity claim (this one is refuted)"]


def test_check_record_tells_a_defender_about_a_claim_on_a_refutation(run_cli, tmp_path):
    record = sample("defense_record")
    record["defenses"][1]["severity_claim"] = "BLOCKING"
    path = tmp_path / "defender-1.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = run_cli("check-record", "defense_record", path, cwd=tmp_path)
    assert result.returncode == 1 and "only a conceded verdict carries a severity claim" in result.stdout


def test_the_stop_hook_holds_a_defender_back_whose_record_claims_a_severity_on_a_refutation(run_stop, unit):
    record = sample("defense_record")
    record["defenses"][1]["severity_claim"] = "BLOCKING"
    write(run_path(unit, RUN, "review", "round-1", "defender-1.json"), json.dumps(record))
    result = run_stop(stop_payload(unit, "plumbline:defender", "Answered.\nRECORD: .plumbline/runs/r1/review/round-1/defender-1.json"), unit)
    assert result.returncode == 2 and "the record you named is not a valid defense_record" in result.stderr
    assert "$.defenses[1].severity_claim: only a conceded verdict carries a severity claim" in result.stderr


def test_a_finding_of_the_review_record_says_what_severity_it_was_filed_at_when_it_was_raised():
    items = pl.load_schema("review_record")["properties"]["findings"]["items"]
    assert items["properties"]["severity_raised_from"]["enum"] == ["MINOR", "MAJOR"]
    assert "severity_raised_from" not in items["required"]
    record = sample("review_record")
    record["findings"][1].update(severity="BLOCKING", severity_raised_from="MAJOR")
    assert pl.check_record("review_record", record) == []
    record["findings"][1]["severity_raised_from"] = "BLOCKING"  # nothing is raised from the top
    assert any(e.startswith("$.findings[1].severity_raised_from:") for e in pl.check_record("review_record", record))


def test_the_defense_and_the_review_record_describe_the_claim_alike():
    description = pl.load_schema("defense_record")["properties"]["defenses"]["items"]["properties"]["severity_claim"]["description"]
    assert "Only on a `conceded` verdict" in description and "`reason` then names the rubric clause" in description
    assert "`merge-review` raises the finding" in description


# --- the rule: at least survive_if_unrefuted_by panel defenders


def test_two_of_three_defenders_claiming_blocking_raise_a_minor_finding_and_it_blocks(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["BLOCKING", "BLOCKING", None])
    result = merge(run_cli, unit)
    assert result.returncode == 0, result.stdout + result.stderr
    record = read(unit, "review")
    found = merged_finding(unit)
    assert (found["severity"], found["severity_raised_from"]) == ("BLOCKING", "MINOR")
    assert record["survivors"] == ["correctness-1"] and record["blockers_surviving"] == 1  # a finding raised to BLOCKING counts as a blocker
    assert pl.check_record("review_record", record) == []
    assert "severity raised: correctness-1 from MINOR to BLOCKING (claimed by d1, d2)" in result.stdout
    assert "for the builder" in result.stdout and "correctness-1 [BLOCKING]" in result.stdout  # and the builder is told it at its new level
    gate = run_cli("gate", RUN, "review", cwd=unit)
    assert gate.returncode == 1 and "1 blocker(s) survive: correctness-1" in gate.stdout


def test_one_claim_in_three_is_not_enough_at_a_threshold_of_two(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["BLOCKING", None, None])
    result = merge(run_cli, unit)
    found = merged_finding(unit)
    assert found["severity"] == "MINOR" and "severity_raised_from" not in found
    assert read(unit, "review")["blockers_surviving"] == 0 and "severity raised" not in result.stdout


@pytest.mark.parametrize(
    "filed,claims,raised_to",
    [
        ("MINOR", ["BLOCKING", "BLOCKING", None], "BLOCKING"),
        ("MINOR", ["BLOCKING", "BLOCKING", "BLOCKING"], "BLOCKING"),
        ("MINOR", ["MAJOR", "MAJOR", None], "MAJOR"),
        ("MINOR", ["BLOCKING", "MAJOR", None], "MAJOR"),  # two claim at least MAJOR; only one claims BLOCKING
        ("MINOR", ["BLOCKING", "MAJOR", "MAJOR"], "MAJOR"),
        ("MINOR", ["BLOCKING", "BLOCKING", "MAJOR"], "BLOCKING"),
        ("MAJOR", ["BLOCKING", "BLOCKING", None], "BLOCKING"),
        ("MAJOR", ["BLOCKING", "MAJOR", None], None),  # a claim of MAJOR for a MAJOR finding is no claim: one of the two is left
        ("MINOR", [None, None, None], None),
    ],
)
def test_the_finding_is_raised_to_the_highest_level_that_enough_defenders_claimed(run_cli, unit, filed, claims, raised_to):
    file_findings(unit, [finding("correctness-1", filed)])
    defend(unit, "correctness-1", claims)
    assert merge(run_cli, unit).returncode == 0
    found = merged_finding(unit)
    assert found["severity"] == (raised_to or filed)
    assert found.get("severity_raised_from") == (filed if raised_to else None)


def test_a_claim_equal_to_or_below_the_filed_severity_is_ignored_with_a_warning(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    defend(unit, "correctness-1", ["MAJOR", "MINOR", "BLOCKING"])
    result = merge(run_cli, unit)
    assert result.returncode == 0
    assert merged_finding(unit)["severity"] == "MAJOR"  # one valid claim (BLOCKING) of the two the threshold asks for
    assert "warning: defender 'd1' claims MAJOR for 'correctness-1', which is not above the MAJOR the prosecutor filed, so the claim is ignored" in result.stderr
    assert "warning: defender 'd2' claims MINOR for 'correctness-1', which is not above the MAJOR the prosecutor filed, so the claim is ignored" in result.stderr
    assert "defender 'd3'" not in result.stderr


def test_a_blocking_finding_with_a_claim_of_blocking_gets_the_warning_and_stays_blocking(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "BLOCKING")])
    defend(unit, "correctness-1", ["BLOCKING", "BLOCKING", None])
    result = merge(run_cli, unit)
    found = merged_finding(unit)
    assert found["severity"] == "BLOCKING" and "severity_raised_from" not in found
    assert result.stderr.count("which is not above the BLOCKING the prosecutor filed") == 2


def test_a_finding_refuted_by_enough_defenders_is_not_raised(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["BLOCKING", "refuted", "refuted"])
    assert merge(run_cli, unit).returncode == 0
    record = read(unit, "review")
    assert record["survivors"] == [] and merged_finding(unit)["severity"] == "MINOR" and record["blockers_surviving"] == 0


def test_a_raise_to_major_is_no_blocker(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["MAJOR", "MAJOR", None])
    assert merge(run_cli, unit).returncode == 0
    assert merged_finding(unit)["severity"] == "MAJOR" and read(unit, "review")["blockers_surviving"] == 0
    assert run_cli("gate", RUN, "review", cwd=unit).returncode == 0


def test_the_rule_is_applied_to_each_finding_on_its_own(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR"), finding("correctness-2", "MINOR"), finding("correctness-3", "BLOCKING")])
    for n, who in enumerate(("d1", "d2", "d3")):
        put_part(
            unit, "review", who,
            {
                "defender": who,
                "defenses": [
                    entry("correctness-1", who, claim="BLOCKING" if n < 2 else None),  # two claim it
                    entry("correctness-2", who, claim="BLOCKING" if n == 0 else None),  # one claims it
                    entry("correctness-3", who),
                ],
            },
        )
    assert merge(run_cli, unit).returncode == 0
    record = read(unit, "review")
    assert {f["id"]: f["severity"] for f in record["findings"]} == {"correctness-1": "BLOCKING", "correctness-2": "MINOR", "correctness-3": "BLOCKING"}
    assert [f["id"] for f in record["findings"] if "severity_raised_from" in f] == ["correctness-1"]
    assert record["blockers_surviving"] == 2


def test_the_threshold_is_the_stages_and_a_majority_without_one(unit):
    project = pl.load_project(unit)
    review = next(s for s in project.pipeline["stage"] if s["id"] == "review")
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["BLOCKING", "BLOCKING", None])
    review["survive_if_unrefuted_by"] = 3  # all three must claim it, as all three must fail to refute
    record, problems, _ = pl.merge_review(project, RUN, "review")
    assert problems == [] and record["findings"][0]["severity"] == "MINOR"
    review["survive_if_unrefuted_by"] = 1
    record, problems, _ = pl.merge_review(project, RUN, "review")
    assert record["findings"][0]["severity"] == "BLOCKING"
    del review["survive_if_unrefuted_by"]  # a majority of 3 is 2
    record, problems, _ = pl.merge_review(project, RUN, "review")
    assert record["findings"][0]["severity"] == "BLOCKING"


def test_the_merged_record_keeps_each_claim_in_the_defenses(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["BLOCKING", "MAJOR", None])
    merge(run_cli, unit)
    claims = {d["defender"]: d.get("severity_claim") for d in read(unit, "review")["defenses"]}
    assert claims == {"d1": "BLOCKING", "d2": "MAJOR", "d3": None}


def test_merge_review_refuses_a_defense_record_with_a_claim_on_a_refutation(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    put_part(unit, "review", "d1", {"defender": "d1", "defenses": [{**entry("correctness-1", "d1", "refuted"), "severity_claim": "BLOCKING"}]})
    result = merge(run_cli, unit)
    assert result.returncode == 1 and "not a valid defense_record: $.defenses[0].severity_claim: only a conceded verdict carries a severity claim" in result.stdout
    assert not run_path(unit, RUN, "review.json").exists()


def test_the_gate_counts_a_raised_finding_from_the_record_and_not_from_what_the_record_says(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["BLOCKING", "BLOCKING", None])
    merge(run_cli, unit)
    record = read(unit, "review")
    assert pl.standing_blockers(record) == ["correctness-1"]
    record["blockers_surviving"] = 0  # a count that does not agree with the findings
    assert any("blockers_surviving says 0, but the findings and survivors give 1" in p for p in pl._gate_no_surviving_blockers(record, SimpleNamespace(measuring=False)))


def test_a_review_without_claims_writes_the_findings_as_it_always_did(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MAJOR")])
    defend(unit, "correctness-1", [None, None, None])
    merge(run_cli, unit)
    assert set(merged_finding(unit)) == {*finding("correctness-1"), "evidence_unverified"}  # no severity_raised_from, no other new key


def test_the_pass_record_lists_a_raised_finding_at_its_new_level(run_cli, unit):
    file_findings(unit, [finding("correctness-1", "MINOR")])
    defend(unit, "correctness-1", ["MAJOR", "MAJOR", None])
    merge(run_cli, unit)
    found, _gaps = pl.open_items("review", read(unit, "review"))
    assert [(f["id"], f["severity"]) for f in found] == [("correctness-1", "MAJOR")]


def test_render_shows_the_claim_and_the_raise():
    review = sample("review_record")
    review["defenses"][0]["severity_claim"] = "BLOCKING"
    review["findings"][1].update(severity="BLOCKING", severity_raised_from="MAJOR")
    out = pl.render_record("review_record", review)
    assert "F-1, d1: conceded (claims BLOCKING). The loop never raises." in out
    assert "Severity raised from MAJOR by the defenders' claims." in out


# --- the defender's prompt, the run skill and the README


def test_the_defender_compares_a_concession_with_the_severity_rubric_and_claims_what_it_is_worth():
    body = agent("defender")[1]
    for level, clause in zip(("BLOCKING", "MAJOR", "MINOR"), SEVERITY_CLAUSES):
        assert clause in body and re.search(rf"- {level} ", body), level  # the rubric the prosecutor files by
    assert "When you concede, compare the finding with the severity rubric" in body
    assert "If it is worse than its `severity` says, for example because it breaks a stated acceptance criterion" in body
    assert "set `severity_claim` to the level it deserves" in body and "quote the rubric clause in `reason`" in body
    assert "A refutation carries no `severity_claim`" in body and "Leave `severity_claim` out when the filed severity is right" in body
    assert "the path of the spec" in body.split("## How you work")[0]  # the criteria a claim rests on
    assert "severity_claim" in pl.load_schema("defense_record")["properties"]["defenses"]["items"]["properties"] and "severity_claim" in body


def test_the_run_skill_says_what_merge_review_does_with_a_claim():
    body = frontmatter(REPO / "skills" / "run" / "SKILL.md")[1]
    step = between(body, "3. `PLUMBLINE merge-review", "4. **Detective.**")
    assert "when enough defenders claim a higher one (`severity_claim` on a conceded verdict), the finding stands at that level" in step
    assert "a finding raised to BLOCKING is a blocker like any other" in step
    defenders = between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    assert "the path of the spec (the plan record) where the run has one" in defenders


def test_the_readme_states_the_rule_of_the_severity_claim():
    unit = next(line for line in section("Runs, gates and the pass record").split("\n\n") if line.startswith("**A review unit**"))
    for needed in (
        "A defender that concedes may add a `severity_claim`", "BLOCKING, MAJOR or MINOR", "naming the rubric clause in its `reason`",
        "When at least `survive_if_unrefuted_by` defenders claim a higher severity than the finding's, `merge-review` raises the finding to the highest level that many of them claimed",
        "`severity_raised_from`", "A finding raised to BLOCKING counts in `blockers_surviving`", "A claim equal to or below the filed severity is ignored, with a warning",
    ):
        assert needed in unit, needed
    row = next(line for line in section("Records").splitlines() if line.startswith("| `defense_record` |"))
    assert "`severity_claim`" in row
