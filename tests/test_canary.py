"""A canary finding, in calibration runs only: `plan --calibrate` marks the run, the canary plants one false finding in each review round that has
defenders, the defenders may not see its key, and `merge-review` reports how they answered it without ever counting it."""
import json

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import REPO, default_pipeline, write
from helpers import commit_all
from hookdata import bash_payload, stop_payload, tool_payload
from rundata import (
    RUN, adopt, adopt_base, begin, build_note_record, change_of, ledger, now_hash, put, put_part, read, run_entry, run_path, spec_record, verify_record,
    write_test_file, written_tests_record,
)
from samples import sample
from test_agents import agent
from test_manifests import between, frontmatter
from test_readme import rows, section

ROUND = ("review", "round-1")


def start(run_cli, repo, *extra, row="code.S"):
    result = run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", row, *extra, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def calibration(repo, run_cli):
    """An adopted repository with a calibration run of row code.S: its review stage has 3 defenders, a threshold of 2 and 1 screening defender.
    src/app.py holds the line the defenders quote."""
    adopt_base(repo)
    start(run_cli, repo, "--calibrate")
    write(repo / "src" / "app.py", "def main():\n    return retry(url)\n")
    return repo


@pytest.fixture
def ordinary(repo, run_cli):
    """The same, as an ordinary run: no canary."""
    adopt_base(repo)
    start(run_cli, repo)
    write(repo / "src" / "app.py", "def main():\n    return retry(url)\n")
    return repo


def finding(fid, severity="BLOCKING", lens="correctness"):
    return {
        "id": fid, "lens": lens, "file": "src/app.py", "line": 2, "claim": f"claim {fid}", "failure_scenario": "a concrete input",
        "rule": "AC-1", "evidence": "return retry(url)", "outside_code": None, "severity": severity,
    }


def canary_finding(fid="canary-1", severity="MAJOR", file="src/app.py"):
    return {**finding(fid, severity, "canary"), "file": file, "claim": "main swallows the error of retry", "failure_scenario": "retry raises, and main returns None"}


KEY = {"finding_id": "canary-1", "why_false": "main returns what retry returns, so retry's error reaches the caller"}


def file_findings(repo, findings, round_no=1):
    put_part(repo, "review", "prosecutor-correctness", {"lens": "correctness", "findings": findings}, round_no)
    put_part(repo, "review", "prosecutor-tests", {"lens": "tests", "findings": []}, round_no)


def plant(repo, finding_=None, key=KEY, round_no=1):
    """What the canary leaves: its record, traced to plumbline:canary, and its key, a file no agent entry covers."""
    put_part(repo, "review", "prosecutor-canary", {"lens": "canary", "findings": [finding_ or canary_finding()]}, round_no)
    if key is not None:
        put_part(repo, "review", "canary-key", key, round_no, agent=False)


def answer(fid, who, verdict="conceded", quote="return retry(url)", claim=None):
    made = {"finding_id": fid, "defender": who, "verdict": verdict, "quote": quote, "reason": f"{who} says {verdict}"}
    if claim:
        made["severity_claim"] = claim
    return made


def panel(repo, per_defender, round_no=1):
    """The panel, defender-1 to defender-3; `per_defender` lists, for each, the entries it gives."""
    for n, entries in enumerate(per_defender, 1):
        who = f"defender-{n}"
        put_part(repo, "review", who, {"defender": who, "defenses": [{**e, "defender": who} for e in entries]}, round_no)


def merge(run_cli, repo):
    return run_cli("merge-review", RUN, "review", cwd=repo)


def merged(repo):
    return read(repo, "review")


# --- the flag


def test_calibrate_marks_the_intake_record_and_the_plan_and_nothing_else(run_cli, repo):
    adopt_base(repo)
    plan = start(run_cli, repo, "--calibrate")
    intake = read(repo, "intake")
    assert intake["calibrate"] is True and plan["calibrate"] is True
    assert pl.check_record("change_class", intake) == []
    assert pl.load_run(pl.load_project(repo), RUN).calibrate is True


def test_an_ordinary_run_has_no_calibrate_key_anywhere(run_cli, repo):
    adopt_base(repo)
    plan = start(run_cli, repo)
    assert "calibrate" not in read(repo, "intake") and plan["calibrate"] is False  # the intake record is as it was before 0.5.0
    assert pl.load_run(pl.load_project(repo), RUN).calibrate is False


def test_calibrate_goes_with_an_intent_that_starts_a_run(run_cli, repo):
    adopt_base(repo)
    write(repo / "src" / "new_module.py", "x = 1\n" * 100)
    result = run_cli("plan", "--calibrate", cwd=repo)
    assert result.returncode == 2 and "--calibrate goes with --intent (the run it marks as a calibration run)" in result.stderr
    assert not (repo / ".plumbline").exists()


def test_calibrate_is_an_optional_boolean_of_the_intake_schema():
    schema = pl.load_schema("change_class")
    assert schema["properties"]["calibrate"]["type"] == "boolean" and "calibrate" not in schema["required"]
    record = sample("change_class")
    assert pl.check_record("change_class", record) == []
    record["calibrate"] = True
    assert pl.check_record("change_class", record) == []
    record["calibrate"] = "yes"
    assert any(e.startswith("$.calibrate:") for e in pl.check_record("change_class", record))


def test_the_intake_record_of_a_calibration_run_renders_as_one():
    record = sample("change_class")
    record["calibrate"] = True
    assert "A calibration run: each review round with defenders also has a canary" in pl.render_record("change_class", record)
    assert "calibration" not in pl.render_record("change_class", sample("change_class"))


# --- the agent, the record and the policy


def test_the_canary_is_the_eighth_agent_and_writes_a_findings_record_with_the_canary_lens():
    assert pl.AGENT_ROLES[-1] == "canary" and pl.AGENT_RECORDS["canary"] == "findings_record" and len(pl.AGENT_ROLES) == 8
    assert "canary" in pl.load_schema("findings_record")["properties"]["lens"]["enum"]
    assert "canary" in pl.load_schema("findings_record")["properties"]["findings"]["items"]["properties"]["lens"]["enum"]
    assert "canary" not in pl.load_schema("review_record")["properties"]["lenses"]["items"]["enum"]  # the merged record never holds the canary's finding
    assert "canary" not in pl.KNOWN_LENSES  # no stage or row reviews through it
    record = sample("findings_record")
    record.update(lens="canary")
    record["findings"] = [{**f, "lens": "canary"} for f in record["findings"][:1]]
    assert pl.check_record("findings_record", record) == []


def test_the_canary_policy_writes_its_records_only_and_runs_git_read_and_search():
    roles = default_pipeline()["roles"]
    assert roles["canary"] == {"writes": ["record"], "commands": ["git-read", "search"]}
    assert pl.validate_pipeline(default_pipeline()) == ([], [])
    pipeline = default_pipeline()
    del pipeline["roles"]["canary"]
    assert any("roles: there is no policy for the agent 'canary' (every agent needs one)" in e for e in pl.validate_pipeline(pipeline)[0])


def test_the_canary_prompt_says_what_to_plant_what_to_write_and_where():
    fields, body = agent("canary")
    assert fields["model"] == "sonnet" and "false finding" in fields["description"] and "calibration run" in fields["description"]
    for needed in (
        "You are the canary in a plumbline calibration run", "The prosecutors file findings that are true. You file one that is false",
        "`merge-review` keeps your finding out of the survivors, the routes, the blockers and what the pass record leaves open",
        "the two paths where you write, both in the round directory: your record, `prosecutor-canary.json`, and your key, `canary-key.json`",
        "It is plausible, so that a reviewer who reads only the claim believes it, and it is false, so that a line of the code, or of its file, shows it: a defender refutes it by quoting that line",
        "`id` `canary-1`, `lens` `canary`", "`severity` `MAJOR`, so that the round needs the same defenders with the canary as without it",
        '{"finding_id": "canary-1", "why_false": ', "Write both files with the Write tool", "RECORD: <the path of your record>",
    ):
        assert needed in body, needed
    assert '{"lens": "canary", "findings": [ <your one finding> ], "diff_sha256": "<the hash in your brief>"}' in body
    assert "check-record findings_record <the path of your record>" in body


# --- the hooks: the canary writes its two files


def write_by(repo, role, name, round_name="round-1", stage="review"):
    path = repo / ".plumbline" / "runs" / RUN / stage / round_name / name
    return pre.decide(tool_payload(repo, "Write", {"file_path": str(path), "content": "{}"}, agent_type=f"plumbline:{role}"))


def test_the_canary_writes_its_record_and_its_key_in_the_current_round_and_nothing_else(calibration):
    assert write_by(calibration, "canary", "prosecutor-canary.json") is None and write_by(calibration, "canary", "canary-key.json") is None
    for other in ("prosecutor-correctness.json", "defender-1.json", "screen-1.json", "detective.json", "notes.json"):
        assert write_by(calibration, "canary", other), other
    assert write_by(calibration, "canary", "prosecutor-canary.json", "round-2")  # another round's directory
    denied = write_by(calibration, "canary", "detective.json")
    assert "round-<n>/prosecutor-canary.json or canary-key.json" in denied


def test_the_canarys_files_are_the_canarys_alone(calibration):
    for role in ("prosecutor", "defender", "detective"):
        assert write_by(calibration, role, "prosecutor-canary.json"), role
        assert write_by(calibration, role, "canary-key.json"), role
    assert write_by(calibration, "prosecutor", "prosecutor-security.json") is None  # a prosecutor's own file is still its own
    assert pl.AGENT_RECORDS["canary"] == "findings_record" and pre.REVIEW_ROLES == ("prosecutor", "defender", "detective", "canary")
    assert pre.REVIEW_FILE_NAMES["canary"] == ("prosecutor-canary.json", "canary-key.json")


def test_the_canarys_stop_is_let_go_with_its_record_in_the_round_directory(run_stop, repo):
    adopt(repo)
    begin(repo, "code.S")
    record = {"lens": "canary", "findings": [canary_finding()], "diff_sha256": now_hash(repo)}
    write(run_path(repo, RUN, *ROUND, "prosecutor-canary.json"), json.dumps(record))
    result = run_stop(stop_payload(repo, "plumbline:canary", "Planted.\nRECORD: .plumbline/runs/r1/review/round-1/prosecutor-canary.json"), repo)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    [entry] = [e for e in ledger(repo) if e["kind"] == "agent"]
    assert (entry["agent_type"], entry["stage"], entry["record_type"], entry["valid"]) == ("plumbline:canary", "review", "findings_record", True)


def test_the_canary_has_no_stage_of_its_own_so_a_record_outside_a_review_round_holds_it_back(run_stop, repo):
    adopt(repo)
    begin(repo, "code.S")
    record = {"lens": "canary", "findings": [canary_finding()], "diff_sha256": now_hash(repo)}
    write(run_path(repo, RUN, "plan.json"), json.dumps(record))
    result = run_stop(stop_payload(repo, "plumbline:canary", "Planted.\nRECORD: .plumbline/runs/r1/plan.json"), repo)
    assert result.returncode == 2 and "a canary writes its record at .plumbline/runs/r1/review/round-<n>/<name>.json" in result.stderr


# --- the hooks: a defender does not see the key


def key_path(repo):
    return repo / ".plumbline" / "runs" / RUN / "review" / "round-1" / "canary-key.json"


def read_as(repo, role, tool, **tool_input):
    return pre.decide(tool_payload(repo, tool, tool_input, agent_type=f"plumbline:{role}" if role else None))


def bash_as(repo, command, role="defender"):
    return pre.decide(bash_payload(repo, command, agent_type=f"plumbline:{role}" if role else None))


@pytest.fixture
def keyed(calibration):
    file_findings(calibration, [finding("correctness-1")])
    plant(calibration)
    return calibration


def test_a_defender_cannot_read_the_key_or_find_it_through_a_symlink(keyed):
    denied = read_as(keyed, "defender", "Read", file_path=str(key_path(keyed)))
    assert denied and "would show a file that a defender answers without" in denied
    assert read_as(keyed, "defender", "Read", file_path=".plumbline/runs/r1/review/round-1/canary-key.json")  # relative to the working directory
    (keyed / "alias.json").symlink_to(key_path(keyed))
    assert read_as(keyed, "defender", "Read", file_path=str(keyed / "alias.json"))  # where a link leads is what counts


def test_a_defender_reads_the_canarys_record_like_any_findings_record(keyed):
    for name in ("prosecutor-canary.json", "prosecutor-correctness.json", "prosecutor-tests.json"):
        assert read_as(keyed, "defender", "Read", file_path=str(key_path(keyed).with_name(name))) is None, name
    assert read_as(keyed, "defender", "Read", file_path=str(keyed / "src" / "app.py")) is None
    assert read_as(keyed, "defender", "Read", file_path=str(keyed / ".plumbline" / "runs" / RUN / "plan.json")) is None  # the spec a claim rests on


def test_a_defender_cannot_search_the_directory_that_holds_the_key(keyed):
    round_dir = str(key_path(keyed).parent)
    for path in (round_dir, str(keyed / ".plumbline"), str(keyed / ".plumbline" / "runs"), str(keyed)):
        assert read_as(keyed, "defender", "Grep", pattern="retry", path=path), path
        assert read_as(keyed, "defender", "Glob", pattern="*.json", path=path), path
    assert read_as(keyed, "defender", "Grep", pattern="retry")  # no path: where the agent is, which is the repository
    assert read_as(keyed, "defender", "Grep", pattern="retry", path="  ")
    assert read_as(keyed, "defender", "Glob", pattern=".plumbline/runs/*/review/round-1/*.json", path=str(keyed))  # the part of the pattern before a wildcard leads there


def test_a_defender_can_still_search_the_source_and_the_findings_records_one_by_one(keyed):
    assert read_as(keyed, "defender", "Grep", pattern="retry", path="src") is None
    assert read_as(keyed, "defender", "Grep", pattern="retry", path=str(keyed / "src" / "app.py")) is None
    assert read_as(keyed, "defender", "Grep", pattern="claim", path=str(key_path(keyed).with_name("prosecutor-correctness.json"))) is None
    assert read_as(keyed, "defender", "Glob", pattern="src/**/*.py", path=str(keyed)) is None


def test_a_glob_that_names_the_key_is_denied_wherever_it_looks(keyed):
    assert read_as(keyed, "defender", "Glob", pattern="**/canary-key.json", path="src")
    assert read_as(keyed, "defender", "Glob", pattern="**/CANARY-KEY.JSON")


def test_the_denial_says_nothing_of_a_canary(keyed):
    shown = []
    shown.append(read_as(keyed, "defender", "Read", file_path=str(key_path(keyed))))
    shown.append(read_as(keyed, "defender", "Grep", pattern="x", path=str(key_path(keyed).parent)))
    shown.append(bash_as(keyed, "grep -rn retry ."))
    shown.append(read_as(keyed, "defender", "Glob", pattern="**/canary-key.json"))
    for reason in shown:
        assert reason and "canary" not in reason.replace("canary-key.json", "").lower(), reason  # the path the defender itself named is the only place the word can be


@pytest.mark.parametrize(
    "command",
    [
        "cat .plumbline/runs/r1/review/round-1/canary-key.json",
        "head -c 100 .plumbline/runs/r1/review/round-1/canary-key.json",
        "grep -rn x .plumbline/runs/r1/review/round-1/",
        "grep -rn retry .",
        "grep -rn retry",
        "grep -R retry",
        "grep --recursive retry",
        "grep x .plumbline/runs/r1/review/round-1/*.json",
        "head -5 .plumbline/runs/r1/review/round-1/canary-*",
        "find . -name '*.json'",
        "find",
        "rg -uu retry",
        "rg --hidden retry",
        "rg retry .plumbline",
        "ls -R .plumbline",
        "ls .plumbline/runs/r1/review/round-1/",
        "cat .plumbline/runs/r1/review/round-1/canary-key.json > /dev/null",
        "cd .plumbline/runs/r1/review/round-1 && cat canary-key.json",
        "cd .plumbline/runs/r1/review/round-1 && grep -r x .",
        "echo $(cat .plumbline/runs/r1/review/round-1/canary-key.json)",
    ],
)
def test_a_defenders_bash_does_not_reach_the_key(keyed, command):
    reason = bash_as(keyed, command)
    assert reason and "would show a file that a defender answers without" in reason, command


@pytest.mark.parametrize(
    "command",
    [
        "cat .plumbline/runs/r1/review/round-1/prosecutor-canary.json",
        "cat .plumbline/runs/r1/review/round-1/prosecutor-correctness.json",
        "grep -n retry src/app.py",
        "grep -rn retry src/",
        "grep -rn retry src",
        "rg retry",
        "rg retry src",
        "find src -name '*.py'",
        "ls src",
        "git diff",
        "git grep retry",
        "git show HEAD",
        "sed -n 1,3p src/app.py",
        "wc -l src/app.py",
    ],
)
def test_a_defenders_bash_still_reads_the_code_and_the_findings_records(keyed, command):
    assert bash_as(keyed, command) is None, command


def test_the_other_roles_and_the_main_session_read_the_key(keyed):
    for role in ("prosecutor", "detective", "canary", None):
        assert read_as(keyed, role, "Read", file_path=str(key_path(keyed))) is None, role
        assert bash_as(keyed, "cat .plumbline/runs/r1/review/round-1/canary-key.json", role) is None, role


def test_without_a_key_nothing_changes_for_a_defender(ordinary):
    file_findings(ordinary, [finding("correctness-1")])
    assert not list((ordinary / ".plumbline" / "runs").glob("*/*/round-*/canary-key.json"))
    assert read_as(ordinary, "defender", "Read", file_path=str(key_path(ordinary))) is None  # there is nothing to hide
    assert read_as(ordinary, "defender", "Grep", pattern="retry") is None
    assert read_as(ordinary, "defender", "Glob", pattern="**/canary-key.json") is None
    for command in ("grep -rn retry .", "find . -name '*.json'", "cat canary-key.json", "ls -R .plumbline"):
        assert bash_as(ordinary, command) is None, command


# --- the hooks: a defender's brief does not name the canary


def launch(repo, prompt, subagent="plumbline:defender", **fields):
    return pre.decide(tool_payload(repo, "Agent", {"description": "answer the findings", "prompt": prompt, "subagent_type": subagent, **fields}))


BRIEF = (
    "Run r1. You are defender-1. The findings records of this round: .plumbline/runs/r1/review/round-1/prosecutor-correctness.json, "
    ".plumbline/runs/r1/review/round-1/prosecutor-tests.json, .plumbline/runs/r1/review/round-1/prosecutor-canary.json. Write your record to .plumbline/runs/r1/review/round-1/defender-1.json."
)


def test_a_defenders_brief_lists_the_canarys_record_among_the_others_and_that_is_no_naming(calibration):
    assert launch(calibration, BRIEF) is None
    assert launch(calibration, BRIEF.replace("prosecutor-canary.json", "PROSECUTOR-CANARY.JSON")) is None


@pytest.mark.parametrize(
    "brief",
    [
        BRIEF + " The last of them is the canary's.",
        BRIEF + " Answer canary-1 as you answer the rest.",
        BRIEF + " The key is in .plumbline/runs/r1/review/round-1/canary-key.json.",
        BRIEF + " CANARY",
        "The canary has planted a finding. " + BRIEF,
    ],
)
def test_a_defenders_brief_that_names_the_canary_or_its_key_is_denied_in_a_calibration_run(calibration, brief):
    reason = launch(calibration, brief)
    assert reason and "names neither the canary nor its key" in reason and "Leave the word out of the brief" in reason


def test_the_same_brief_is_allowed_in_an_ordinary_run(ordinary):
    assert launch(ordinary, BRIEF + " A canary release is under review.") is None  # the word means nothing where no canary runs


def test_the_same_brief_is_allowed_to_any_other_agent_of_a_calibration_run(calibration):
    assert launch(calibration, "You are the canary. " + BRIEF, subagent="plumbline:canary") is None
    assert launch(calibration, BRIEF + " The canary is here.", subagent="plumbline:prosecutor") is None


def test_the_launch_denial_for_another_agent_names_the_canary_among_the_plumbline_agents(calibration):
    reason = launch(calibration, "read .plumbline/runs/r1/plan.json", subagent="general-purpose")
    assert "(plumbline:planner, test-writer, builder, verifier, prosecutor, defender, detective or canary), not to general-purpose" in reason


def test_a_canary_is_launched_in_the_main_checkout_with_the_model_its_definition_pins(calibration):
    assert launch(calibration, "plant it", subagent="plumbline:canary") is None
    assert "without `isolation`" in launch(calibration, "plant it", subagent="plumbline:canary", isolation="remote")
    assert "pinned to the sonnet model" in launch(calibration, "plant it", subagent="plumbline:canary", model="haiku")
    assert launch(calibration, "plant it", subagent="plumbline:canary", model="Sonnet") is None


# --- merge-review: how the canary is answered, and that it is never counted


def test_a_canary_the_panel_refuted_is_reported_and_counted_nowhere(run_cli, calibration):
    file_findings(calibration, [finding("correctness-1", "BLOCKING")])
    plant(calibration)
    panel(calibration, [
        [answer("correctness-1", "x"), answer("canary-1", "x", "refuted")],
        [answer("correctness-1", "x"), answer("canary-1", "x", "refuted")],
        [answer("correctness-1", "x"), answer("canary-1", "x")],
    ])
    result = merge(run_cli, calibration)
    assert result.returncode == 0, result.stdout + result.stderr
    record = merged(calibration)
    assert record["canary"] == {"finding_id": "canary-1", "refuted_by": ["defender-1", "defender-2"], "conceded_by": ["defender-3"]}
    assert [f["id"] for f in record["findings"]] == ["correctness-1"] and record["survivors"] == ["correctness-1"]
    assert record["routes"] == {"builder": ["correctness-1"], "test-writer": [], "planner": []} and record["blockers_surviving"] == 1
    assert all(d["finding_id"] == "correctness-1" for d in record["defenses"]) and len(record["defenses"]) == 3  # the answers to the canary are in its field alone
    assert "canary: refuted by 2 of 3 defenders (defender-1, defender-2); conceded by defender-3" in result.stdout
    assert pl.check_record("review_record", record) == []
    assert result.stderr == ""


def test_a_canary_every_defender_conceded_is_the_rubber_stamp_the_run_measures(run_cli, calibration):
    file_findings(calibration, [finding("correctness-1", "BLOCKING")])
    plant(calibration)
    panel(calibration, [[answer("correctness-1", "x"), answer("canary-1", "x")]] * 3)
    result = merge(run_cli, calibration)
    assert merged(calibration)["canary"] == {"finding_id": "canary-1", "refuted_by": [], "conceded_by": ["defender-1", "defender-2", "defender-3"]}
    assert "canary: refuted by 0 of 3 defenders; conceded by defender-1, defender-2, defender-3" in result.stdout


def test_one_screening_defender_answers_the_canary_in_a_round_without_a_blocking_finding(run_cli, calibration):
    file_findings(calibration, [finding("correctness-1", "MAJOR")])
    plant(calibration)
    put_part(calibration, "review", "screen-1", {"defender": "screen-1", "defenses": [answer("correctness-1", "screen-1"), answer("canary-1", "screen-1", "refuted")]})
    result = merge(run_cli, calibration)
    assert result.returncode == 0 and result.stderr == ""
    assert merged(calibration)["canary"] == {"finding_id": "canary-1", "refuted_by": ["screen-1"], "conceded_by": []}
    assert "canary: refuted by 1 of 1 defender (screen-1)" in result.stdout
    assert merged(calibration)["survivors"] == ["correctness-1"]


def test_a_screening_defenders_claim_of_blocking_for_the_canary_asks_for_no_panel(run_cli, calibration):
    file_findings(calibration, [finding("correctness-1", "MAJOR")])
    plant(calibration)
    put_part(calibration, "review", "screen-1", {"defender": "screen-1", "defenses": [answer("correctness-1", "screen-1"), answer("canary-1", "screen-1", claim="BLOCKING")]})
    result = merge(run_cli, calibration)
    record = merged(calibration)
    assert record["panel_needed"] == [] and "panel needed" not in result.stdout and record["blockers_surviving"] == 0
    assert record["canary"]["conceded_by"] == ["screen-1"]  # a claim that it is BLOCKING is a concession, and the strongest
    assert run_cli("gate", RUN, "review", cwd=calibration).returncode == 0


def test_a_canary_filed_as_blocking_is_still_no_blocker(run_cli, calibration):
    file_findings(calibration, [])
    plant(calibration, canary_finding(severity="BLOCKING"))
    panel(calibration, [[answer("canary-1", "x")]] * 3)
    result = merge(run_cli, calibration)
    record = merged(calibration)
    assert record["findings"] == [] and record["survivors"] == [] and record["blockers_surviving"] == 0
    assert record["canary"]["conceded_by"] == ["defender-1", "defender-2", "defender-3"]
    assert run_cli("gate", RUN, "review", cwd=calibration).returncode == 0


def test_a_refutation_of_the_canary_needs_a_quote_the_change_or_its_file_holds(run_cli, calibration):
    file_findings(calibration, [])
    plant(calibration)
    panel(calibration, [
        [answer("canary-1", "x", "refuted", quote="return retry(url)")],
        [answer("canary-1", "x", "refuted", quote="return somewhere_else(url)")],
        [answer("canary-1", "x", "refuted", quote="")],
    ])
    result = merge(run_cli, calibration)
    assert merged(calibration)["canary"] == {"finding_id": "canary-1", "refuted_by": ["defender-1"], "conceded_by": []}
    assert "defender 'defender-2' refuted 'canary-1' with a quote that is in neither the change nor src/app.py, which does not count" in result.stderr
    assert "defender 'defender-3' refuted 'canary-1' without quoting code, which does not count" in result.stderr
    assert "canary: refuted by 1 of 1 defender (defender-1)" in result.stdout  # the two that did not count are in neither list


def test_a_round_whose_defenders_did_not_run_has_a_canary_nobody_answered(run_cli, calibration):
    project = pl.load_project(calibration)
    next(s for s in project.pipeline["stage"] if s["id"] == "review")["screen_defenders"] = 0
    file_findings(calibration, [finding("correctness-1", "MAJOR")])
    plant(calibration)
    record, problems, warnings = pl.merge_review(project, RUN, "review")
    assert problems == [] and warnings == [] and record["canary"] == {"finding_id": "canary-1", "refuted_by": [], "conceded_by": []}
    assert pl.canary_summary(record) == "no defender answered it"


def test_the_canary_alone_is_a_round_to_answer(run_cli, calibration):
    file_findings(calibration, [])  # no prosecutor filed anything
    plant(calibration)
    panel(calibration, [[answer("canary-1", "x", "refuted")]] * 3)
    result = merge(run_cli, calibration)
    assert result.returncode == 0 and merged(calibration)["findings"] == []
    assert "canary: refuted by 3 of 3 defenders (defender-1, defender-2, defender-3)" in result.stdout


# --- merge-review: what makes the canary's files a problem


def problems_of(run_cli, repo):
    result = merge(run_cli, repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not run_path(repo, RUN, "review.json").exists()  # nothing was written
    return result.stdout


def test_a_calibration_rounds_defenders_with_no_canary_to_answer_are_a_problem(run_cli, calibration):
    file_findings(calibration, [finding("correctness-1", "BLOCKING")])
    panel(calibration, [[answer("correctness-1", "x")]] * 3)
    out = problems_of(run_cli, calibration)
    assert "this is a calibration run, and its defenders answered a round that has no canary: .plumbline/runs/r1/review/round-1/prosecutor-canary.json is missing" in out


def test_a_round_with_no_defender_yet_needs_no_canary_yet(run_cli, calibration):
    file_findings(calibration, [finding("correctness-1", "BLOCKING")])
    assert merge(run_cli, calibration).returncode == 0 and "canary" not in merged(calibration)


@pytest.mark.parametrize(
    "arrange,said",
    [
        (lambda r: plant(r, key=None), "the canary left no key (.plumbline/runs/r1/review/round-1/canary-key.json)"),
        (lambda r: put_part(r, "review", "canary-key", KEY, agent=False), "canary-key.json has no prosecutor-canary.json beside it"),
        (lambda r: plant(r, key={"finding_id": "canary-9", "why_false": "x"}), "the key names 'canary-9', but the canary's finding is 'canary-1'"),
        (lambda r: plant(r, key={"finding_id": "canary-1"}), "not a valid canary key: $.why_false: missing required key"),
        (lambda r: plant(r, key={"finding_id": "canary-1", "why_false": "x", "more": 1}), "not a valid canary key: $.more: unexpected key"),
        (lambda r: plant(r, key=[1]), "not a valid canary key: $: expected object, got array"),
        (lambda r: plant(r, canary_finding("correctness-1")), "the canary's finding id 'correctness-1' is also used in"),
    ],
    ids=["no-key", "key-alone", "key-names-another-finding", "key-without-why", "key-with-an-extra-field", "key-not-an-object", "id-of-a-prosecutor"],
)
def test_a_canary_whose_files_do_not_agree_is_a_problem(run_cli, calibration, arrange, said):
    file_findings(calibration, [finding("correctness-1", "MINOR")])
    arrange(calibration)
    assert said in problems_of(run_cli, calibration)


def test_a_canary_that_files_two_findings_or_none_or_another_lens_is_a_problem(run_cli, calibration):
    file_findings(calibration, [])
    put_part(calibration, "review", "prosecutor-canary", {"lens": "canary", "findings": [canary_finding("canary-1"), canary_finding("canary-2")]})
    put_part(calibration, "review", "canary-key", KEY, agent=False)
    assert "the canary files one finding (this record has 2)" in problems_of(run_cli, calibration)
    put_part(calibration, "review", "prosecutor-canary", {"lens": "canary", "findings": []})
    assert "the canary files one finding (this record has 0)" in problems_of(run_cli, calibration)
    put_part(calibration, "review", "prosecutor-canary", {"lens": "correctness", "findings": [canary_finding()]})
    assert "the canary's record carries lens 'correctness', not 'canary'" in problems_of(run_cli, calibration)


def test_the_canarys_record_is_traced_to_the_canary_and_not_to_a_prosecutor(run_cli, calibration):
    file_findings(calibration, [])
    put_part(calibration, "review", "prosecutor-canary", {"lens": "canary", "findings": [canary_finding()]}, agent=False)
    put_part(calibration, "review", "canary-key", KEY, agent=False)
    assert "prosecutor-canary.json has no entry from plumbline:canary; run the canary" in problems_of(run_cli, calibration)
    path = run_path(calibration, RUN, *ROUND, "prosecutor-canary.json")
    pl.append_ledger(calibration, RUN, {
        "kind": "agent", "agent_id": "p-1", "agent_type": "plumbline:prosecutor", "stage": "review", "record": pl.rel_path(calibration, path),
        "record_type": "findings_record", "record_sha256": pl.file_sha256(path), "valid": True, "blocks": 0,
    })
    assert "the latest entry for .plumbline/runs/r1/review/round-1/prosecutor-canary.json is from plumbline:prosecutor, not plumbline:canary; run the canary" in problems_of(run_cli, calibration)


def test_the_gate_traces_the_canary_as_a_part_of_the_merge(run_cli, calibration):
    file_findings(calibration, [])
    plant(calibration)
    panel(calibration, [[answer("canary-1", "x")]] * 3)
    merge(run_cli, calibration)
    merge_entry = next(e for e in ledger(calibration) if e["kind"] == "merge")
    assert ".plumbline/runs/r1/review/round-1/prosecutor-canary.json" in [part["path"] for part in merge_entry["parts"]]
    assert run_cli("gate", RUN, "review", cwd=calibration).returncode == 0
    path = run_path(calibration, RUN, *ROUND, "prosecutor-canary.json")
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    gate = run_cli("gate", RUN, "review", cwd=calibration)
    assert gate.returncode == 1 and "prosecutor-canary.json changed after merge-review read it" in gate.stdout


def test_the_canarys_files_are_ignored_with_a_warning_where_the_run_is_no_calibration_run(run_cli, ordinary):
    file_findings(ordinary, [finding("correctness-1", "MINOR")])
    plant(ordinary)
    result = merge(run_cli, ordinary)
    assert result.returncode == 0
    assert "run 'r1' is not a calibration run, so the canary's records were ignored" in result.stderr
    record = merged(ordinary)
    assert "canary" not in record and [f["id"] for f in record["findings"]] == ["correctness-1"]
    merge_entry = next(e for e in ledger(ordinary) if e["kind"] == "merge")
    assert not [part for part in merge_entry["parts"] if "canary" in part["path"]]


def test_the_canarys_files_are_ignored_with_a_warning_where_the_stage_has_no_defenders(run_cli, repo):
    adopt_base(repo)
    start(run_cli, repo, "--calibrate", row="code.M")
    put_part(repo, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    put_part(repo, "test-review", "prosecutor-canary", {"lens": "canary", "findings": [canary_finding()]})
    put_part(repo, "test-review", "canary-key", KEY, agent=False)
    result = run_cli("merge-review", RUN, "test-review", cwd=repo)
    assert result.returncode == 0 and "this stage has no defenders, so the canary's records were ignored" in result.stderr
    assert "canary" not in read(repo, "test-review")


def test_a_calibration_run_does_not_ask_the_canary_of_a_stage_without_defenders(run_cli, repo):
    adopt_base(repo)
    start(run_cli, repo, "--calibrate", row="code.M")
    put_part(repo, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    assert run_cli("merge-review", RUN, "test-review", cwd=repo).returncode == 0  # no defenders, so no canary is wanted


# --- what the run keeps of it


def test_the_review_record_with_a_canary_renders_it_and_a_record_without_renders_as_before():
    record = sample("review_record")
    assert "Canary" not in pl.render_record("review_record", record)
    record["canary"] = {"finding_id": "canary-1", "refuted_by": ["d1"], "conceded_by": ["d2", "d3"]}
    out = pl.render_record("review_record", record)
    assert "## Canary" in out and "`canary-1`, a finding planted to be false: refuted by 1 of 3 defenders (d1); conceded by d2, d3" in out
    assert pl.check_record("review_record", record) == []


def test_the_canary_field_has_the_three_keys_and_is_optional():
    schema = pl.load_schema("review_record")
    assert "canary" not in schema["required"]
    assert schema["properties"]["canary"]["required"] == ["finding_id", "refuted_by", "conceded_by"]
    record = sample("review_record")
    record["canary"] = {"finding_id": "canary-1", "refuted_by": []}
    assert any(e.startswith("$.canary.conceded_by:") for e in pl.check_record("review_record", record))


def test_the_pass_record_of_a_calibration_run_carries_what_the_canary_measured_and_none_of_its_finding(run_cli, calibration):
    write_test_file(calibration)
    commit_all(calibration, "the change and its tests")
    diff = change_of(calibration)
    put(calibration, "plan", spec_record())
    put(calibration, "tests", written_tests_record())
    run_entry(calibration, "tests", None, exit_code=1)
    put(calibration, "build", build_note_record())
    put(calibration, "verify", verify_record(diff=diff))
    run_entry(calibration, "verify", diff)
    file_findings(calibration, [finding("correctness-1", "MINOR")])
    plant(calibration)
    panel(calibration, [[answer("correctness-1", "x"), answer("canary-1", "x", "refuted")]] * 3)
    assert merge(run_cli, calibration).returncode == 0
    result = run_cli("pass", RUN, cwd=calibration)
    assert result.returncode == 0, result.stdout
    record = read(calibration, "reduce")
    assert "calibration run: the canary of review, round 1: refuted by 3 of 3 defenders (defender-1, defender-2, defender-3)" in record["notes"]
    assert [f["id"] for f in record["open_findings"]] == ["correctness-1"]  # the canary is not an open finding


# --- the run skill and the README


def test_the_run_skill_explains_the_calibration_run_and_the_canarys_round():
    body = frontmatter(REPO / "skills" / "run" / "SKILL.md")[1]
    start_section = between(body, "## 3. Start the run", "## 4. The stages")
    assert "PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>] --request-file <file> [--calibrate]" in start_section
    assert "`--calibrate` marks a calibration run" in start_section and "ask for it only when the builder wants to measure whether the defenders can refute a finding at all" in start_section
    prosecutors = between(body, "1. **Prosecutors.**", "2. **Defenders.**")
    assert "In a calibration run, launch `plumbline:canary` in the same message as the prosecutors" in prosecutors
    assert "`.../prosecutor-canary.json` and `.../canary-key.json`" in prosecutors
    defenders = between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    assert "In a calibration run the canary's record is one of the findings records: list its path with the others, written the same way, and say nothing of a canary" in defenders
    assert "The canary is a finding to answer, so launch the defenders the round's rule names even when no prosecutor filed a finding" in defenders
    merge_step = between(body, "3. `PLUMBLINE merge-review", "4. **Detective.**")
    assert "In a calibration run it also prints `canary: refuted by 2 of 3 defenders`" in merge_step and "tell the builder how the defenders answered it" in merge_step
    assert "A defender that concedes the canary concedes whatever it reads" in merge_step
    assert "what the canary measured" in between(body, "## 7. Reduce", "## 8.")


def test_the_readme_explains_calibration_runs_the_eighth_agent_and_the_canarys_hooks():
    plan_row = next(line for line in section("The command line", 3).splitlines() if line.startswith("| `plan "))
    assert plan_row.startswith("| `plan [--project PATH] [--base REF] [--run-id ID] [--intent ID [--spec FILE] [--request-file FILE] [--calibrate]] [--row ROW]` |")
    assert "`--calibrate` marks the run as a calibration run in its intake record" in plan_row
    agents = {r[0].strip("`"): r for r in rows(section("Agents"))}
    assert agents["plumbline:canary"][1:] == ["Sonnet", "Read, Grep, Glob, Bash, Write", "its records", "`findings_record`"]
    assert section("Agents").lstrip().startswith("Eight agents,")
    assert "`[roles.<agent>]`: what each of the eight agents may do" in section("The pipeline file")
    paragraph = section("Runs, gates and the pass record").split("**Calibration runs.**", 1)[1].split("\n\n", 1)[0]
    for needed in (
        "Whether defenders can refute at all is untested when they concede everything", "`plan --intent ... --calibrate` marks the run as a calibration run in its intake record",
        "every review round that has defenders also gets a canary", "`plumbline:canary` (Sonnet) writes one plausible but false finding about the change as `prosecutor-canary.json` (a `findings_record` with lens `canary`, filed MAJOR) and `canary-key.json`",
        "defenders' brief lists the canary's record among the prosecutors' records and names neither it nor the key",
        "`merge-review` never counts the canary in the survivors, the routes, the blockers or the open findings",
        "`canary` field of the review record", "`finding_id`, `refuted_by` (a refutation whose quote the change or the finding's file holds) and `conceded_by`", "canary: refuted by 2 of 3 defenders", "the pass record's notes",
        "A defender that concedes the canary is a rubber stamp",
    ):
        assert needed in paragraph, needed
    hooks = section("Hooks")
    assert "a defender does not read `canary-key.json`" in hooks and "a defender's brief that names the canary" in hooks
    assert "`prosecutor-canary.json` and `canary-key.json`" in hooks
    records = {r[0].strip("`"): r for r in rows(section("Records"))}
    assert "whether it is a calibration run" in records["change_class"][2] and "`canary`" in records["review_record"][2]


def test_the_limits_the_readme_lists_for_the_canary_are_real_today(keyed):
    limits = section("Limits")
    assert "**The canary's key through a path the hook cannot read.**" in limits and "`P=<the round directory>/canary-; cat ${P}key.json`" in limits
    assert "the record's own name, `prosecutor-canary.json`, and its lens, `canary`, are in front of every defender that reads the findings" in limits
    assert bash_as(keyed, f"P={key_path(keyed).parent}/canary-; cat ${{P}}key.json") is None  # the hook reads the words, and a path put together by the shell is none
    assert bash_as(keyed, f"cat {key_path(keyed)}")  # while the plain spelling is held
    record = json.loads(key_path(keyed).with_name("prosecutor-canary.json").read_text(encoding="utf-8"))
    assert record["lens"] == "canary"  # what a defender reads


def test_a_defender_may_not_run_a_shell_at_all_so_sh_c_is_no_way_round(keyed):
    reason = bash_as(keyed, "sh -c 'cat .plumbline/runs/r1/review/round-1/canary-key.json'")
    assert reason and "the defender's Bash may run only" in reason
