"""Pipeline validation: the default is valid, and each rule catches a bad fixture."""
import pytest

import plumbline as pl
from helpers import DEFAULT_TOML, default_pipeline


def stage(d, sid):
    return next(s for s in d["stage"] if s["id"] == sid)


def check(mutate):
    d = default_pipeline()
    mutate(d)
    return pl.validate_pipeline(d)


def test_default_pipeline_validates_with_no_errors_and_no_notes():
    errors, notes = pl.validate_pipeline(default_pipeline())
    assert errors == [] and notes == []


def test_the_docs_config_and_tests_rows_say_where_a_failed_verify_or_review_goes():
    # they leave out build, the stage verify and review go back to, so the rows send the run to the main session themselves
    matrix = default_pipeline()["matrix"]
    for name in ("docs", "config", "tests"):
        assert matrix[name]["on_fail"] == {"verify": "main", "review": "main"}, name
        assert "build" not in matrix[name]["stages"]
    assert not any("on_fail" in matrix["code"][size] for size in "SML")  # the code rows include build, so the stages' own on_fail stands


def test_default_pipeline_via_the_cli(run_cli, tmp_path):
    result = run_cli("validate-pipeline", cwd=tmp_path)
    assert result.returncode == 0
    assert "pipeline 'default': valid (0 errors, 0 notes)" in result.stdout and "note:" not in result.stdout


def test_every_record_named_by_the_default_pipeline_has_a_schema():
    for st in default_pipeline()["stage"]:
        assert st["record"] in pl.record_types()


# (id, mutation, text that must appear in some error)
BAD_FIXTURES = [
    ("duplicate-stage-id", lambda d: stage(d, "plan").update(id="intake"), "stage id 'intake' is defined more than once"),
    ("unknown-role", lambda d: stage(d, "build").update(role="wizard"), "unknown role 'wizard'"),
    ("unknown-gate", lambda d: stage(d, "verify").update(gate="vibes"), "unknown gate 'vibes'"),
    ("unknown-kind", lambda d: stage(d, "build").update(kind="oracle"), "unknown kind 'oracle'"),
    ("unknown-lens-in-stage", lambda d: stage(d, "review")["lenses"].append("vibes"), "unknown lens 'vibes'"),
    ("unknown-lens-in-row", lambda d: d["matrix"]["docs"].update(lenses=["vibes"]), "row 'docs': unknown lens 'vibes'"),
    ("unknown-record", lambda d: stage(d, "build").update(record="nope"), "unknown record 'nope'"),
    ("review-without-lenses", lambda d: stage(d, "review").pop("lenses"), "needs a non-empty lenses list"),
    ("review-with-empty-lenses", lambda d: stage(d, "review").update(lenses=[]), "needs a non-empty lenses list"),
    ("review-without-target", lambda d: stage(d, "review").pop("target"), "needs a target"),
    ("review-target-unknown", lambda d: stage(d, "review").update(target="ghost"), "target 'ghost' must be an input or an earlier stage"),
    ("review-with-role", lambda d: stage(d, "review").update(role="verifier"), "a review stage has no role"),
    ("agent-without-role", lambda d: stage(d, "build").pop("role"), "an agent stage needs a role"),
    ("agent-with-review-key", lambda d: stage(d, "build").update(lenses=["tests"]), "'lenses' only applies to review stages"),
    ("on-fail-forward", lambda d: stage(d, "plan").update(on_fail="build", max_rounds=1), "on_fail 'build' must be an earlier stage"),
    ("on-fail-itself-for-a-review", lambda d: stage(d, "review").update(on_fail="review"), "a review stage cannot name itself in on_fail"),
    ("on-fail-unknown", lambda d: stage(d, "verify").update(on_fail="nowhere"), "on_fail names unknown stage 'nowhere'"),
    ("on-fail-review-stage", lambda d: stage(d, "build").update(on_fail="test-review", max_rounds=1), "is a review stage"),
    ("on-fail-without-max-rounds", lambda d: stage(d, "verify").pop("max_rounds"), "on_fail needs max_rounds"),
    ("max-rounds-zero", lambda d: stage(d, "verify").update(max_rounds=0), "$.stage[5].max_rounds"),
    ("survive-exceeds-defenders", lambda d: stage(d, "review").update(survive_if_unrefuted_by=4), "cannot exceed defenders"),
    ("row-reorders-stages", lambda d: d["matrix"]["docs"].update(stages=["intake", "review", "verify", "reduce"]), "definition order"),
    ("row-reads-stage-it-lacks", lambda d: d["matrix"]["docs"].update(stages=["intake", "review", "reduce"]), "reads 'verify', which is neither an input nor an earlier stage of this row"),
    ("row-unknown-stage", lambda d: d["matrix"]["docs"]["stages"].append("ghost"), "unknown stage 'ghost'"),
    ("row-lists-stage-twice", lambda d: d["matrix"]["docs"]["stages"].append("reduce"), "stage 'reduce' is listed twice"),
    ("row-lenses-without-diff-review", lambda d: d["matrix"]["code"]["L"].update(lenses=["docs"]), "includes no review stage whose target is diff"),
    ("row-flat-and-split", lambda d: d["matrix"]["code"].update(stages=["intake"]), "has both 'stages' and size keys"),
    ("row-unknown-key", lambda d: d["matrix"]["docs"].update(colour="red"), "unknown key 'colour'"),
    ("missing-matrix-row", lambda d: d["matrix"].pop("tests"), "type 'tests' has no matrix row"),
    ("split-row-missing-size", lambda d: d["matrix"]["code"].pop("L"), "missing size L"),
    ("matrix-row-without-type", lambda d: d["matrix"].update(ghost={"stages": ["intake"]}), "matrix row 'ghost' does not belong to any type"),
    ("precedence-missing-type", lambda d: d["precedence"].remove("docs"), "precedence is missing type 'docs'"),
    ("precedence-unknown-type", lambda d: d["precedence"].append("ghost"), "precedence names unknown type 'ghost'"),
    ("precedence-repeats-type", lambda d: d["precedence"].append("code"), "precedence names type 'code' more than once"),
    ("duplicate-type", lambda d: d["type"].insert(0, dict(d["type"][0])), "type id 'docs' is defined more than once"),
    ("last-type-does-not-match-everything", lambda d: d["type"][-1].update(paths=["src/**"]), "must match every path"),
    ("schema-not-1", lambda d: d.update(schema=2), "$.schema: must be 1"),
    ("missing-top-level-key", lambda d: d.pop("sizes"), "$.sizes: missing required key"),
    ("unknown-top-level-key", lambda d: d.update(extra=1), "$.extra: unexpected key"),
    ("typo-in-stage-key", lambda d: stage(d, "verify").update(on_fial="build"), "unexpected key"),
    ("uppercase-stage-id", lambda d: stage(d, "build").update(id="Build"), "does not match"),
    ("read-of-unknown-name", lambda d: stage(d, "plan")["reads"].append("ghost"), "reads unknown name 'ghost'"),
    ("optional-read-of-unknown-name", lambda d: stage(d, "verify")["reads"].append("ghost?"), "reads unknown name 'ghost'"),
    ("read-of-later-stage", lambda d: stage(d, "plan")["reads"].append("build"), "'build', which is not defined before it"),
    ("input-named-like-a-stage", lambda d: d["inputs"].append("plan"), "input 'plan' has the same name as a stage"),
    ("bad-glob-leading-slash", lambda d: d["type"][0]["paths"].append("/abs"), "must not start with '/'"),
    ("bad-glob-trailing-slash", lambda d: d["generated"].append("dir/"), "must not end with '/'"),
    ("sizes-out-of-order", lambda d: d["sizes"].update(S=500), "S must be smaller than M"),
]


@pytest.mark.parametrize("mutate,expected", [(m, e) for _, m, e in BAD_FIXTURES], ids=[i for i, _, _ in BAD_FIXTURES])
def test_bad_fixture_is_caught(mutate, expected):
    errors, _ = check(mutate)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_an_agent_stage_may_name_itself_in_on_fail_to_run_its_agent_again():  # C-08
    stages = {s["id"]: s for s in default_pipeline()["stage"]}
    assert (stages["plan"]["on_fail"], stages["plan"]["max_rounds"]) == ("plan", 2)
    assert (stages["tests"]["on_fail"], stages["tests"]["max_rounds"]) == ("tests", 3)
    assert check(lambda d: stage(d, "verify").update(on_fail="verify"))[0] == []
    def without_a_cap(d):
        del stage(d, "verify")["max_rounds"]
        stage(d, "verify")["on_fail"] = "verify"

    assert any("on_fail needs max_rounds" in e for e in check(without_a_cap)[0])  # a retry is a round, so it needs its cap


def test_a_later_stage_is_still_no_on_fail_target_and_the_message_names_the_retry():
    errors, _ = check(lambda d: stage(d, "plan").update(on_fail="build", max_rounds=1))
    assert any("on_fail 'build' must be an earlier stage (or this stage itself, to run its agent again, or 'main'), not a later one" in e for e in errors)


def test_on_fail_target_outside_its_row_is_a_note_not_an_error():
    errors, notes = check(lambda d: d["matrix"]["docs"].pop("on_fail"))  # the row does not say where a failure goes
    assert errors == []
    assert len(notes) == 2 and all(n.startswith("row 'docs': ") for n in notes)
    assert any(
        "row 'docs': stage 'review' has on_fail 'build', which this row does not include; it fails over to main" in n
        for n in notes
    )
    assert 'on_fail = { review = "main" } in the row says so' in next(n for n in notes if "stage 'review'" in n)


# --- a row's on_fail: where a failed gate goes in that row, in place of the stage's own


def test_a_rows_on_fail_takes_the_place_of_the_stages_own_and_is_not_noted():
    errors, notes = check(lambda d: d["matrix"]["docs"].update(on_fail={"verify": "main", "review": "main"}))
    assert errors == [] and notes == []
    errors, notes = check(lambda d: d["matrix"]["docs"].update(on_fail={"verify": "main"}))  # one stage covered, the other not
    assert errors == [] and len(notes) == 1 and "row 'docs': stage 'review' has on_fail 'build'" in notes[0]


def test_a_row_may_send_a_failure_to_main_to_an_earlier_agent_stage_of_the_row_or_to_the_stages_own_agent():
    def mutate(d):
        d["matrix"]["code"]["S"]["on_fail"] = {"plan": "main", "tests": "main", "verify": "verify", "review": "tests"}

    assert check(mutate) == ([], [])


# (id, mutation, text that must appear in some error)
BAD_ON_FAIL_ROWS = [
    ("key-not-a-stage-of-the-row", lambda d: d["matrix"]["docs"]["on_fail"].update(build="main"), "row 'docs': on_fail names stage 'build', which the row does not include"),
    ("key-not-a-stage-at-all", lambda d: d["matrix"]["docs"]["on_fail"].update(ghost="main"), "row 'docs': on_fail names stage 'ghost', which the row does not include"),
    ("value-not-a-stage-of-the-row", lambda d: d["matrix"]["docs"]["on_fail"].update(verify="build"),
     "row 'docs': on_fail for stage 'verify' names 'build', which the row does not include (name a stage of the row, or 'main')"),
    ("value-not-a-stage-at-all", lambda d: d["matrix"]["docs"]["on_fail"].update(verify="ghost"), "on_fail for stage 'verify' names 'ghost', which the row does not include"),
    ("value-a-later-stage", lambda d: d["matrix"]["docs"]["on_fail"].update(verify="review"), "on_fail for stage 'verify' names 'review', which comes later in the row"),
    ("value-a-review-stage", lambda d: d["matrix"]["code"]["M"].update(on_fail={"review": "test-review"}),
     "row 'code.M': on_fail for stage 'review' names 'test-review', which is a review stage; it must name an agent stage or 'main'"),
    ("a-review-stage-names-itself", lambda d: d["matrix"]["docs"]["on_fail"].update(review="review"), "on_fail for the review stage 'review' cannot name itself"),
    ("stage-without-max-rounds", lambda d: d["matrix"]["code"]["S"].update(on_fail={"build": "plan"}),
     "row 'code.S': on_fail names stage 'build', which has no max_rounds (a stage that goes back needs its cap)"),
    ("not-a-table", lambda d: d["matrix"]["docs"].update(on_fail="main"), "row 'docs': on_fail must map stage ids to a stage of the row, or 'main'"),
    ("a-list", lambda d: d["matrix"]["docs"].update(on_fail=["verify"]), "row 'docs': on_fail must map stage ids to a stage of the row, or 'main'"),
    ("a-value-that-is-not-a-string", lambda d: d["matrix"]["docs"].update(on_fail={"verify": 1}), "row 'docs': on_fail must map stage ids to a stage of the row, or 'main'"),
    ("set-on-the-split-row-itself", lambda d: d["matrix"]["code"].update(on_fail={"verify": "main"}), "row 'code': unexpected key 'on_fail' (a row split by size has only S, M and L)"),
]


@pytest.mark.parametrize("mutate,expected", [(m, e) for _, m, e in BAD_ON_FAIL_ROWS], ids=[i for i, _, _ in BAD_ON_FAIL_ROWS])
def test_a_bad_on_fail_of_a_row_is_an_error(mutate, expected):
    errors, _ = check(mutate)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_a_bad_on_fail_adds_no_note_of_its_own_and_the_row_still_reports_its_other_errors():
    errors, notes = check(lambda d: d["matrix"]["docs"].update(on_fail={"verify": "build", "review": "main"}, lenses=["vibes"]))
    assert any("row 'docs': unknown lens 'vibes'" in e for e in errors) and any("names 'build'" in e for e in errors)
    assert notes == []  # the error says it; a stage the row names is never also noted as failing over implicitly


def test_the_key_on_fail_of_a_row_is_known_to_the_row_key_check():
    errors, _ = check(lambda d: d["matrix"]["docs"].update(colour="red"))
    assert any("unknown key 'colour' (a row has stages, lenses, note and on_fail)" in e for e in errors)


def test_a_rows_on_fail_reaches_the_runtime_through_the_effective_row_and_stage_and_leaves_the_pipeline_alone():
    pipeline = default_pipeline()
    by_id = {s["id"]: s for s in pipeline["stage"]}
    docs, code = pl.effective_row(pipeline, "docs"), pl.effective_row(pipeline, "code.S")
    assert docs.on_fail == {"verify": "main", "review": "main"} and code.on_fail == {}
    assert pl.effective_stage(by_id["review"], docs)["on_fail"] == "main"
    assert pl.effective_stage(by_id["review"], code)["on_fail"] == "build"
    assert by_id["review"]["on_fail"] == "build"  # the stage's own is untouched: another row may still use it


def test_the_plan_prints_the_rows_on_fail_and_the_intent_that_skips_its_target_fails_over_to_main():
    from samples import sample

    pipeline = default_pipeline()
    pipeline["matrix"]["docs"] = {"stages": ["intake", "plan", "build", "verify", "review", "reduce"], "on_fail": {"review": "plan"}}
    record = {**sample("change_class"), "row": "docs", "intent": "feature"}
    plan = {s["id"]: s for s in pl.build_plan(pipeline, record, "demo")["stages"]}
    assert plan["review"]["on_fail"] == "plan"  # the row's, in place of the stage's own build
    assert plan["verify"]["on_fail"] == "build" and plan["plan"]["on_fail"] == "plan"  # no on_fail of the row for these: their own
    record["intent"] = "spec-supplied"  # skips plan, which the row's on_fail names
    skipped = {s["id"]: s for s in pl.build_plan(pipeline, record, "demo")["stages"]}
    assert skipped["review"]["on_fail"] == "main" and skipped["verify"]["on_fail"] == "build"


def test_a_new_row_that_leaves_out_the_on_fail_target_only_adds_a_note():
    def mutate(d):
        d["matrix"]["code"]["S"]["stages"] = ["intake", "plan", "verify", "review", "reduce"]  # no tests, no build

    errors, notes = check(mutate)
    assert errors == []
    assert any("row 'code.S'" in n and "fails over to main" in n for n in notes)


def test_optional_reads_may_be_absent_from_the_row():
    # the docs row has no plan, tests or build, yet review reads plan? and verify reads tests? and build?
    d = default_pipeline()
    assert "plan?" in stage(d, "review")["reads"]
    assert "plan" not in d["matrix"]["docs"]["stages"]
    errors, _ = pl.validate_pipeline(d)
    assert errors == []


def test_the_same_read_without_the_question_mark_is_an_error():
    errors, _ = check(lambda d: stage(d, "review")["reads"].__setitem__(1, "plan"))
    assert any("row 'docs': stage 'review' reads 'plan'" in e for e in errors)


def test_reduce_reads_verify_and_review_only_when_the_row_has_them():
    assert stage(default_pipeline(), "reduce")["reads"] == ["intake", "verify?", "review?"]


def test_a_row_of_just_intake_and_reduce_validates_with_no_errors_and_no_read_notes():
    # reduce reads verify? and review?, so it reduces whatever the row produced
    def mutate(d):
        d["matrix"]["docs"] = {"stages": ["intake", "reduce"], "note": "Handled by the repo's own evals."}

    errors, notes = check(mutate)
    assert errors == []
    assert [n for n in notes if "row 'docs'" in n] == []
    assert not any("reads" in n for n in notes)


def test_a_row_that_places_reduce_before_the_stages_it_reads_is_an_error():
    # reduce reads verify? and review? optionally, so only the ordering rule catches this
    errors, _ = check(lambda d: d["matrix"]["docs"].update(stages=["intake", "reduce", "verify", "review"]))
    assert any("definition order" in e for e in errors)


def make_reduce_reads_required(d):
    stage(d, "reduce")["reads"] = ["intake", "verify", "review"]
    d["matrix"]["docs"].update(stages=["intake", "reduce"])


def make_verify_read_tests(d):
    stage(d, "verify")["reads"] = ["diff", "tests", "build?"]


# (the stage that reads, a mutation that makes the row leave out what it reads, text of the error)
LEFT_OUT_READS = [
    ("plan", lambda d: d["matrix"]["code"]["S"].update(stages=["plan", "tests", "build", "verify", "review", "reduce"]), "row 'code.S': stage 'plan' reads 'intake'"),
    ("tests", lambda d: d["matrix"]["code"]["S"].update(stages=["intake", "tests", "build", "verify", "review", "reduce"]), "row 'code.S': stage 'tests' reads 'plan'"),
    ("build", lambda d: d["matrix"]["code"]["S"].update(stages=["intake", "build", "verify", "review", "reduce"]), "row 'code.S': stage 'build' reads 'plan'"),
    ("verify", make_verify_read_tests, "row 'docs': stage 'verify' reads 'tests'"),
    ("review", lambda d: d["matrix"]["docs"].update(stages=["intake", "review", "reduce"]), "row 'docs': stage 'review' reads 'verify'"),
    ("reduce", lambda d: d["matrix"]["docs"].update(stages=["verify", "review", "reduce"]), "row 'docs': stage 'reduce' reads 'intake'"),
    ("reduce", make_reduce_reads_required, "row 'docs': stage 'reduce' reads 'verify'"),
    ("reduce", make_reduce_reads_required, "row 'docs': stage 'reduce' reads 'review'"),
]


@pytest.mark.parametrize("mutate,expected", [(m, e) for _, m, e in LEFT_OUT_READS], ids=[f"{s}-{i}" for i, (s, _, _) in enumerate(LEFT_OUT_READS)])
def test_a_required_read_of_a_left_out_stage_is_an_error_for_every_role(mutate, expected):
    errors, notes = check(mutate)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"
    assert not any("run by the main session" in n for n in notes)  # no role draws a note instead


def test_the_left_out_read_cases_cover_every_role_and_the_review_kind():
    default = default_pipeline()
    readers = {stage(default, sid).get("role") or stage(default, sid)["kind"] for sid, _, _ in LEFT_OUT_READS}
    assert readers == set(pl.KNOWN_ROLES) | {"review"}


def test_the_first_stage_may_read_the_inputs():
    d = default_pipeline()
    assert set(stage(d, "intake")["reads"]) <= set(d["inputs"])
    assert pl.validate_pipeline(d)[0] == []


def test_a_row_that_omits_every_size_key_or_stages_is_rejected():
    errors, _ = check(lambda d: d["matrix"].update(docs={"note": "nothing here"}))
    assert any("needs 'stages' (a flat row) or the size keys S, M and L" in e for e in errors)


def test_the_last_type_may_use_any_pattern_that_matches_everything():
    errors, _ = check(lambda d: d["type"][-1].update(paths=["**/*"]))
    assert errors == []


def test_every_error_is_listed_not_just_the_first():
    def mutate(d):
        stage(d, "build").update(role="wizard")
        stage(d, "verify").update(gate="vibes")
        d["precedence"].remove("docs")

    errors, _ = check(mutate)
    assert len(errors) >= 3


def test_cli_reports_a_bad_pipeline_file_and_exits_1(run_cli, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(DEFAULT_TOML.read_text().replace('role = "builder"', 'role = "wizard"'))
    result = run_cli("validate-pipeline", bad, cwd=tmp_path)
    assert result.returncode == 1
    assert "error: stage 'build': unknown role 'wizard'" in result.stdout
    assert "invalid (1 error" in result.stdout


def test_cli_reports_invalid_toml_and_exits_1(run_cli, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("schema = \n")
    result = run_cli("validate-pipeline", bad, cwd=tmp_path)
    assert result.returncode == 1
    assert "invalid TOML" in result.stdout


def test_cli_a_missing_pipeline_file_is_reported_as_an_error_with_exit_1(run_cli, tmp_path):
    result = run_cli("validate-pipeline", tmp_path / "nope.toml", cwd=tmp_path)
    assert result.returncode == 1
    assert "nope.toml: file not found" in result.stdout
    assert "invalid (1 error" in result.stdout
