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


def test_default_pipeline_validates_with_only_fail_over_notes():
    errors, notes = pl.validate_pipeline(default_pipeline())
    assert errors == []
    # docs, config and tests rows leave out build, so verify and review fail over to main
    assert len(notes) == 6
    assert all("fails over to main" in n for n in notes)


def test_default_pipeline_via_the_cli(run_cli, tmp_path):
    result = run_cli("validate-pipeline", cwd=tmp_path)
    assert result.returncode == 0
    assert "pipeline 'default': valid (0 errors, 6 notes)" in result.stdout


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
    ("on-fail-itself", lambda d: stage(d, "verify").update(on_fail="verify"), "on_fail 'verify' must be an earlier stage"),
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


def test_on_fail_target_outside_its_row_is_a_note_not_an_error():
    errors, notes = pl.validate_pipeline(default_pipeline())
    assert errors == []
    assert any(
        "row 'docs': stage 'review' has on_fail 'build', which this row does not include; it fails over to main" in n
        for n in notes
    )


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


def test_a_row_of_just_intake_and_reduce_validates_with_notes():
    # the main session reduces whatever the row produced, so a row that leaves out
    # verify and review (the reads of reduce) draws notes, not errors
    def mutate(d):
        d["matrix"]["docs"] = {"stages": ["intake", "reduce"], "note": "Handled by the repo's own evals."}

    errors, notes = check(mutate)
    assert errors == []
    reduce_notes = [n for n in notes if "row 'docs'" in n]
    assert len(reduce_notes) == 2
    assert any("stage 'reduce' is run by the main session and reads 'verify', which this row does not include" in n for n in reduce_notes)
    assert any("reads 'review'" in n for n in reduce_notes)


def test_a_main_session_stage_placed_before_what_it_reads_is_still_an_error():
    # the leniency is for stages the row leaves out, not for a row that reorders them
    errors, _ = check(lambda d: d["matrix"]["docs"].update(stages=["intake", "reduce", "verify", "review"]))
    assert any("definition order" in e for e in errors)
    assert any("stage 'reduce' reads 'verify'" in e for e in errors)


def test_only_stages_run_by_the_main_session_get_the_leniency():
    # verify and review are not run by the main session: a row without what they read is an error
    errors, _ = check(lambda d: d["matrix"]["docs"].update(stages=["intake", "review", "reduce"]))
    assert any("stage 'review' reads 'verify'" in e for e in errors)
    errors, _ = check(lambda d: d["matrix"]["code"]["S"].update(stages=["intake", "tests", "build", "verify", "review", "reduce"]))
    assert any("row 'code.S': stage 'tests' reads 'plan'" in e for e in errors)


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
