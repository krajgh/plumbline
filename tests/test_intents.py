"""The intent axis: why a change is made, next to its type and size. The row selects stages; the intent then removes
stages, or supplies the record a removed stage would have written, which is validated at intake."""
import json

import pytest

import plumbline as pl
from helpers import DEFAULT_TOML, commit_all, default_pipeline, git, numbered, write
from rundata import (
    CONTROLLED, RUN, adopt, adopt_base, build_note_record, change_of, put, put_part, read, review_record, run_entry, run_path, set_exit_code,
    spec_record, verify_record, write_test_file, written_tests_record,
)

INTENTS = ("feature", "spec-supplied", "fix", "refactor", "review-only")


def check(mutate):
    pipeline = default_pipeline()
    mutate(pipeline)
    return pl.validate_pipeline(pipeline)


def stage_ids(plan_):
    return [s["id"] for s in plan_["stages"]]


# --- the intents of the spec, as data


def test_the_default_pipeline_has_the_five_intents_of_the_spec_and_validates():
    intents = default_pipeline()["intent"]
    assert list(intents) == list(INTENTS)
    assert intents["feature"] == {"skip": []}
    assert intents["spec-supplied"] == {"skip": ["plan"], "supplies": ["plan"]}
    assert intents["fix"] == {"skip": ["plan"], "supplies": ["plan"], "gates": {"tests": "reproduces_on_head"}}
    assert intents["refactor"] == {"skip": ["plan", "tests", "test-review"], "supplies": ["plan"], "lenses": ["correctness", "boundaries"]}
    assert intents["review-only"] == {"skip": ["plan", "tests", "test-review", "build"]}
    errors, notes = pl.validate_pipeline(default_pipeline())
    assert errors == []
    assert notes == []  # the intents add no notes, and the rows that have no build stage say where a failure goes


def test_reproduces_on_head_joins_the_known_gates():
    assert "reproduces_on_head" in pl.KNOWN_GATES
    assert "reproduces_on_head" in pl.GATE_CHECKS
    assert pl.GATE_RECORDS["reproduces_on_head"] == "tests_record"


# (id, mutation, text of an error)
BAD_INTENTS = [
    ("unknown-stage-in-skip", lambda d: d["intent"]["fix"]["skip"].append("ghost"), "intent 'fix': skip names unknown stage 'ghost'"),
    ("unknown-stage-in-supplies", lambda d: d["intent"]["fix"]["supplies"].append("ghost"), "intent 'fix': supplies names unknown stage 'ghost'"),
    ("unknown-stage-in-gates", lambda d: d["intent"]["fix"]["gates"].update(ghost="verify_green"), "intent 'fix': gates names unknown stage 'ghost'"),
    ("unknown-gate", lambda d: d["intent"]["fix"]["gates"].update(tests="vibes"), "intent 'fix': unknown gate 'vibes'"),
    ("unknown-lens", lambda d: d["intent"]["refactor"]["lenses"].append("vibes"), "intent 'refactor': unknown lens 'vibes'"),
    ("empty-lenses", lambda d: d["intent"]["refactor"].update(lenses=[]), "intent 'refactor': lenses must be a non-empty list"),
    (
        "a-required-read-not-covered-by-supplies",
        lambda d: d["intent"]["fix"].pop("supplies"),
        "intent 'fix', row 'code.S': stage 'tests' reads 'plan', which no stage left in the row produces and the intent does not supply",
    ),
    (
        "a-skipped-stage-that-later-stages-read",
        lambda d: d["intent"]["review-only"].update(skip=["plan", "tests", "test-review"]),  # build stays, and reads plan
        "intent 'review-only', row 'code.M': stage 'build' reads 'plan'",
    ),
    (
        "verify-must-still-be-readable-by-review",
        lambda d: d["intent"].update(quick={"skip": ["verify"]}),  # review reads verify, which is required
        "intent 'quick', row 'code.S': stage 'review' reads 'verify'",
    ),
    ("supplies-a-stage-that-is-not-skipped", lambda d: d["intent"]["fix"].update(skip=[]), "intent 'fix': supplies names 'plan', which the intent does not skip"),
    (
        "supplies-a-record-that-is-not-a-spec",
        lambda d: d["intent"]["review-only"].update(supplies=["build"]),
        "supplies names 'build', which writes a build_note record; only a spec can be supplied",
    ),
    ("gates-a-skipped-stage", lambda d: d["intent"]["review-only"].update(gates={"build": "verify_green"}), "gates names 'build', which the intent skips"),
    (
        "a-gate-that-reads-another-record",
        lambda d: d["intent"]["fix"]["gates"].update(tests="verify_green"),
        "gate 'verify_green' reads a verify_record record, but stage 'tests' writes a tests_record",
    ),
    ("skipping-the-intake", lambda d: d["intent"]["feature"].update(skip=["intake"]), "intent 'feature': skip names 'intake', which every run needs"),
    ("skipping-the-reduce", lambda d: d["intent"]["feature"].update(skip=["reduce"]), "intent 'feature': skip names 'reduce', which every run needs"),
    ("an-unexpected-key", lambda d: d["intent"]["fix"].update(reads=["x"]), "intent 'fix': unexpected key 'reads'"),
    ("skip-not-a-list", lambda d: d["intent"]["fix"].update(skip="plan"), "intent 'fix': skip must be a list of stage ids"),
    ("gates-not-a-table", lambda d: d["intent"]["fix"].update(gates=["tests"]), "intent 'fix': gates must map stage ids to gate names"),
    ("a-bad-id", lambda d: d["intent"].update({"Bad Id": {"skip": []}}), "intent 'Bad Id': the id must match"),
    ("not-a-table", lambda d: d["intent"].update(odd=["skip"]), "intent 'odd': must be a table"),
]


@pytest.mark.parametrize("mutate,expected", [(m, e) for _, m, e in BAD_INTENTS], ids=[i for i, _, _ in BAD_INTENTS])
def test_a_bad_intent_is_an_error_naming_the_intent(mutate, expected):
    errors, _ = check(mutate)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_every_row_is_checked_against_every_intent_not_only_the_first():
    errors, _ = check(lambda d: d["intent"]["fix"].pop("supplies"))
    rows = {e.split("row '")[1].split("'")[0] for e in errors if e.startswith("intent 'fix', row")}
    assert rows == {"code.S", "code.M"}  # the rows that include the tests stage; docs, config, tests and code.L do not


def test_an_intent_may_omit_skip_and_a_pipeline_may_define_none():
    errors, _ = check(lambda d: d["intent"].update(quiet={"lenses": ["docs"]}))
    assert errors == []
    without = default_pipeline()
    del without["intent"]
    assert pl.validate_pipeline(without)[0] == []
    assert pl.intent_ids(without) == ["feature"] and pl.intent_table(without, "feature") == {}  # feature is the implicit default


def test_a_stage_that_is_missing_from_a_row_is_not_an_error_when_the_intent_skips_it():
    # review-only skips plan, tests, test-review and build: rows that never had them are fine
    errors, _ = pl.validate_pipeline(default_pipeline())
    assert errors == []
    assert pl.effective_row(default_pipeline(), "docs", "review-only").stages == ["intake", "verify", "review", "reduce"]


def test_the_effective_row_applies_skip_supplies_gates_and_lenses():
    pipeline = default_pipeline()
    fix = pl.effective_row(pipeline, "code.M", "fix")
    assert fix.stages == ["intake", "tests", "test-review", "build", "verify", "review", "reduce"]
    assert (fix.supplied, fix.gates, fix.lenses) == (["plan"], {"tests": "reproduces_on_head"}, None)
    refactor = pl.effective_row(pipeline, "code.M", "refactor")
    assert refactor.stages == ["intake", "build", "verify", "review", "reduce"]
    assert refactor.lenses == ["correctness", "boundaries"]  # code.M sets no lenses of its own: the intent's stand alone
    assert pl.effective_row(pipeline, "code.S", "refactor").lenses == ["correctness", "tests", "boundaries"]  # code.S has its own: the intent's are added
    assert pl.effective_row(pipeline, "code.S", "feature").lenses == ["correctness", "tests"]
    assert pl.effective_row(pipeline, "code.M", "feature").stages == pipeline["matrix"]["code"]["M"]["stages"]
    with pytest.raises(pl.PlumblineError, match="unknown intent 'nope'; the intents are feature, spec-supplied, fix, refactor, review-only"):
        pl.effective_row(pipeline, "code.M", "nope")
    with pytest.raises(pl.PlumblineError, match="'code' is not a row"):
        pl.effective_row(pipeline, "code", "feature")


@pytest.mark.parametrize(
    "row,lenses",
    [
        ("config", ["security", "boundaries", "correctness"]),  # a refactor of a workflow file keeps the security lens
        ("docs", ["docs", "correctness", "boundaries"]),
        ("tests", ["tests", "correctness", "boundaries"]),
        ("code.S", ["correctness", "tests", "boundaries"]),
        ("code.M", ["correctness", "boundaries"]),
    ],
)
def test_an_intents_lenses_are_added_to_the_rows_not_put_in_their_place(row, lenses):  # C-13
    assert pl.effective_row(default_pipeline(), row, "refactor").lenses == lenses


def test_an_intent_without_lenses_leaves_the_rows_alone():
    pipeline = default_pipeline()
    assert pl.effective_row(pipeline, "config", "feature").lenses == ["security", "boundaries"]
    assert pl.effective_row(pipeline, "code.M", "feature").lenses is None


# --- plan --intent


@pytest.fixture
def code_m(repo):
    """An adopted repository (a run starts only in one; the base branch has plumbline.toml) with a 100-line change: row code.M."""
    adopt_base(repo)
    write(repo / "src" / "new_module.py", numbered(100))
    return repo


SPEC = ".plumbline/supplied-spec.json"  # inside .plumbline/, so that the spec file is neither part of the change nor a dirty file


def spec_file(repo, record=None, name=SPEC):
    write(repo / name, json.dumps(record if record is not None else spec_record()))
    return name


def fix_spec():
    record = spec_record(planned=("AC-1",))
    record["acceptance_criteria"] = record["acceptance_criteria"][:1]
    return record


def start(run_cli, repo, *args, run_id="r1"):
    return run_cli("plan", "--base", "main", "--run-id", run_id, *args, cwd=repo)


def started(run_cli, repo, *args, run_id="r1"):
    result = start(run_cli, repo, *args, run_id=run_id)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_plan_without_an_intent_is_the_feature_plan_and_writes_nothing(run_cli, code_m):
    plan = started(run_cli, code_m)
    assert plan["intent"] == "feature" and plan["supplied"] == []
    assert stage_ids(plan) == ["intake", "plan", "tests", "test-review", "build", "verify", "review", "reduce"]
    assert not (code_m / ".plumbline").exists()


def test_feature_starts_a_run_with_the_planner_and_writes_the_intake_record_carrying_the_intent(run_cli, code_m):
    plan = started(run_cli, code_m, "--intent", "feature")
    assert plan["intent"] == "feature" and plan["supplied"] == []
    assert stage_ids(plan) == ["intake", "plan", "tests", "test-review", "build", "verify", "review", "reduce"]
    intake = read(code_m, "intake")
    assert pl.check_record("change_class", intake) == []
    assert (intake["intent"], intake["row"]) == ("feature", "code.M")
    assert not run_path(code_m, RUN, "plan.json").exists()  # the planner writes it


def test_spec_supplied_skips_the_planner_and_copies_the_spec_as_the_plan_record(run_cli, code_m):
    given = spec_record()
    plan = started(run_cli, code_m, "--intent", "spec-supplied", "--spec", spec_file(code_m, given))
    assert plan["intent"] == "spec-supplied"
    assert stage_ids(plan) == ["intake", "tests", "test-review", "build", "verify", "review", "reduce"]
    assert plan["supplied"] == [{"stage": "plan", "record": "spec", "path": ".plumbline/runs/r1/plan.json", "source": SPEC}]
    assert read(code_m, "plan") == given  # validated, then copied as it is
    assert read(code_m, "intake")["intent"] == "spec-supplied"
    # what reads the plan finds it where the plan says
    tests = next(s for s in plan["stages"] if s["id"] == "tests")
    assert tests["reads"] == [{"name": "plan", "kind": "record", "path": ".plumbline/runs/r1/plan.json", "optional": False}]


def test_fix_skips_the_planner_supplies_a_one_criterion_spec_and_replaces_the_tests_gate(run_cli, code_m):
    given = fix_spec()
    plan = started(run_cli, code_m, "--intent", "fix", "--spec", spec_file(code_m, given))
    assert stage_ids(plan) == ["intake", "tests", "test-review", "build", "verify", "review", "reduce"]
    tests = next(s for s in plan["stages"] if s["id"] == "tests")
    assert tests["gate"] == "reproduces_on_head"
    assert next(s for s in plan["stages"] if s["id"] == "verify")["gate"] == "verify_green"  # the others keep their own
    assert read(code_m, "plan") == given and read(code_m, "intake")["intent"] == "fix"


def test_refactor_uses_the_shipped_template_and_skips_the_planner_the_tests_and_their_review(run_cli, code_m):
    plan = started(run_cli, code_m, "--intent", "refactor")
    assert stage_ids(plan) == ["intake", "build", "verify", "review", "reduce"]
    template = json.loads((pl.PIPELINE_DIR / "templates" / "refactor.json").read_text(encoding="utf-8"))
    assert read(code_m, "plan") == template
    assert plan["supplied"][0]["source"] == "the shipped template pipeline/templates/refactor.json"
    review = next(s for s in plan["stages"] if s["id"] == "review")
    assert review["lenses"] == ["correctness", "boundaries"]
    assert read(code_m, "intake")["intent"] == "refactor"
    assert [r["path"] for r in next(s for s in plan["stages"] if s["id"] == "build")["reads"]] == [".plumbline/runs/r1/plan.json"]


def test_the_refactor_template_is_a_complete_spec_that_says_behaviour_is_unchanged_and_the_tests_pass():
    template = json.loads((pl.PIPELINE_DIR / "templates" / "refactor.json").read_text(encoding="utf-8"))
    assert pl.check_record("spec", template) == []
    assert pl._gate_spec_complete(template, None) == []
    text = json.dumps(template).lower()
    assert "behaviour of the code is unchanged" in text and "every existing test passes" in text
    assert template["split_proposal"] is None and template["interfaces"] == []


def test_review_only_skips_everything_that_builds_and_supplies_nothing(run_cli, code_m):
    plan = started(run_cli, code_m, "--intent", "review-only")
    assert stage_ids(plan) == ["intake", "verify", "review", "reduce"]
    assert plan["supplied"] == []
    assert sorted(p.name for p in run_path(code_m, RUN).iterdir()) == ["intake.json", "ledger.jsonl"]  # the ledger holds the intake record's hash
    verify = next(s for s in plan["stages"] if s["id"] == "verify")
    assert verify["on_fail"] == "main"  # its build stage is not in the run: it fails over to main
    assert [r["name"] for r in verify["reads"] if r["path"]] == []


def test_plan_reports_the_repos_commands_and_graft(run_cli, code_m):
    write(code_m / "plumbline.toml", 'schema = 1\n\n[graft]\nenabled = true\n\n[commands]\ntest = "python3 -m pytest"\nlint = ["ruff check"]\n')
    plan = started(run_cli, code_m)
    assert plan["commands"] == {"test": ["python3 -m pytest"], "lint": ["ruff check"]} and plan["graft"] is True


# --- what --spec accepts and refuses


def test_an_invalid_spec_is_refused_with_its_errors_and_nothing_is_written(run_cli, code_m):
    bad = spec_record()
    del bad["goal"]
    bad["acceptance_criteria"][0]["id"] = "AC1"
    result = start(run_cli, code_m, "--intent", "spec-supplied", "--spec", spec_file(code_m, bad))
    assert result.returncode == 1
    assert f"error: {SPEC}: $.goal: missing required key" in result.stdout
    assert f"error: {SPEC}: $.acceptance_criteria[0].id: 'AC1' does not match ^AC-[0-9]+$" in result.stdout
    assert "the spec for intent 'spec-supplied' does not hold (2 problems); nothing was written" in result.stdout
    assert not run_path(code_m, RUN).exists()


def test_a_spec_that_is_not_complete_is_refused_like_the_planners_would_be(run_cli, code_m):
    result = start(run_cli, code_m, "--intent", "fix", "--spec", spec_file(code_m, spec_record(planned=("AC-1",))))  # AC-2 has no test plan entry
    assert result.returncode == 1 and "AC-2 has no test_plan entry" in result.stdout
    hollow = spec_record()
    hollow["acceptance_criteria"], hollow["test_plan"] = [], []
    result = start(run_cli, code_m, "--intent", "fix", "--spec", spec_file(code_m, hollow))
    assert result.returncode == 1 and "the spec has no acceptance criteria" in result.stdout
    assert not run_path(code_m, RUN).exists()


def test_a_spec_that_is_not_json_is_refused_and_one_that_is_missing_could_not_be_read(run_cli, code_m):
    write(code_m / ".plumbline" / "broken.json", "{not json")
    result = start(run_cli, code_m, "--intent", "fix", "--spec", ".plumbline/broken.json")
    assert result.returncode == 1 and ".plumbline/broken.json: not valid JSON" in result.stdout
    result = start(run_cli, code_m, "--intent", "fix", "--spec", ".plumbline/nope.json")
    assert result.returncode == 2 and ".plumbline/nope.json: no such file" in result.stderr
    write(code_m / ".plumbline" / "list.json", "[]")
    result = start(run_cli, code_m, "--intent", "fix", "--spec", ".plumbline/list.json")
    assert result.returncode == 1 and "expected object, got array" in result.stdout


def test_a_fix_takes_a_spec_of_exactly_one_acceptance_criterion(run_cli, code_m):  # C-15
    result = start(run_cli, code_m, "--intent", "fix", "--spec", spec_file(code_m, spec_record()))  # two criteria
    assert result.returncode == 1
    assert f"error: {SPEC}: the intent 'fix' reproduces one bug, so its spec has exactly one acceptance criterion (this one has 2)" in result.stdout
    assert "the spec for intent 'fix' does not hold (1 problem); nothing was written" in result.stdout
    assert not run_path(code_m, RUN).exists()
    many = spec_record()
    many["acceptance_criteria"] = many["acceptance_criteria"] * 3
    many["test_plan"] = many["test_plan"] * 3
    result = start(run_cli, code_m, "--intent", "fix", "--spec", spec_file(code_m, many))
    assert result.returncode == 1 and "(this one has 6)" in result.stdout and "defined more than once" in result.stdout
    assert start(run_cli, code_m, "--intent", "fix", "--spec", spec_file(code_m, fix_spec())).returncode == 0


def test_a_spec_supplied_run_may_have_any_number_of_criteria(run_cli, code_m):
    assert start(run_cli, code_m, "--intent", "spec-supplied", "--spec", spec_file(code_m, spec_record())).returncode == 0


def test_the_spec_gate_of_a_fix_run_holds_the_spec_to_one_criterion_too(run_cli, adopted):  # C-15
    started(run_cli, adopted, "--intent", "fix", "--spec", spec_file(adopted, fix_spec()), "--row", "code.S")
    put(adopted, "plan", spec_record(), agent=False)  # a two-criterion spec put in place of the supplied one
    result = run_cli("gate", RUN, "plan", cwd=adopted)
    assert result.returncode == 1 and "its record changed after `plan --intent` supplied it" in result.stdout
    assert pl._gate_spec_complete(spec_record(), None) == []  # without a run there is no intent to hold it to
    run = pl.load_run(pl.load_project(adopted), RUN)
    assert pl._gate_spec_complete(spec_record(), pl.GateContext(pl.load_project(adopted), RUN, {"id": "plan", "gate": "spec_complete"}, run, [], False)) == [
        "the intent 'fix' reproduces one bug, so its spec has exactly one acceptance criterion (this one has 2)"
    ]
    assert pl.single_ac_problems("feature", spec_record()) == [] and pl.single_ac_problems("fix", fix_spec()) == []


@pytest.mark.parametrize("intent", ["spec-supplied", "fix"])
def test_an_intent_that_supplies_a_spec_needs_one(run_cli, code_m, intent):
    result = start(run_cli, code_m, "--intent", intent)
    assert result.returncode == 2 and f"the intent '{intent}' supplies the spec: pass --spec FILE" in result.stderr
    assert not run_path(code_m, RUN).exists()


@pytest.mark.parametrize("intent", ["feature", "review-only"])
def test_an_intent_that_supplies_nothing_takes_no_spec(run_cli, code_m, intent):
    result = start(run_cli, code_m, "--intent", intent, "--spec", spec_file(code_m))
    assert result.returncode == 2 and f"the intent '{intent}' supplies no record, so --spec does not apply" in result.stderr


def test_refactor_uses_its_template_and_takes_no_spec(run_cli, code_m):
    result = start(run_cli, code_m, "--intent", "refactor", "--spec", spec_file(code_m))
    assert result.returncode == 2 and "uses its shipped template" in result.stderr and "so --spec does not apply" in result.stderr


def test_a_spec_goes_with_an_intent(run_cli, code_m):
    result = start(run_cli, code_m, "--spec", spec_file(code_m))
    assert result.returncode == 2 and "--spec goes with --intent" in result.stderr


def test_an_unknown_intent_is_refused_with_the_known_ones(run_cli, code_m):
    for command in ("plan", "classify"):
        result = run_cli(command, "--base", "main", "--intent", "nope", cwd=code_m)
        assert result.returncode == 2 and "unknown intent 'nope'; the intents are feature, spec-supplied, fix, refactor, review-only" in result.stderr


# --- the run: a run that has begun, and a start that failed halfway


def test_a_run_that_has_begun_is_never_restarted(run_cli, code_m):
    started(run_cli, code_m, "--intent", "feature")
    before = read(code_m, "intake")
    again = start(run_cli, code_m, "--intent", "review-only")
    assert again.returncode == 1 and "run 'r1' has begun" in again.stderr and "pick another --run-id" in again.stderr
    assert read(code_m, "intake") == before
    assert start(run_cli, code_m, "--intent", "review-only", run_id="r2").returncode == 0


def test_a_start_that_failed_before_the_intake_record_can_be_redone(run_cli, code_m):
    write(run_path(code_m, RUN, "plan.json"), "{leftover")  # a supplied record without an intake record: the run never began
    plan = started(run_cli, code_m, "--intent", "spec-supplied", "--spec", spec_file(code_m))
    assert read(code_m, "plan") == spec_record() and read(code_m, "intake")["intent"] == "spec-supplied" and plan["run_id"] == RUN


def test_the_supplied_record_is_entered_in_the_ledger_and_its_gate_evaluated(run_cli, code_m):
    started(run_cli, code_m, "--intent", "spec-supplied", "--spec", spec_file(code_m))
    rows = pl.read_ledger(code_m, RUN)
    assert [(r["kind"], r["stage"]) for r in rows] == [("supplied", "plan"), ("gate", "plan"), ("intake", "intake")]
    assert rows[0]["source"] == SPEC and rows[0]["record_sha256"] == pl.file_sha256(run_path(code_m, RUN, "plan.json"))
    assert (rows[1]["gate"], rows[1]["passed"]) == ("spec_complete", True)
    assert rows[2]["record_sha256"] == pl.file_sha256(run_path(code_m, RUN, "intake.json")) and rows[2]["merge_base"] == read(code_m, "intake")["merge_base"]


# --- a change that does not exist yet, and --row


def test_plan_with_an_intent_starts_a_run_only_in_a_repository_that_has_adopted_plumbline(run_cli, repo):  # C-16
    result = start(run_cli, repo, "--intent", "feature", "--row", "code.S")
    assert result.returncode == 2 and "has not adopted plumbline" in result.stderr and "/plumbline:init adopts it" in result.stderr
    assert not (repo / ".plumbline").exists()  # nothing was left behind, un-ignored and untracked
    assert start(run_cli, repo, "--row", "code.S").returncode == 0  # a plan that starts nothing needs no adoption
    assert not (repo / ".plumbline").exists()


def test_nothing_to_classify_says_how_to_declare_the_row(run_cli, repo):
    adopt_base(repo)
    result = start(run_cli, repo, "--intent", "feature")
    assert result.returncode == 2 and "nothing to classify" in result.stderr and "--row ROW" in result.stderr


def test_a_declared_row_stands_in_for_a_change_that_does_not_exist_yet(run_cli, repo):
    adopt_base(repo)
    plan = started(run_cli, repo, "--intent", "feature", "--row", "code.M")
    assert (plan["row"], plan["size"], plan["lines"], plan["types"]) == ("code.M", "M", 0, ["code"])
    assert stage_ids(plan) == ["intake", "plan", "tests", "test-review", "build", "verify", "review", "reduce"]
    intake = read(repo, "intake")
    assert intake["files"] == [] and intake["notes"] == ["nothing has changed yet, so the row code.M was declared, not measured"]
    assert pl.check_record("change_class", intake) == []


def test_a_flat_row_is_declared_by_its_type_and_a_split_row_needs_its_size(run_cli, repo):
    adopt_base(repo)
    plan = started(run_cli, repo, "--intent", "review-only", "--row", "docs")
    assert (plan["row"], plan["size"], stage_ids(plan)) == ("docs", "S", ["intake", "verify", "review", "reduce"])
    result = run_cli("classify", "--base", "main", "--row", "code", cwd=repo)
    assert result.returncode == 2 and "'code' is not a row of this pipeline (the rows are docs, config, tests, code.S, code.M, code.L)" in result.stderr
    assert run_cli("classify", "--base", "main", "--row", "code.XL", cwd=repo).returncode == 2


def test_a_declared_row_with_a_change_says_what_the_change_measures_as(run_cli, code_m):
    plan = started(run_cli, code_m, "--intent", "feature", "--row", "code.S")
    assert plan["row"] == "code.S" and plan["lines"] == 100 and plan["size"] == "M"
    assert "the row code.S was declared; the change measures as code.M" in read(code_m, "intake")["notes"]


def test_classify_carries_the_intent(run_cli, code_m):
    result = run_cli("classify", "--base", "main", cwd=code_m)
    assert json.loads(result.stdout)["intent"] == "feature"
    result = run_cli("classify", "--base", "main", "--intent", "refactor", cwd=code_m)
    record = json.loads(result.stdout)
    assert record["intent"] == "refactor" and pl.check_record("change_class", record) == []


def test_the_intake_record_must_carry_a_well_formed_intent():
    from samples import sample

    record = sample("change_class")
    del record["intent"]
    assert pl.check_record("change_class", record) == ["$.intent: missing required key"]
    record["intent"] = "Not An Id"
    assert any(e.startswith("$.intent:") and "does not match" in e for e in pl.check_record("change_class", record))


# --- a run under an intent: gates, lenses, and pass


@pytest.fixture
def adopted(repo):
    adopt(repo, commands={"test": CONTROLLED})
    return repo


def test_the_fix_gate_needs_the_new_tests_to_fail_on_an_assertion_against_todays_code(run_cli, adopted):
    started(run_cli, adopted, "--intent", "fix", "--spec", spec_file(adopted, fix_spec()), "--row", "code.S")
    write_test_file(adopted, covering=("AC-1",))
    set_exit_code(adopted, 1)  # the test command fails on today's code
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=True))
    ok = run_cli("gate", RUN, "tests", cwd=adopted)
    assert ok.returncode == 0 and "gate reproduces_on_head for stage 'tests': pass" in ok.stdout
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=False))
    bad = run_cli("gate", RUN, "tests", cwd=adopted)
    assert bad.returncode == 1 and "gate reproduces_on_head for stage 'tests': FAIL" in bad.stdout
    assert "not every new test fails on an assertion against today's code" in bad.stdout
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=False, all_failed=False))
    assert "were not run against today's code" in run_cli("gate", RUN, "tests", cwd=adopted).stdout
    put(adopted, "tests", written_tests_record(covering=(), ran=True, all_failed=True))
    out = run_cli("gate", RUN, "tests", cwd=adopted).stdout
    assert "AC-1 is covered by no test" in out and "there are no tests" in out  # it keeps the gate it replaces
    ledger = [r for r in pl.read_ledger(adopted, RUN) if r["kind"] == "gate" and r["stage"] == "tests"]
    assert {r["gate"] for r in ledger} == {"reproduces_on_head"}  # the ledger names the gate that was evaluated


def test_the_fix_gate_measures_the_same_run_and_a_test_command_that_passes_reproduces_nothing(run_cli, adopted):
    started(run_cli, adopted, "--intent", "fix", "--spec", spec_file(adopted, fix_spec()), "--row", "code.S")
    write_test_file(adopted, covering=("AC-1",))
    put(adopted, "tests", written_tests_record(covering=("AC-1",), ran=True, all_failed=True))  # the record says it fails; the command says it does not
    set_exit_code(adopted, 0)
    out = run_cli("gate", RUN, "tests", cwd=adopted)
    assert out.returncode == 1 and "the test command exited 0: every test passed, so none of them fails without the change it tests" in out.stdout


def test_a_feature_run_gates_its_tests_with_tests_fail_on_stub_which_measures_the_test_command(run_cli, adopted):
    started(run_cli, adopted, "--intent", "feature", "--row", "code.S")
    put(adopted, "plan", spec_record())
    write_test_file(adopted)
    put(adopted, "tests", written_tests_record())
    set_exit_code(adopted, 1)
    assert "gate tests_fail_on_stub for stage 'tests': pass" in run_cli("gate", RUN, "tests", cwd=adopted).stdout
    set_exit_code(adopted, 0)
    assert run_cli("gate", RUN, "tests", cwd=adopted).returncode == 1


def test_the_gate_of_a_stage_outside_the_runs_row_is_not_replaced_by_a_guess(run_cli, adopted):
    started(run_cli, adopted, "--intent", "review-only", "--row", "code.S")
    put(adopted, "plan", spec_record())
    put(adopted, "tests", written_tests_record(covering=("AC-1",)))  # AC-2 is not covered
    result = run_cli("gate", RUN, "tests", cwd=adopted)  # tests is not in the run; the pipeline's own gate applies
    assert result.returncode == 1 and "gate tests_fail_on_stub" in result.stdout


def review_lenses(record):
    return sorted(f["lens"] for f in record["findings"])


def test_a_refactor_reviews_the_diff_through_its_own_lenses(run_cli, adopted):
    started(run_cli, adopted, "--intent", "refactor", "--row", "code.M")
    run = pl.load_run(pl.load_project(adopted), RUN)
    assert run.lenses == ["correctness", "boundaries"] and run.intent == "refactor"
    put_part(adopted, "review", "prosecutor-correctness", {"lens": "correctness", "findings": []})
    result = run_cli("merge-review", RUN, "review", cwd=adopted)
    assert result.returncode == 1 and "no findings_record for lens 'boundaries'" in result.stdout
    assert "no findings_record for lens 'security'" not in result.stdout  # the stage's own five lenses do not apply
    put_part(adopted, "review", "prosecutor-boundaries", {"lens": "boundaries", "findings": []})
    for k in (1, 2, 3):
        put_part(adopted, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []})
    assert run_cli("merge-review", RUN, "review", cwd=adopted).returncode == 0
    assert read(adopted, "review")["lenses"] == ["correctness", "boundaries"]


def test_a_refactor_passes_with_its_supplied_plan_listed_at_zero_rounds(run_cli, adopted):
    started(run_cli, adopted, "--intent", "refactor", "--row", "code.M")
    diff = change_of(adopted)
    put(adopted, "build", build_note_record())
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff)
    put(adopted, "review", review_record(diff=diff))
    result = run_cli("pass", RUN, cwd=adopted)
    assert result.returncode == 0, result.stdout + result.stderr
    record = read(adopted, "reduce")
    assert [(s["id"], s["gate"], s["passed"], s["rounds"]) for s in record["stages"]] == [
        ("intake", None, True, 1),
        ("plan", "spec_complete", True, 0),
        ("build", None, True, 1),
        ("verify", "verify_green", True, 1),
        ("review", "no_surviving_blockers", True, 1),
        ("reduce", "all_gates_passed", True, 1),
    ]
    assert any("intent refactor: the record of plan was supplied, not written by an agent" in n for n in record["notes"])
    assert record["row"] == "code.M"


def test_pass_needs_the_supplied_record_to_still_be_the_one_that_was_supplied(run_cli, adopted):
    started(run_cli, adopted, "--intent", "spec-supplied", "--spec", spec_file(adopted), "--row", "code.S")
    write_test_file(adopted)
    commit_all(adopted, "the tests")
    diff = change_of(adopted)
    put(adopted, "tests", written_tests_record())
    run_entry(adopted, "tests", None, exit_code=1)
    put(adopted, "build", build_note_record())
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff)
    put(adopted, "review", review_record(diff=diff))
    plan_file = run_path(adopted, RUN, "plan.json")
    hollow = spec_record()
    hollow["acceptance_criteria"], hollow["test_plan"] = [], []
    put(adopted, "plan", hollow, agent=False)  # the supplied record was gutted after it was accepted
    result = run_cli("pass", RUN, cwd=adopted)
    assert result.returncode == 1 and "stage 'plan' (gate spec_complete): its record changed after `plan --intent` supplied it" in result.stdout
    pl.write_json_atomic(plan_file, spec_record())  # put back as it was supplied
    assert run_cli("pass", RUN, cwd=adopted).returncode == 0


def test_all_gates_passed_covers_a_supplied_record(run_cli, adopted):
    started(run_cli, adopted, "--intent", "refactor", "--row", "code.M")
    for stage in ("build", "verify", "review"):
        diff = change_of(adopted)
        put(adopted, stage, {"build": build_note_record(), "verify": verify_record(diff=diff), "review": review_record(diff=diff)}[stage])
    for stage in ("verify", "review"):
        assert run_cli("gate", RUN, stage, cwd=adopted).returncode == 0
    assert run_cli("gate", RUN, "reduce", cwd=adopted).returncode == 0
    run_path(adopted, RUN, "plan.json").unlink()
    failed = run_cli("gate", RUN, "reduce", cwd=adopted)
    assert failed.returncode == 1 and "stage 'plan': .plumbline/runs/r1/plan.json: no such file" in failed.stdout


def test_a_review_only_run_needs_no_planner_tests_or_builder_records(run_cli, adopted):
    started(run_cli, adopted, "--intent", "review-only", "--row", "docs")
    diff = change_of(adopted)
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff)
    put(adopted, "review", review_record(diff=diff))
    assert run_cli("pass", RUN, cwd=adopted).returncode == 0
    assert [s["id"] for s in read(adopted, "reduce")["stages"]] == ["intake", "verify", "review", "reduce"]


def test_status_shows_the_intent_and_the_supplied_stage(run_cli, adopted):
    started(run_cli, adopted, "--intent", "spec-supplied", "--spec", spec_file(adopted), "--row", "code.S")
    out = run_cli("status", cwd=adopted).stdout
    assert "run r1: row code.S, 6 stages, intent spec-supplied" in out
    lines = {l.split()[0]: l.split() for l in out.splitlines() if l.startswith("  ") and not l.startswith("      ")}
    assert lines["plan"][1:] == ["spec", "supplied", "spec_complete"]
    assert lines["tests"][2] == "missing"
    assert list(lines)[:2] == ["intake", "plan"]  # in the pipeline's order, the supplied stage among the others


def test_a_run_whose_intent_is_not_the_pipelines_could_not_be_read(run_cli, adopted):
    put(adopted, "intake", {**read_intake_of(adopted), "intent": "nope"})
    result = run_cli("gate", RUN, "reduce", cwd=adopted)
    assert result.returncode == 2 and "the run's intake record does not fit this repository's pipeline: unknown intent 'nope'" in result.stderr


def read_intake_of(repo):
    from rundata import intake_record

    return intake_record("code.S", repo)


def test_the_intent_of_an_older_intake_record_is_required(run_cli, adopted):
    record = read_intake_of(adopted)
    del record["intent"]
    put(adopted, "intake", record)
    result = run_cli("gate", RUN, "reduce", cwd=adopted)
    assert result.returncode == 2 and "intake record is not usable" in result.stderr and "$.intent: missing required key" in result.stderr


def test_the_shipped_templates_are_named_for_the_intents_that_use_them():
    templates = sorted(p.stem for p in pl.TEMPLATE_DIR.glob("*.json"))
    assert templates == ["refactor"]
    pipeline = default_pipeline()
    for name in templates:
        assert pipeline["intent"][name]["supplies"]  # a template stands in for a supplied record


def test_git_history_is_untouched_by_starting_a_run(run_cli, code_m):
    head = git(code_m, "rev-parse", "HEAD")
    started(run_cli, code_m, "--intent", "refactor")
    assert git(code_m, "rev-parse", "HEAD") == head
    assert "src/new_module.py" in git(code_m, "status", "--porcelain")  # the change is as it was, untracked
