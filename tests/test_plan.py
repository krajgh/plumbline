"""plan: the resolved row and its ordered stages, as JSON."""
import json
import re

import pytest

import plumbline as pl
from helpers import numbered, write

CODE_M_STAGES = ["intake", "plan", "tests", "test-review", "build", "verify", "review", "reduce"]


def plan(run_cli, repo, *extra, run_id="demo"):
    args = ["plan", "--base", "main", *extra]
    if run_id is not None:
        args += ["--run-id", run_id]
    result = run_cli(*args, cwd=repo)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def stage(plan_, sid):
    return next(s for s in plan_["stages"] if s["id"] == sid)


@pytest.fixture
def code_m(repo):
    write(repo / "src" / "new_module.py", numbered(100))
    return repo


@pytest.fixture
def docs_only(repo):
    write(repo / "docs" / "guide.md", numbered(5))
    return repo


def test_code_m_lists_the_eight_stages_in_order_with_record_paths(run_cli, code_m):
    p = plan(run_cli, code_m)
    assert (p["row"], p["size"], p["lines"]) == ("code.M", "M", 100)
    assert [s["id"] for s in p["stages"]] == CODE_M_STAGES
    for s in p["stages"]:
        assert s["path"] == f".plumbline/runs/demo/{s['id']}.json"
    assert p["record_dir"] == ".plumbline/runs/demo"
    assert [(s["id"], s["record"]) for s in p["stages"]] == [
        ("intake", "change_class"),
        ("plan", "spec"),
        ("tests", "tests_record"),
        ("test-review", "review_record"),
        ("build", "build_note"),
        ("verify", "verify_record"),
        ("review", "review_record"),
        ("reduce", "pass_record"),
    ]


def test_every_stage_carries_role_or_kind_gate_on_fail_max_rounds_and_lenses(run_cli, code_m):
    p = plan(run_cli, code_m)
    plan_stage = stage(p, "plan")
    assert (plan_stage["kind"], plan_stage["role"], plan_stage["gate"]) == ("agent", "planner", "spec_complete")
    assert (plan_stage["on_fail"], plan_stage["max_rounds"], plan_stage["lenses"]) == ("plan", 2, None)  # the planner runs again, twice at most
    tests_stage = stage(p, "tests")
    assert (tests_stage["gate"], tests_stage["on_fail"], tests_stage["max_rounds"]) == ("tests_fail_on_stub", "tests", 3)
    test_review = stage(p, "test-review")
    assert (test_review["kind"], test_review["role"]) == ("review", None)
    assert (test_review["on_fail"], test_review["max_rounds"], test_review["lenses"]) == ("tests", 2, ["tests"])
    review = stage(p, "review")
    assert review["lenses"] == ["correctness", "tests", "security", "data", "boundaries"]  # code.M sets no lenses
    assert (review["target"], review["defenders"], review["survive_if_unrefuted_by"], review["detective"]) == ("diff", 3, 2, True)
    assert (stage(p, "verify")["on_fail"], stage(p, "verify")["max_rounds"]) == ("build", 3)


def test_reads_are_resolved_to_record_paths(run_cli, code_m):
    p = plan(run_cli, code_m)
    assert stage(p, "intake")["reads"] == [
        {"name": "request", "kind": "input", "path": None, "optional": False},
        {"name": "diff", "kind": "input", "path": None, "optional": False},
    ]
    assert stage(p, "plan")["reads"][1] == {"name": "intake", "kind": "record", "path": ".plumbline/runs/demo/intake.json", "optional": False}
    assert stage(p, "verify")["reads"] == [
        {"name": "diff", "kind": "input", "path": None, "optional": False},
        {"name": "tests", "kind": "record", "path": ".plumbline/runs/demo/tests.json", "optional": True},
        {"name": "build", "kind": "record", "path": ".plumbline/runs/demo/build.json", "optional": True},
    ]
    # the builder reads the plan and never the tests
    assert [r["name"] for r in stage(p, "build")["reads"]] == ["plan"]


def test_the_docs_row_review_stage_carries_the_docs_lens(run_cli, docs_only):
    p = plan(run_cli, docs_only)
    assert p["row"] == "docs"
    assert [s["id"] for s in p["stages"]] == ["intake", "verify", "review", "reduce"]
    assert stage(p, "review")["lenses"] == ["docs"]


def test_the_reviews_on_fail_resolves_to_main_in_the_docs_row(run_cli, docs_only):
    p = plan(run_cli, docs_only)
    assert stage(p, "review")["on_fail"] == "main"  # build is not in the docs row
    assert stage(p, "verify")["on_fail"] == "main"
    assert stage(p, "review")["max_rounds"] == 3


def test_on_fail_stays_a_stage_id_when_the_row_includes_it(run_cli, repo):
    write(repo / "src" / "new_module.py", numbered(30))
    p = plan(run_cli, repo)
    assert p["row"] == "code.S"
    assert stage(p, "review")["on_fail"] == "build"
    assert stage(p, "review")["lenses"] == ["correctness", "tests"]  # the code.S row's lenses replace the stage's


def test_absent_optional_reads_have_no_path(run_cli, docs_only):
    p = plan(run_cli, docs_only)
    reads = {r["name"]: r for r in stage(p, "review")["reads"]}
    assert reads["plan"] == {"name": "plan", "kind": "record", "path": None, "optional": True}
    assert reads["verify"]["path"] == ".plumbline/runs/demo/verify.json"


def test_the_test_review_stage_keeps_its_own_lenses_in_every_row(run_cli, code_m):
    # a row's lenses replace only the review stage whose target is diff
    assert stage(plan(run_cli, code_m), "test-review")["lenses"] == ["tests"]


def test_code_l_runs_only_intake_and_plan_and_carries_the_note(run_cli, repo):
    write(repo / "src" / "big.py", numbered(500))
    p = plan(run_cli, repo)
    assert p["row"] == "code.L"
    assert [s["id"] for s in p["stages"]] == ["intake", "plan"]
    assert "split into changes of size M or smaller" in p["note"]


def test_run_id_sets_the_paths(run_cli, code_m):
    p = plan(run_cli, code_m, run_id="my-run.2")
    assert p["run_id"] == "my-run.2"
    assert all(s["path"].startswith(".plumbline/runs/my-run.2/") for s in p["stages"])
    assert stage(p, "plan")["reads"][1]["path"] == ".plumbline/runs/my-run.2/intake.json"


def test_the_default_run_id_is_the_short_head_sha_and_a_utc_timestamp(run_cli, code_m):
    p = plan(run_cli, code_m, run_id=None)
    assert re.fullmatch(r"[0-9a-f]{7,}-\d{8}T\d{6}Z", p["run_id"])
    assert p["run_id"].startswith(p["head"][:7])


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "", "with space", "x" * 200])
def test_an_unsafe_run_id_is_refused(run_cli, code_m, bad):
    result = run_cli("plan", "--base", "main", "--run-id", bad, cwd=code_m)
    assert result.returncode == 2
    assert "run id" in result.stderr


def test_plan_reports_the_base_and_the_head(run_cli, code_m):
    p = plan(run_cli, code_m)
    assert p["base"] == "main"
    assert re.fullmatch(r"[0-9a-f]{40}", p["head"])
    assert p["pipeline"] == "default"


def test_build_plan_is_pure_given_a_record(repo):
    project = pl.load_project(repo)
    write(repo / "src" / "new_module.py", numbered(100))
    record = pl.classify(repo, project.pipeline, "main")
    first = pl.build_plan(project.pipeline, record, "r")
    assert first == pl.build_plan(project.pipeline, record, "r")
    assert [s["id"] for s in first["stages"]] == CODE_M_STAGES


def test_plan_with_no_changes_exits_2(run_cli, repo):
    result = run_cli("plan", "--base", "main", "--run-id", "demo", cwd=repo)
    assert result.returncode == 2
    assert "nothing to classify" in result.stderr
