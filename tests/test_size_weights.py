"""The size of a change counts a line of a test file at reduced weight: a well-tested part of 120 code lines and 284 test lines measured 404 (size L, with
M at most 400) in a real run, and splitting a feature because its tests are thorough is backwards. `[sizes]` has an optional `weights` table keyed by file
type id, a type it does not list weighs 1, and the size is the sum over the files of changed lines times the weight of the file's type, rounded up."""
import copy
import json
import math

import pytest

import plumbline as pl
from helpers import default_pipeline, numbered, write
from rundata import RUN, adopt_base
from samples import sample

PIPELINE = default_pipeline()


@pytest.fixture
def part(repo):
    """An adopted repository holding a part of 120 lines of code and 284 lines of tests, as the real size-M part held them: nothing committed."""
    adopt_base(repo)
    write(repo / "src" / "tags.py", numbered(120))
    write(repo / "tests" / "test_tags.py", numbered(284))
    return repo


def classify_with(repo, weights="default"):
    pipeline = copy.deepcopy(pl.load_project(repo).pipeline)
    if weights is None:
        pipeline["sizes"].pop("weights", None)
    elif weights != "default":
        pipeline["sizes"]["weights"] = weights
    return pl.classify(repo, pipeline, "main")


# --- the pipeline: `weights` in [sizes], and what validate-pipeline says about it


def test_the_default_pipeline_weighs_tests_at_half_and_validates_clean():
    assert PIPELINE["sizes"] == {"S": 50, "M": 400, "weights": {"tests": 0.5}}
    assert pl.validate_pipeline(PIPELINE) == ([], [])
    assert pl.size_weights(PIPELINE) == {"tests": 0.5}


def test_a_pipeline_without_weights_is_valid_and_every_type_weighs_one():
    plain = copy.deepcopy(PIPELINE)
    del plain["sizes"]["weights"]
    assert pl.validate_pipeline(plain) == ([], []) and pl.size_weights(plain) == {}
    assert pl.validate_pipeline({**plain, "sizes": {**plain["sizes"], "weights": {}}}) == ([], [])


def errors_with(weights):
    pipeline = copy.deepcopy(PIPELINE)
    pipeline["sizes"]["weights"] = weights
    return pl.validate_pipeline(pipeline)[0]


@pytest.mark.parametrize(
    "weights,said",
    [
        ({"test": 0.5}, "sizes.weights: 'test' is not a type of this pipeline (a key is the id of a [[type]]: docs, tests, config, code)"),
        ({"tests": 1.5}, "sizes.weights.tests: 1.5 is not a number from 0 to 1"),
        ({"tests": 2}, "sizes.weights.tests: 2 is not a number from 0 to 1"),
        ({"tests": -0.1}, "sizes.weights.tests: -0.1 is not a number from 0 to 1"),
        ({"tests": "half"}, "sizes.weights.tests: 'half' is not a number from 0 to 1"),
        ({"tests": True}, "sizes.weights.tests: True is not a number from 0 to 1"),
        ({"tests": float("nan")}, "sizes.weights.tests: nan is not a number from 0 to 1"),
        ({"tests": None}, "sizes.weights.tests: None is not a number from 0 to 1"),
        ({"tests": [0.5]}, "sizes.weights.tests: [0.5] is not a number from 0 to 1"),
    ],
)
def test_validate_pipeline_refuses_an_unknown_type_and_a_weight_that_is_no_number_from_0_to_1(weights, said):
    assert said in errors_with(weights)


def test_both_errors_are_reported_together_and_the_bounds_are_allowed():
    assert errors_with({"nope": 3}) == [
        "sizes.weights: 'nope' is not a type of this pipeline (a key is the id of a [[type]]: docs, tests, config, code)",
        "sizes.weights.nope: 3 is not a number from 0 to 1",
    ]
    assert errors_with({"tests": 0, "docs": 1, "code": 0.25, "config": 1.0}) == []  # 0 and 1 are in: a weight of 0 leaves a type out of the size


def test_weights_must_be_a_table(tmp_path):
    pipeline = copy.deepcopy(PIPELINE)
    pipeline["sizes"]["weights"] = 0.5
    errors, _notes = pl.validate_pipeline(pipeline)
    assert errors == ["$.sizes.weights: expected object, got number"]


def test_a_type_a_repository_adds_may_be_weighed(repo):
    adopt_base(repo)
    (repo / "plumbline.toml").write_text(
        (repo / "plumbline.toml").read_text(encoding="utf-8").replace("[graft]", 'precedence = ["voice", "code", "config", "tests", "docs"]\n\n[graft]', 1)
        + '\n[[type]]\nid = "voice"\npaths = ["voice/**"]\n\n[matrix.voice]\nstages = ["intake", "reduce"]\n',
        encoding="utf-8",
    )
    assert pl.load_project(repo).errors == []
    assert "voice" in {t["id"] for t in pl.load_project(repo).pipeline["type"]}
    pipeline = copy.deepcopy(pl.load_project(repo).pipeline)
    pipeline["sizes"]["weights"] = {"voice": 0.1}
    assert pl.validate_pipeline(pipeline) == ([], [])


def test_the_cli_reports_a_bad_weight_and_the_shipped_default_stays_valid(run_cli, tmp_path):
    bad = (pl.PIPELINE_DIR / "default.toml").read_text(encoding="utf-8").replace("weights = { tests = 0.5 }", "weights = { tests = 1.5, nope = 0.5 }")
    write(tmp_path / "bad.toml", bad)
    result = run_cli("validate-pipeline", str(tmp_path / "bad.toml"), cwd=tmp_path)
    assert result.returncode == 1 and "error: sizes.weights.tests: 1.5 is not a number from 0 to 1" in result.stdout and "error: sizes.weights: 'nope' is not a type" in result.stdout
    ok = run_cli("validate-pipeline", cwd=tmp_path)
    assert ok.returncode == 0 and "valid (0 errors, 0 notes)" in ok.stdout


# --- the measure: changed lines times the weight of the file's type, summed, rounded up


def test_a_part_of_120_code_lines_and_284_test_lines_measures_m_and_not_l(part):
    record = classify_with(part)
    assert (record["lines"], record["size"], record["row"]) == (262, "M", "code.M")  # 120 + 284 * 0.5
    assert record["weights"] == {"tests": 0.5}
    assert pl.check_record("change_class", record) == []


def test_a_weight_of_1_restores_the_measure_of_before(part):
    for weights in ({"tests": 1}, {"tests": 1.0}, None, {}):
        record = classify_with(part, weights)
        assert (record["lines"], record["size"], record["row"]) == (404, "L", "code.L"), weights  # the real run's 404 lines, over M's 400
        assert "weights" not in record, weights  # nothing weighs less than a line: the record is as it was


def test_the_size_rounds_up_and_a_weight_is_exact(repo):
    pipeline = copy.deepcopy(PIPELINE)
    files = lambda *specs: [{"path": f"{t}/{i}", "type": t, "added": n, "removed": r, "generated": g, "symlink": False} for i, (t, n, r, g) in enumerate(specs)]
    assert pl.measure_size(pipeline, files(("tests", 1, 0, False))) == (1, {"tests": 0.5})  # half a line counts as one
    assert pl.measure_size(pipeline, files(("tests", 3, 0, False))) == (2, {"tests": 0.5})  # 1.5 rounds up
    assert pl.measure_size(pipeline, files(("code", 7, 0, False), ("tests", 1, 0, False))) == (8, {"tests": 0.5})  # 7.5 as a whole, not 7 and 1
    assert pl.measure_size(pipeline, files(("tests", 5, 3, False))) == (4, {"tests": 0.5})  # added and removed lines both count
    seven_hundredths = {"sizes": {"S": 50, "M": 400, "weights": {"tests": 0.07}}}
    assert pl.measure_size(seven_hundredths, files(("tests", 100, 0, False)))[0] == 7  # exactly 7: 0.07 is read as 7/100
    assert math.ceil(100 * 0.07) == 8  # where a sum in floating point is 7.000000000000001, and rounds up to 8
    assert pl.measure_size({"sizes": {"S": 50, "M": 400, "weights": {"tests": 0}}}, files(("tests", 900, 0, False))) == (0, {"tests": 0})  # a weight of 0 leaves a type out
    assert pl.measure_size(pipeline, files()) == (0, {})


def test_generated_files_stay_out_and_a_type_without_a_weight_counts_in_full(repo):
    pipeline = copy.deepcopy(PIPELINE)
    files = [
        {"path": "uv.lock", "type": "code", "added": 300, "removed": 0, "generated": True, "symlink": False},
        {"path": "tests/a.py", "type": "tests", "added": 40, "removed": 0, "generated": True, "symlink": False},
        {"path": "docs/a.md", "type": "docs", "added": 10, "removed": 0, "generated": False, "symlink": False},
        {"path": "pyproject.toml", "type": "config", "added": 6, "removed": 2, "generated": False, "symlink": False},
    ]
    assert pl.measure_size(pipeline, files) == (18, {})  # docs and config weigh 1, and a generated file counts for nothing whatever its type


def test_every_measure_of_a_change_reads_the_same_function(part, run_cli):
    project = pl.load_project(part)
    assert run_cli("plan", "--intent", "feature", "--row", "code.M", "--run-id", RUN, cwd=part).returncode == 0
    run = pl.load_run(project, RUN)
    measured, problems = pl.measure_row(project, run)  # what pass and check-diff measure from the run's merge base
    changed, _ = pl.changed_files(project, run)  # what the verify gate's refactor check reads
    classified = pl.classify(part, project.pipeline, "main")  # what `classify` and `plan --intent` write
    assert problems == [] and (measured["lines"], changed["lines"], classified["lines"]) == (262, 262, 262)
    assert measured["weights"] == changed["weights"] == classified["weights"] == {"tests": 0.5}
    assert json.loads(run_cli("plan", "--run", RUN, cwd=part).stdout)["lines"] == 262  # and the plan prints the intake record's count, the same


def test_check_diff_of_a_run_measures_the_part_as_m_and_finds_no_problem(part, run_cli):
    assert run_cli("plan", "--intent", "feature", "--row", "code.M", "--run-id", RUN, cwd=part).returncode == 0
    result = run_cli("check-diff", "--run", RUN, cwd=part)
    data = json.loads(result.stdout)
    assert result.returncode == 0 and data["row"] == {"declared": "code.M", "measured": "code.M", "missing_stages": []} and data["problems"] == []


# --- the output: the weighted total and the raw split, and the five files that contribute most


def record_of(*specs, weights=None, lines=None):
    files = [{"path": path, "type": kind, "added": added, "removed": removed, "generated": False, "symlink": False} for path, kind, added, removed in specs]
    types = [t for t in ("code", "config", "tests", "docs") if any(f["type"] == t for f in files)]
    record = {"files": files, "types": types, "lines": lines if lines is not None else 0}
    return {**record, **({"weights": weights} if weights else {})}


def test_a_size_is_printed_as_the_weighted_total_and_the_raw_split():
    record = record_of(("src/tags.py", "code", 120, 0), ("tests/test_tags.py", "tests", 284, 0), weights={"tests": 0.5}, lines=262)
    assert pl.size_text(record) == "262 weighted lines (code 120, tests 284 at 0.5)"
    assert pl.size_text(record_of(("src/a.py", "code", 7, 3), lines=10)) == "10 weighted lines (code 10)"  # nothing weighs less: the split is the total
    assert pl.size_text(record_of(("src/a.py", "code", 1, 0), ("docs/a.md", "docs", 2, 0), ("a.toml", "config", 3, 0), lines=6)) == "6 weighted lines (code 1, config 3, docs 2)"  # in the record's order of types
    assert pl.size_text(record_of(lines=0)) == "0 weighted lines"


def test_a_record_from_before_the_weights_prints_as_a_plain_count_of_the_lines_it_holds():
    record = record_of(("src/a.py", "code", 30, 0), ("tests/t.py", "tests", 20, 0), lines=50)  # a record without `weights`: every type counted in full
    assert pl.size_text(record) == "50 weighted lines (code 30, tests 20)"


def test_the_refusal_of_a_size_l_change_gives_both_the_total_and_the_split_and_names_what_contributes_most(part, run_cli):
    write(part / "src" / "big.py", numbered(400))  # 520 lines of code, and 284 of tests at half
    assert run_cli("plan", "--intent", "review-only", "--row", "code.M", "--run-id", RUN, cwd=part).returncode == 0
    data = json.loads(run_cli("check-diff", "--run", RUN, cwd=part).stdout)
    [problem] = [p for p in data["problems"] if p.startswith("this row ends before reduce")]
    assert problem == (
        "this row ends before reduce: split the change (it measures as code.L, 662 weighted lines (code 520, tests 284 at 0.5), and nothing is built at that size)."
        " The files that contribute most: src/big.py (400 lines), tests/test_tags.py (284 lines at 0.5), src/tags.py (120 lines)."
    )


def test_the_files_that_contribute_most_are_ordered_by_what_they_add_to_the_size_and_name_their_weight():
    record = record_of(("tests/test_a.py", "tests", 300, 0), ("src/b.py", "code", 100, 0), ("src/c.py", "code", 149, 0), ("tests/test_d.py", "tests", 100, 0), weights={"tests": 0.5})
    assert pl.biggest_files(record) == (
        " The files that contribute most: tests/test_a.py (300 lines at 0.5), src/c.py (149 lines), src/b.py (100 lines), tests/test_d.py (100 lines at 0.5)."
    )  # 300 test lines weigh 150, which is more than 149 lines of code; 100 of them weigh 50, less than 100 of code
    other = pl.biggest_files(record_of(("tests/test_a.py", "tests", 300, 0), ("src/c.py", "code", 151, 0), weights={"tests": 0.5}))
    assert other.index("src/c.py") < other.index("tests/test_a.py")  # and 151 lines of code are more than the 150
    tie = pl.biggest_files(record_of(("tests/test_a.py", "tests", 300, 0), ("src/c.py", "code", 150, 0), weights={"tests": 0.5}))
    assert tie.index("src/c.py") < tie.index("tests/test_a.py")  # equal shares: by path


def test_a_file_of_weight_0_contributes_nothing_and_is_not_named():
    record = record_of(("tests/test_a.py", "tests", 300, 0), ("src/b.py", "code", 4, 0), weights={"tests": 0})
    assert pl.biggest_files(record) == " The files that contribute most: src/b.py (4 lines)."
    assert pl.biggest_files(record_of(("tests/test_a.py", "tests", 300, 0), weights={"tests": 0})) == ""


def test_the_five_files_limit_holds_with_weights():
    specs = [(f"tests/test_{n}.py", "tests", 10 + n, 0) for n in range(7)]
    text = pl.biggest_files(record_of(*specs, weights={"tests": 0.5}))
    assert text.count("lines at 0.5") == 5 and text.endswith("and 2 files more.")


def test_the_rendered_record_gives_the_weighted_size_and_falls_back_for_a_record_that_lacks_the_files():
    record = {**sample("change_class"), "lines": 262, "weights": {"tests": 0.5}}
    record["files"] = [
        {"path": "src/tags.py", "type": "code", "added": 120, "removed": 0, "generated": False, "symlink": False},
        {"path": "tests/test_tags.py", "type": "tests", "added": 284, "removed": 0, "generated": False, "symlink": False},
    ]
    record["types"] = ["code", "tests"]
    assert "Row `code.M`: size M, 262 weighted lines (code 120, tests 284 at 0.5). Types: code, tests." in pl.render_record("change_class", record)
    assert "size M, 7 changed lines." in pl.render_record("change_class", {"row": "code.M", "size": "M", "lines": 7})


def test_the_schema_holds_weights_as_an_optional_object_and_says_what_lines_is():
    schema = pl.load_schema("change_class")
    assert "weights" in schema["properties"] and "weights" not in schema["required"] and schema["properties"]["weights"]["type"] == "object"
    assert "times the weight of its file type under the pipeline's [sizes]" in schema["properties"]["lines"]["description"] and "rounded up" in schema["properties"]["lines"]["description"]
    record = sample("change_class")
    assert pl.check_record("change_class", record) == [] and pl.check_record("change_class", {**record, "weights": {"tests": 0.5}}) == []
    assert any(e.startswith("$.weights:") for e in pl.check_record("change_class", {**record, "weights": [0.5]}))


# --- the docs say why, in the README, the pipeline file and the run skill


def test_the_readme_the_pipeline_file_and_the_run_skill_say_what_the_weights_are_and_why():
    from helpers import DEFAULT_TOML, REPO
    from test_readme import README, rows, section

    sizes = next(line for line in section("The pipeline file").splitlines() if line.startswith("- `[sizes]`"))
    assert "the changed-line limits for size S and size M. More than M is L." in sizes
    assert "`weights` (optional) maps a file type id to what a changed line of that type counts for, a number from 0 to 1, and a type it does not list counts 1" in sizes
    assert "The size is the sum over the files of changed lines times the weight of the file's type, rounded up" in sizes
    assert "so that thorough tests do not push a change into a bigger row" in sizes and "The default pipeline weighs `tests` at 0.5, so that part measures 262 and is size M" in sizes
    assert "`validate-pipeline` checks that each key is a type id and each value a number from 0 to 1" in sizes
    assert "262 weighted lines (code 120, tests 284 at 0.5)" in sizes
    toml = DEFAULT_TOML.read_text(encoding="utf-8")
    assert "weights = { tests = 0.5 }" in toml and "Thorough tests should not push a change into a bigger row" in toml and "a line of a test file counts half" in toml
    records = {r[0].strip("`"): r for r in rows(section("Records"))}
    assert "the size (`lines`, weighted by file type, and the `weights` that applied)" in records["change_class"][2]
    text = section("Runs, gates and the pass record")
    assert "up to 5, each with its changed lines, untracked files included" in text and "ordered by what it adds to the size, with the weight of its type where that is not 1" in text
    skill = (REPO / "skills" / "run" / "SKILL.md").read_text(encoding="utf-8")
    assert "(S up to 50 weighted changed lines, M up to 400, L more; a line of a test file counts for half)" in skill
    assert "weights" in README
