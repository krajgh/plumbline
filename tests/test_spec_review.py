"""A spec review after plan, for size M and L: the run stores the request, a requirements lens compares the spec with it, and a finding
about the spec goes back to the planner."""
import json

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import DEFAULT_TOML, REPO, default_pipeline, write
from hookdata import stop_payload, tool_payload
from rundata import RUN, adopt, adopt_base, begin, ledger, now_hash, put, put_part, read, run_path, spec_record
from samples import sample
from test_agents import agent
from test_manifests import between, frontmatter
from test_pipeline import check, stage
from test_readme import README, rows, section

REQUEST = "Add a retry to the fetch helper.\nIt retries up to three times, and raises the last error unchanged.\n"
PLAN_FILE = f".plumbline/runs/{RUN}/plan.json"
REQUEST_FILE = f".plumbline/runs/{RUN}/request.md"


@pytest.fixture
def request_file(tmp_path):
    return write(tmp_path / "request.md", REQUEST)


def start(run_cli, repo, *extra, intent="feature", row="code.M"):
    result = run_cli("plan", "--run-id", RUN, "--intent", intent, "--row", row, *extra, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def stored(repo, run_cli, request_file):
    """An adopted repository with a run of row code.M started with its request, and the planner's spec in place."""
    adopt_base(repo)
    start(run_cli, repo, "--request-file", str(request_file))
    put(repo, "plan", spec_record())
    return repo


def finding(fid="requirements-1", severity="BLOCKING", file=REQUEST_FILE, line=2, evidence="raises the last error unchanged"):
    return {
        "id": fid, "lens": "requirements", "file": file, "line": line, "claim": f"claim {fid}", "failure_scenario": "a build that passes every gate and does not do what was asked",
        "rule": "the request asks for it explicitly", "evidence": evidence, "outside_code": None, "severity": severity,
    }


def file_findings(repo, findings, round_no=1):
    put_part(repo, "spec-review", "prosecutor-requirements", {"lens": "requirements", "findings": findings}, round_no)


def conceding(fid, who):
    return {"finding_id": fid, "defender": who, "verdict": "conceded", "quote": "", "reason": f"{who} agrees"}


def panel(repo, fid, round_no=1):
    for n in (1, 2, 3):
        who = f"defender-{n}"
        put_part(repo, "spec-review", who, {"defender": who, "defenses": [conceding(fid, who)]}, round_no)


# --- the request is stored and traced


def test_plan_stores_the_request_as_it_is_and_enters_its_hash_in_the_ledger_before_the_intake(run_cli, repo, request_file):
    adopt_base(repo)
    request_file.write_bytes("Café au lait\r\nline two, no newline at the end".encode("utf-8"))
    plan = start(run_cli, repo, "--request-file", str(request_file))
    stored_file = run_path(repo, RUN, "request.md")
    assert stored_file.read_bytes() == request_file.read_bytes()  # the bytes, not a rewrite
    assert plan["request"] == REQUEST_FILE
    kinds = [e["kind"] for e in ledger(repo)]
    assert kinds.index("request") < kinds.index("intake")  # the hash is entered first, as the intake record's is
    [entry] = [e for e in ledger(repo) if e["kind"] == "request"]
    assert entry["record"] == REQUEST_FILE and entry["record_sha256"] == pl.file_sha256(stored_file)
    assert set(entry) == {"at", "kind", "record", "record_sha256"}  # no path of the file the request came from: the ledger is the run's


def test_the_spec_review_finds_the_request_where_the_plan_says(run_cli, repo, request_file):
    adopt_base(repo)
    plan = start(run_cli, repo, "--request-file", str(request_file))
    [review] = [s for s in plan["stages"] if s["id"] == "spec-review"]
    assert review["reads"][0] == {"name": "request", "kind": "input", "path": REQUEST_FILE, "optional": False}
    intake = next(s for s in plan["stages"] if s["id"] == "intake")
    assert intake["reads"][0]["path"] == REQUEST_FILE  # every stage that reads the request is told where it is


def test_a_run_started_without_a_request_stores_none(run_cli, repo):
    adopt_base(repo)
    plan = start(run_cli, repo)
    assert plan["request"] is None and not run_path(repo, RUN, "request.md").exists()
    assert not [e for e in ledger(repo) if e["kind"] == "request"]


def test_the_request_goes_with_an_intent_that_starts_a_run(run_cli, repo, request_file):
    adopt_base(repo)
    write(repo / "src" / "new_module.py", "x = 1\n" * 100)
    result = run_cli("plan", "--request-file", str(request_file), cwd=repo)
    assert result.returncode == 2 and "--request-file goes with --intent (the run that stores the request)" in result.stderr
    assert not (repo / ".plumbline").exists()


@pytest.mark.parametrize(
    "content,said",
    [
        (None, "--request-file {path}: no such file"),
        (b"", "the file is empty, so there is no request to store"),
        (b" \n\t\n", "the file is empty, so there is no request to store"),
        (b"\xff\xfe\x00bad", "the request must be UTF-8 text"),
    ],
    ids=["missing", "empty", "blank", "not-utf-8"],
)
def test_a_request_file_that_is_missing_empty_or_not_text_starts_nothing(run_cli, repo, tmp_path, content, said):
    adopt_base(repo)
    path = tmp_path / "nope.md"
    if content is not None:
        path.write_bytes(content)
    result = run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.M", "--request-file", str(path), cwd=repo)
    assert result.returncode == 2 and said.format(path=path) in result.stderr
    assert not (repo / ".plumbline" / "runs").exists()  # not the run, not ACTIVE


def test_a_run_that_has_begun_keeps_its_request(run_cli, repo, request_file, tmp_path):
    adopt_base(repo)
    start(run_cli, repo, "--request-file", str(request_file))
    other = write(tmp_path / "other.md", "A different request.\n")
    again = run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.M", "--request-file", str(other), cwd=repo)
    assert again.returncode == 1 and "run 'r1' has begun" in again.stderr
    assert run_path(repo, RUN, "request.md").read_text(encoding="utf-8") == REQUEST
    assert len([e for e in ledger(repo) if e["kind"] == "request"]) == 1


def test_a_fix_stores_the_request_next_to_the_spec_it_supplies(run_cli, repo, request_file, tmp_path):
    adopt_base(repo)
    spec = write(tmp_path / "spec.json", json.dumps({**spec_record(("AC-1",)), "acceptance_criteria": spec_record()["acceptance_criteria"][:1]}))
    start(run_cli, repo, "--request-file", str(request_file), "--spec", str(spec), intent="fix")
    assert run_path(repo, RUN, "request.md").read_text(encoding="utf-8") == REQUEST and run_path(repo, RUN, "plan.json").is_file()


def test_the_spec_review_cannot_be_merged_for_a_run_that_stored_no_request(run_cli, repo):
    adopt_base(repo)
    start(run_cli, repo)
    put(repo, "plan", spec_record())
    file_findings(repo, [])
    result = run_cli("merge-review", RUN, "spec-review", cwd=repo)
    assert result.returncode == 1 and "the run stored no request, so there is nothing to compare the spec with: start it with `plan --intent <intent> --request-file FILE`" in result.stdout
    assert not run_path(repo, RUN, "spec-review.json").exists()


def test_a_request_edited_after_it_was_stored_is_refused_by_merge_review_and_by_the_gate(run_cli, stored):
    file_findings(stored, [])
    assert run_cli("merge-review", RUN, "spec-review", cwd=stored).returncode == 0
    assert run_cli("gate", RUN, "spec-review", cwd=stored).returncode == 0
    run_path(stored, RUN, "request.md").write_text(REQUEST + "And it must be fast.\n", encoding="utf-8")
    merge = run_cli("merge-review", RUN, "spec-review", cwd=stored)
    assert merge.returncode == 1 and f"{REQUEST_FILE} changed after `plan --intent` stored it (the ledger holds its hash from then)" in merge.stdout
    gate = run_cli("gate", RUN, "spec-review", cwd=stored)
    assert gate.returncode == 1 and f"{REQUEST_FILE} changed after `plan --intent` stored it" in gate.stdout


def test_a_request_that_was_deleted_is_missing_for_the_spec_review(run_cli, stored):
    file_findings(stored, [])
    run_path(stored, RUN, "request.md").unlink()
    merge = run_cli("merge-review", RUN, "spec-review", cwd=stored)
    assert merge.returncode == 1 and f"{REQUEST_FILE} is missing, though the ledger shows `plan --intent` storing it" in merge.stdout


def test_a_review_that_reads_no_request_is_not_held_to_one(run_cli, repo):
    adopt_base(repo)
    start(run_cli, repo)  # no request stored
    put_part(repo, "test-review", "prosecutor-tests", {"lens": "tests", "findings": []})
    assert run_cli("merge-review", RUN, "test-review", cwd=repo).returncode == 0  # test-review reads the plan and the tests, not the request


def test_the_provenance_of_the_spec_review_includes_the_request_as_stored(run_cli, stored):
    """`pass` and the push gate evaluate all_gates_passed again, which asks provenance_problems about each stage: one that reads the request is traced to it."""
    context = pl.load_project(stored)
    spec_review = next(s for s in context.pipeline["stage"] if s["id"] == "spec-review")
    ledger_before = pl.read_ledger(stored, RUN)
    assert pl.provenance_problems(stored, RUN, spec_review, None, ledger_before) == ["its record has no entry from merge-review; run `plumbline.py merge-review r1 spec-review`"]
    run_path(stored, RUN, "request.md").write_text("Something else.\n", encoding="utf-8")
    problems = pl.provenance_problems(stored, RUN, spec_review, None, pl.read_ledger(stored, RUN))
    assert any("changed after `plan --intent` stored it" in p for p in problems)


# --- the lens


def test_requirements_is_a_lens_of_the_pipeline_and_of_the_records():
    assert "requirements" in pl.KNOWN_LENSES and pl.KNOWN_LENSES[-1] == "requirements"
    for name, path in (("findings_record", ["properties", "lens"]), ("findings_record", ["properties", "findings", "items", "properties", "lens"]),
                       ("review_record", ["properties", "lenses", "items"]), ("review_record", ["properties", "findings", "items", "properties", "lens"])):
        node = pl.load_schema(name)
        for key in path:
            node = node[key]
        assert "requirements" in node["enum"], (name, path)
    record = sample("findings_record")
    record.update(lens="requirements")
    for f in record["findings"]:
        f["lens"] = "requirements"
    assert pl.check_record("findings_record", record) == []


def test_a_row_or_an_intent_may_name_the_requirements_lens():
    assert check(lambda d: d["matrix"]["docs"].update(lenses=["requirements"])) == ([], [])
    assert check(lambda d: d["intent"]["refactor"].update(lenses=["correctness", "requirements"])) == ([], [])


def test_the_prosecutor_carries_the_requirements_lens_and_its_rubric():
    body = agent("prosecutor")[1]
    lens = next(line for line in body.splitlines() if line.startswith("- requirements:"))
    for needed in ("Read `request.md` against the spec's acceptance criteria and test plan", "something the request asks for that no criterion states", "an intent the spec changes",
                   "a criterion that conflicts with the request or with another criterion", "a criterion with no result a test can observe"):
        assert needed in lens, needed
    assert "In the review of the spec, the requirements lens, what you review is the spec: the brief gives the path of the request (`request.md` in the run) and of the spec (the plan record)" in body
    assert "In the requirements lens the same three levels read: BLOCKING when the spec contradicts, or leaves out, something the request asks for explicitly; MAJOR when an ambiguity would let a wrong build pass; MINOR for wording." in body
    assert "In the review of the spec, `file` is the spec (the plan record) for a criterion that is wrong or unclear and `request.md` for something the spec leaves out" in body


# --- the stage, the rows and the intents


def test_the_spec_review_is_defined_as_the_brief_says_and_stands_directly_after_plan():
    stages = default_pipeline()["stage"]
    ids = [s["id"] for s in stages]
    assert ids[ids.index("plan") + 1] == "spec-review"
    review = stages[ids.index("spec-review")]
    assert review == {
        "id": "spec-review", "kind": "review", "target": "plan", "reads": ["request", "plan"], "record": "review_record", "lenses": ["requirements"], "defenders": 3,
        "survive_if_unrefuted_by": 2, "screen_defenders": 1, "detective": False, "gate": "no_surviving_blockers", "on_fail": "plan", "max_rounds": 2,
    }


def test_the_spec_review_is_in_the_rows_of_size_m_and_l_and_in_no_other():
    matrix = default_pipeline()["matrix"]
    assert matrix["code"]["M"]["stages"] == ["intake", "plan", "spec-review", "tests", "test-review", "build", "verify", "review", "reduce"]
    assert matrix["code"]["L"]["stages"] == ["intake", "plan", "spec-review"]
    for label in ("S",):
        assert "spec-review" not in matrix["code"][label]["stages"]  # a spec of size S has one or two criteria
    for name in ("docs", "config", "tests"):
        assert "spec-review" not in matrix[name]["stages"]


def test_the_default_pipeline_still_validates_with_no_errors_and_no_notes():
    assert pl.validate_pipeline(default_pipeline()) == ([], [])
    text = DEFAULT_TOML.read_text(encoding="utf-8")
    assert "# spec-review is in the rows of size M and L" in text  # the row comment says why


def test_the_refactor_and_the_review_only_intents_skip_the_spec_review_and_the_others_keep_it():
    pipeline = default_pipeline()
    assert "spec-review" not in pl.effective_row(pipeline, "code.M", "refactor").stages
    assert "spec-review" not in pl.effective_row(pipeline, "code.M", "review-only").stages
    for intent in ("feature", "fix", "spec-supplied"):
        assert "spec-review" in pl.effective_row(pipeline, "code.M", intent).stages, intent
    assert pl.effective_row(pipeline, "code.L", "fix").stages == ["intake", "spec-review"]


def test_where_the_intent_supplies_the_spec_a_failed_spec_review_goes_to_main(run_cli, repo, request_file, tmp_path):
    adopt_base(repo)
    spec = write(tmp_path / "spec.json", json.dumps({**spec_record(("AC-1",)), "acceptance_criteria": spec_record()["acceptance_criteria"][:1]}))
    plan = start(run_cli, repo, "--request-file", str(request_file), "--spec", str(spec), intent="fix")
    assert next(s for s in plan["stages"] if s["id"] == "spec-review")["on_fail"] == "main"  # plan is not in the run: a corrected spec is a new run


# --- routing


def route(path, lens="correctness"):
    return pl.route_of(default_pipeline(), {**finding(), "lens": lens, "file": path})


@pytest.mark.parametrize(
    "path,lens,goes_to",
    [
        ("src/app.py", "requirements", "planner"),  # the requirements lens, wherever it points
        (REQUEST_FILE, "requirements", "planner"),
        (PLAN_FILE, "correctness", "planner"),  # a finding on the plan record, whatever lens it came through
        (".plumbline/runs/other-run.2/plan.json", "boundaries", "planner"),
        ("./.plumbline/runs/r1/plan.json", "docs", "planner"),
        ("/some/checkout/.plumbline/runs/r1/plan.json", "correctness", "planner"),
        ("tests/test_app.py", "correctness", "test-writer"),
        ("src/app.py", "tests", "test-writer"),
        ("src/app.py", "correctness", "builder"),
        ("src/plan.json", "correctness", "builder"),  # a file called plan.json is not the plan record
        (".plumbline/runs/r1/tests.json", "correctness", "builder"),
        (".plumbline/runs/r1/plan.json.bak", "correctness", "builder"),
    ],
)
def test_requirements_findings_and_findings_on_the_plan_record_go_to_the_planner(path, lens, goes_to):
    assert route(path, lens) == goes_to


def test_a_pipeline_names_the_plan_record_by_the_stage_that_writes_a_spec():
    pipeline = default_pipeline()
    stage(pipeline, "plan")["id"] = "spec"
    for s in pipeline["stage"]:
        s["reads"] = [("spec" if r == "plan" else r) for r in s.get("reads", [])]
    assert pl.route_of(pipeline, {**finding(), "lens": "correctness", "file": ".plumbline/runs/r1/spec.json"}) == "planner"
    assert pl.route_of(pipeline, {**finding(), "lens": "correctness", "file": ".plumbline/runs/r1/plan.json"}) == "builder"


def test_merge_review_routes_a_surviving_requirements_finding_to_the_planner_and_prints_its_text(run_cli, stored):
    file_findings(stored, [finding("requirements-1", "BLOCKING")])
    panel(stored, "requirements-1")
    result = run_cli("merge-review", RUN, "spec-review", cwd=stored)
    assert result.returncode == 0, result.stdout + result.stderr
    record = read(stored, "spec-review")
    assert record["routes"] == {"builder": [], "test-writer": [], "planner": ["requirements-1"]}
    assert (record["target"], record["lenses"], record["blockers_surviving"]) == ("plan", ["requirements"], 1)
    assert pl.check_record("review_record", record) == []
    assert "surviving findings for the planner (the requirements lens, and findings on the plan record: the spec is what is wrong):" in result.stdout
    assert f"  - requirements-1 [BLOCKING] {REQUEST_FILE}:2: claim requirements-1 Failure: a build that passes every gate" in result.stdout
    assert "for the builder" not in result.stdout and "evidence found in neither" not in result.stdout  # the quote is in request.md


def test_a_review_of_the_diff_has_an_empty_planner_list_and_a_record_from_before_0_5_0_has_none():
    record = sample("review_record")
    record["routes"] = {"builder": ["F-1"], "test-writer": []}  # no planner key
    assert pl.check_record("review_record", record) == []
    assert pl.route_lines(record) == [
        "surviving findings for the builder (give the builder this text and nothing else):",
        "  - F-1 [BLOCKING] src/app.py:14: The retry loop swallows the last error. Failure: Every attempt fails: the caller receives None instead of the error. Rule: AC-2",
    ]
    schema = pl.load_schema("review_record")["properties"]["routes"]
    assert "planner" in schema["properties"] and "planner" not in schema["required"]


def test_the_spec_review_fails_its_gate_on_a_blocker_and_opens_the_next_round_for_the_planner(run_cli, stored):
    file_findings(stored, [finding("requirements-1", "BLOCKING")])
    panel(stored, "requirements-1")
    run_cli("merge-review", RUN, "spec-review", cwd=stored)
    gate = run_cli("gate", RUN, "spec-review", cwd=stored)
    assert gate.returncode == 1 and "1 blocker(s) survive: requirements-1" in gate.stdout and "round 1 of 2" in gate.stdout
    assert "round 2 of 2 is open" in gate.stdout and run_path(stored, RUN, "spec-review", "round-2").is_dir()
    put(stored, "plan", spec_record())  # the planner runs again
    file_findings(stored, [], round_no=2)
    assert run_cli("merge-review", RUN, "spec-review", cwd=stored).returncode == 0
    second = run_cli("gate", RUN, "spec-review", cwd=stored)
    assert second.returncode == 0 and "round 2 of 2" in second.stdout


def test_a_spec_review_with_no_blocking_finding_needs_one_screening_defender_and_no_panel(run_cli, stored):
    file_findings(stored, [finding("requirements-1", "MAJOR")])
    put_part(stored, "spec-review", "screen-1", {"defender": "screen-1", "defenses": [conceding("requirements-1", "screen-1")]})
    result = run_cli("merge-review", RUN, "spec-review", cwd=stored)
    assert result.returncode == 0 and result.stderr == ""
    record = read(stored, "spec-review")
    assert record["survivors"] == ["requirements-1"] and record["routes"]["planner"] == ["requirements-1"] and record["panel_needed"] == []
    assert run_cli("gate", RUN, "spec-review", cwd=stored).returncode == 0  # MAJOR is no blocker: the planner may take it up, or the run goes on


# --- the hooks


def test_a_prosecutor_writes_its_requirements_record_in_the_spec_review_round_and_its_stop_is_let_go(run_stop, repo):
    adopt(repo)
    begin(repo, "code.M")
    path = run_path(repo, RUN, "spec-review", "round-1", "prosecutor-requirements.json")
    assert pre.decide(tool_payload(repo, "Write", {"file_path": str(path), "content": "{}"}, agent_type="plumbline:prosecutor")) is None
    record = {"lens": "requirements", "findings": [], "diff_sha256": now_hash(repo)}
    write(path, json.dumps(record))
    result = run_stop(stop_payload(repo, "plumbline:prosecutor", f"Reviewed.\nRECORD: .plumbline/runs/r1/spec-review/round-1/prosecutor-requirements.json"), repo)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert [e["stage"] for e in ledger(repo) if e["kind"] == "agent"] == ["spec-review"]


def test_the_denial_names_the_spec_review_among_the_stages_a_review_agent_writes_in(repo):
    adopt(repo)
    begin(repo, "code.M")
    denied = pre.decide(tool_payload(repo, "Write", {"file_path": str(repo / "src" / "app.py"), "content": "x"}, agent_type="plumbline:prosecutor"))
    assert "{spec-review,test-review,review}/round-<n>/prosecutor-<lens>.json" in denied


# --- the skill and the README


def test_the_run_skill_says_how_the_request_is_stored_and_what_a_failed_spec_review_does():
    body = frontmatter(REPO / "skills" / "run" / "SKILL.md")[1]
    start_section = between(body, "## 3. Start the run", "## 4. The stages")
    assert "Write the request, as the builder gave it, to a file too (`.plumbline/request.md` is a good place: git ignores it)" in start_section
    assert "PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>] --request-file <file>" in start_section
    assert "stores the request as the run's `request.md` (its hash goes into the ledger first, as the intake record's does)" in start_section and "the `request` file" in start_section
    assert "From the second round of the spec review, the text of the surviving findings that `merge-review` printed under \"for the planner\", verbatim." in between(body, "## 4. The stages", "## 5. Review units")
    assert "for the review of the spec, the plan record against the run's `request.md`" in between(body, "1. **Prosecutors.**", "2. **Defenders.**")
    failing = between(body, "## 6. When a gate fails", "## 7. Reduce")
    assert "`spec-review` goes back to `plan`. Send the planner the text under \"for the planner\"" in failing
    assert "Where the intent supplied the spec, `plan` is not in the run, the failure goes to the builder (`on_fail` is `main`), and a corrected spec means a new run." in failing


def test_the_readme_documents_the_spec_review_the_request_file_and_the_planner_route():
    plan_row = next(line for line in section("The command line", 3).splitlines() if line.startswith("| `plan "))
    assert plan_row.startswith("| `plan [--project PATH] [--base REF] [--run-id ID] [--intent ID [--spec FILE] [--request-file FILE]")
    assert "stores the request a `--request-file` holds as `request.md` (its hash goes into the ledger before the file exists, as the intake record's does)" in plan_row
    runs = section("Runs, gates and the pass record")
    assert ".plumbline/runs/<run-id>/request.md" in runs
    ledger_rows = {r[0].strip("`"): r for r in rows(runs.split("**The ledger**", 1)[1].split("**Provenance.**", 1)[0])}
    assert "request" in ledger_rows and "plan --intent" in ledger_rows["request"][1] and "`record_sha256` (entered before the file exists)" in ledger_rows["request"][2]
    paragraph = runs.split("**The spec review.**", 1)[1].split("\n\n", 1)[0]
    for needed in (
        "Nothing else checks the spec against the request", "`spec-review` stage, directly after `plan`", "the rows of size M and L, and not size S",
        "the `requirements` lens", "missing criteria, a misread intent, contradictions and criteria no test could check", "BLOCKING when the spec contradicts, or leaves out, something the request asks for explicitly",
        "`defenders = 3` and `screen_defenders = 1`", "`on_fail = \"plan\"`", "`max_rounds = 2`", "The `refactor` and `review-only` intents skip it",
        "finding of the requirements lens, and any finding on the plan record, goes to the planner", "`routes` has a `planner` list", "needs the request as `plan --request-file` stored it",
    ):
        assert needed in paragraph, needed
    assert "In the default pipeline `plan` has 2 rounds, `spec-review` 2, `tests` 3" in README
    intents = {r[0].strip("`"): r for r in rows(section("Intents", 3))}
    assert intents["refactor"][1] == "`plan`, `spec-review`, `tests`, `test-review`" and intents["review-only"][1] == "`plan`, `spec-review`, `tests`, `test-review`, `build`"
