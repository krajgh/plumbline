"""Open findings last beyond the run: `pass` keeps the surviving non-blocking findings and the detective's gaps in the pass record, `open`
lists them, and `status` counts them. The run skill says how a follow-up starts from them."""
import json
import os

import pytest

import plumbline as pl
from helpers import REPO, commit_all, git
from rundata import (
    CONTROLLED, RUN, adopt, build_note_record, change_of, intake_record, put, read, review_record, run_entry, run_path, spec_record, store_request,
    verify_record, write_test_file, written_tests_record,
)
from samples import sample
from test_manifests import between, frontmatter
from test_readme import section


def finding(fid, severity="MINOR", lens="correctness", file="src/app.py", line=21):
    """A finding as the merged review record holds it."""
    return {
        "id": fid, "lens": lens, "file": file, "line": line, "claim": f"claim {fid}", "failure_scenario": f"the failure of {fid}", "rule": "AC-1",
        "evidence": "return retry(url)", "outside_code": None, "severity": severity, "evidence_unverified": False,
    }


def gap(gid, kind="missing_test", ac="AC-1"):
    return {"id": gid, "kind": kind, "detail": f"the detail of {gid}", "ac": ac}


def review_leaving(diff, findings, survivors, gaps, target="diff"):
    """A merged review record in which `survivors` stand, and the detective named `gaps`."""
    record = review_record(diff=diff, target=target)
    blockers = [f["id"] for f in findings if f["severity"] == "BLOCKING" and f["id"] in survivors]
    record.update(findings=findings, defenses=[], survivors=survivors, gaps=gaps, blockers_surviving=len(blockers), routes={"builder": list(survivors), "test-writer": []})
    return record


FINDINGS = [
    finding("correctness-1", "MAJOR"),
    finding("correctness-2", "MINOR", line=30),
    finding("tests-1", "MAJOR", lens="tests", file="tests/test_app.py", line=3),  # refuted: not a survivor
]
GAPS = [gap("G-1"), gap("G-2", "edge_case", ac=None)]


def docs_run(repo, review, run_id=RUN):
    """A finished run of the docs row awaiting its pass; `review` makes its review record from the hash of the change."""
    diff = change_of(repo)
    put(repo, "intake", intake_record("docs", repo), run_id)
    put(repo, "verify", verify_record(diff=diff), run_id)
    run_entry(repo, "verify", diff, run_id)
    put(repo, "review", review(diff), run_id)


def leaving(findings=FINDINGS, survivors=("correctness-1", "correctness-2"), gaps=GAPS):
    return lambda diff: review_leaving(diff, findings, list(survivors), gaps)


@pytest.fixture
def adopted(repo):
    adopt(repo, commands={"test": CONTROLLED})
    return repo


@pytest.fixture
def passed(adopted, run_cli):
    """A passed run of the docs row whose review left two findings standing and named two gaps."""
    docs_run(adopted, leaving())
    assert run_cli("pass", RUN, cwd=adopted).returncode == 0
    return adopted


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def pass_file(repo):
    return repo / ".plumbline" / "pass" / f"{head(repo)}.json"


def pass_without_the_lists(repo, run_id=RUN):
    """The pass a 0.4.2 `pass` wrote: the same record without `open_findings` and `gaps`, entered in the ledger the way `pass` enters it."""
    record, problems, run_copy = pl.make_pass_record(pl.load_project(repo), run_id)
    assert record is not None, problems
    del record["open_findings"], record["gaps"]
    pl.write_json_atomic(run_copy, record)
    pl.write_json_atomic(pass_file(repo), record)
    pl.note_pass(repo, run_id, record, pass_file(repo))


def open_json(run_cli, repo, *extra):
    result = run_cli("open", "--json", *extra, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


# --- the pass record


def test_the_pass_record_keeps_each_surviving_non_blocking_finding_and_the_gaps(passed):
    record = read(passed, "reduce")
    assert pl.check_record("pass_record", record) == []
    assert record["open_findings"] == [
        {"stage": "review", "id": "correctness-1", "lens": "correctness", "severity": "MAJOR", "file": "src/app.py", "line": 21, "claim": "claim correctness-1", "failure_scenario": "the failure of correctness-1"},
        {"stage": "review", "id": "correctness-2", "lens": "correctness", "severity": "MINOR", "file": "src/app.py", "line": 30, "claim": "claim correctness-2", "failure_scenario": "the failure of correctness-2"},
    ]  # tests-1 did not survive: it is not open
    assert record["gaps"] == [
        {"stage": "review", "id": "G-1", "kind": "missing_test", "detail": "the detail of G-1", "ac": "AC-1"},
        {"stage": "review", "id": "G-2", "kind": "edge_case", "detail": "the detail of G-2", "ac": None},
    ]
    assert json.loads(pass_file(passed).read_text(encoding="utf-8")) == record  # the copy in .plumbline/pass/ holds them too


def test_a_run_whose_review_leaves_nothing_has_two_empty_lists(adopted, run_cli):
    docs_run(adopted, leaving(findings=[], survivors=(), gaps=[]))
    assert run_cli("pass", RUN, cwd=adopted).returncode == 0
    record = read(adopted, "reduce")
    assert (record["open_findings"], record["gaps"]) == ([], [])


def test_a_surviving_finding_is_open_only_while_it_is_not_blocking():
    review = review_leaving(sample("review_record")["diff_sha256"], [finding("a-1", "BLOCKING"), finding("a-2", "MAJOR")], ["a-1", "a-2", "a-2"], [gap("G-1")])
    found, gaps = pl.open_items("review", review)  # a blocker that stands fails its gate, so `pass` never sees one: the function says what it would keep
    assert [f["id"] for f in found] == ["a-2"] and [g["id"] for g in gaps] == ["G-1"]  # and a survivor listed twice is one finding


def test_each_review_stage_adds_its_findings_and_gaps_with_its_own_stage_id(adopted, run_cli):
    write_test_file(adopted)
    commit_all(adopted, "the tests")
    diff = change_of(adopted)
    put(adopted, "intake", intake_record("code.M", adopted))
    store_request(adopted)
    put(adopted, "plan", spec_record())
    put(adopted, "spec-review", review_leaving(diff, [finding("requirements-1", "MINOR", lens="requirements", file=".plumbline/runs/r1/request.md", line=1)], ["requirements-1"], [], target="plan"))
    put(adopted, "tests", written_tests_record())
    run_entry(adopted, "tests", None, RUN, exit_code=1)
    put(adopted, "test-review", review_leaving(diff, [finding("tests-1", "MAJOR", lens="tests", file="tests/test_app.py", line=3)], ["tests-1"], [gap("G-1")], target="tests"))
    put(adopted, "build", build_note_record())
    put(adopted, "verify", verify_record(diff=diff))
    run_entry(adopted, "verify", diff, RUN)
    put(adopted, "review", review_leaving(diff, FINDINGS[:2], ["correctness-2"], [gap("G-2", "uncovered_ac", ac="AC-2")]))
    result = run_cli("pass", RUN, cwd=adopted)
    assert result.returncode == 0, result.stdout
    record = read(adopted, "reduce")
    assert [(f["stage"], f["id"]) for f in record["open_findings"]] == [("spec-review", "requirements-1"), ("test-review", "tests-1"), ("review", "correctness-2")]
    assert [(g["stage"], g["id"]) for g in record["gaps"]] == [("test-review", "G-1"), ("review", "G-2")]


def test_the_ledger_pins_the_open_lists_with_the_pass_record(passed):
    entry = next(e for e in pl.read_ledger(passed, RUN) if e["kind"] == "pass")
    assert entry["pass_sha256"] == pl.file_sha256(pass_file(passed))  # an edit of what is open is an edit of the pass record: the push gate sees it
    record = json.loads(pass_file(passed).read_text(encoding="utf-8"))
    record["open_findings"] = []
    pass_file(passed).write_text(json.dumps(record), encoding="utf-8")
    how, detail = pl.coverage(passed, head(passed), pl.load_project(passed))
    assert how is None and "the pass record was altered after `pass` wrote it" in detail


def test_the_pass_record_schema_leaves_the_two_lists_optional_and_names_the_stage_of_each_entry():
    schema = pl.load_schema("pass_record")
    assert "open_findings" in schema["properties"] and "gaps" in schema["properties"]
    assert not {"open_findings", "gaps"} & set(schema["required"])  # a pass record from 0.4.2 stays valid
    old = sample("pass_record")
    del old["open_findings"], old["gaps"]
    assert pl.check_record("pass_record", old) == []
    assert schema["properties"]["open_findings"]["items"]["required"] == ["stage", "id", "lens", "severity", "file", "line", "claim", "failure_scenario"]
    assert schema["properties"]["gaps"]["items"]["required"] == ["stage", "id", "kind", "detail", "ac"]
    assert schema["properties"]["open_findings"]["items"]["properties"]["lens"]["enum"] == list(pl.KNOWN_LENSES)
    assert schema["properties"]["open_findings"]["items"]["properties"]["severity"]["enum"] == ["MAJOR", "MINOR"]  # a blocker that stands fails its gate


def test_a_pass_record_with_a_bad_open_finding_is_not_valid():
    record = sample("pass_record")
    record["open_findings"][0]["severity"] = "BLOCKING"
    record["open_findings"][0].pop("stage")
    record["gaps"][0]["kind"] = "typo"
    errors = pl.check_record("pass_record", record)
    assert any(e.startswith("$.open_findings[0].severity:") for e in errors) and any(e.startswith("$.open_findings[0].stage:") for e in errors)
    assert any(e.startswith("$.gaps[0].kind:") for e in errors)


def test_the_pass_record_renders_its_open_findings_and_gaps(run_cli, passed):
    out = run_cli("render", pass_file(passed), cwd=passed).stdout
    assert "## Open findings" in out and "**review/correctness-2** [MINOR] (correctness) `src/app.py:30` claim correctness-2 Failure: the failure of correctness-2" in out
    assert "## Gaps" in out and "**review/G-1** (missing_test, AC-1) the detail of G-1" in out and "**review/G-2** (edge_case) the detail of G-2" in out
    old = sample("pass_record")
    del old["open_findings"], old["gaps"]
    assert "Open findings" not in pl.render_record("pass_record", old)  # a record from before 0.5.0 says nothing of them


# --- plumbline.py open


def test_open_lists_the_findings_and_gaps_of_the_run_that_covers_head(run_cli, passed):
    result = run_cli("open", cwd=passed)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == f"run r1 (commit {head(passed)[:7]}, row docs): 2 open findings, 2 gaps"
    assert "  findings:" in lines and "    review/correctness-1 [MAJOR] src/app.py:21: claim correctness-1" in lines
    assert "      failure: the failure of correctness-1" in lines and "    review/correctness-2 [MINOR] src/app.py:30: claim correctness-2" in lines
    assert "  gaps:" in lines and "    review/G-1 (missing_test, AC-1): the detail of G-1" in lines and "    review/G-2 (edge_case): the detail of G-2" in lines
    assert "tests-1" not in result.stdout


def test_open_says_when_a_run_left_nothing_open(adopted, run_cli):
    docs_run(adopted, leaving(findings=[], survivors=(), gaps=[]))
    run_cli("pass", RUN, cwd=adopted)
    assert run_cli("open", cwd=adopted).stdout == f"run r1 (commit {head(adopted)[:7]}, row docs): nothing open\n"


def test_open_json_prints_the_runs_with_what_the_pass_record_holds(run_cli, passed):
    data = open_json(run_cli, passed)
    [run] = data["runs"]
    record = read(passed, "reduce")
    assert run == {"run_id": "r1", "commit": head(passed), "row": "docs", "recorded": True, "open_findings": record["open_findings"], "gaps": record["gaps"]}


def test_open_takes_a_named_run_and_all_lists_every_passed_run_oldest_first(run_cli, passed):
    docs_run(passed, leaving(findings=FINDINGS[:1], survivors=("correctness-1",), gaps=[]), "r2")
    assert run_cli("pass", "r2", cwd=passed).returncode == 0
    for name in os.listdir(run_path(passed, "r1")):
        os.utime(run_path(passed, "r1", name), (1_000_000_000, 1_000_000_000))  # r1 passed first
    assert [r["run_id"] for r in open_json(run_cli, passed)["runs"]] == ["r2"]  # the pass record of HEAD is r2's now
    assert [r["run_id"] for r in open_json(run_cli, passed, "r1")["runs"]] == ["r1"]
    assert [(r["run_id"], len(r["open_findings"])) for r in open_json(run_cli, passed, "--all")["runs"]] == [("r1", 2), ("r2", 1)]
    text = run_cli("open", "--all", cwd=passed).stdout
    assert text.index("run r1 ") < text.index("run r2 ")


def test_open_all_skips_a_run_that_did_not_pass_and_says_so_when_none_did(adopted, run_cli):
    assert run_cli("open", "--all", cwd=adopted).stdout == "no run has a pass record\n"
    assert open_json(run_cli, adopted, "--all") == {"runs": []}
    docs_run(adopted, leaving())  # a finished run that was never passed
    assert open_json(run_cli, adopted, "--all") == {"runs": []}


def test_open_all_skips_a_directory_that_is_no_run(run_cli, passed):
    (passed / ".plumbline" / "runs" / "active").mkdir()  # a name the run ids refuse: it sits beside the run directories as ACTIVE does
    assert [r["run_id"] for r in open_json(run_cli, passed, "--all")["runs"]] == ["r1"]


def test_open_refuses_a_head_no_pass_record_covers_and_a_run_that_did_not_pass(run_cli, passed):
    docs_run(passed, leaving(), "r3")  # never passed
    named = run_cli("open", "r3", cwd=passed)
    assert named.returncode == 1 and "run 'r3' has no pass record" in named.stderr and "Nothing to list" in named.stderr and named.stdout == ""
    write = passed / "docs" / "next.md"
    write.parent.mkdir()
    write.write_text("next\n", encoding="utf-8")
    commit_all(passed, "next")  # HEAD has moved on from the pass
    moved = run_cli("open", cwd=passed)
    assert moved.returncode == 1 and f"HEAD {head(passed)[:7]} is not covered by a pass record" in moved.stderr and "open --all" in moved.stderr and moved.stdout == ""


def test_open_refuses_a_head_that_only_an_override_covers(run_cli, adopted):
    run_cli("override", "--reason", "The pipeline cannot run offline; a one-line typo fix.", cwd=adopted)
    result = run_cli("open", cwd=adopted)
    assert result.returncode == 1 and "is not covered by a pass record" in result.stderr


def test_open_could_not_run_for_a_run_that_does_not_exist_or_for_all_with_a_run(run_cli, passed):
    missing = run_cli("open", "nope", cwd=passed)
    assert missing.returncode == 2 and "there is no run 'nope'" in missing.stderr
    both = run_cli("open", "--all", "r1", cwd=passed)
    assert both.returncode == 2 and "--all lists every passed run, so it takes no RUN" in both.stderr


def test_open_reads_a_pass_record_from_before_0_5_0_as_not_recorded(adopted, run_cli):
    docs_run(adopted, leaving())
    pass_without_the_lists(adopted)
    assert pl.coverage(adopted, head(adopted), pl.load_project(adopted))[0] == "pass"  # the old record still covers HEAD
    result = run_cli("open", cwd=adopted)
    assert result.returncode == 0 and "not recorded (the pass record is from before 0.5.0 and keeps no open findings)" in result.stdout
    [run] = open_json(run_cli, adopted)["runs"]
    assert (run["recorded"], run["open_findings"], run["gaps"]) == (False, [], [])


def test_open_changes_nothing_in_the_run(run_cli, passed):
    before = {p: p.read_bytes() for p in (passed / ".plumbline").rglob("*") if p.is_file()}
    run_cli("open", cwd=passed)
    run_cli("open", "--all", "--json", cwd=passed)
    assert {p: p.read_bytes() for p in (passed / ".plumbline").rglob("*") if p.is_file()} == before


# --- status


def test_status_counts_what_the_covering_run_left_open(run_cli, passed):
    out = run_cli("status", cwd=passed).stdout.splitlines()
    covered = next(i for i, line in enumerate(out) if line.startswith(f"HEAD {head(passed)[:7]}: covered by a pass record"))
    assert out[covered + 1] == "open: 2 findings, 2 gaps; `plumbline.py open` lists them"


def test_status_counts_one_finding_and_one_gap_in_the_singular(adopted, run_cli):
    docs_run(adopted, leaving(survivors=("correctness-1",), gaps=GAPS[:1]))
    run_cli("pass", RUN, cwd=adopted)
    assert "open: 1 finding, 1 gap; `plumbline.py open` lists them" in run_cli("status", cwd=adopted).stdout


def test_status_says_open_none_when_nothing_is_left_and_nothing_for_an_older_pass_record(adopted, run_cli):
    docs_run(adopted, leaving(findings=[], survivors=(), gaps=[]))
    run_cli("pass", RUN, cwd=adopted)
    assert "open: none" in run_cli("status", cwd=adopted).stdout
    pass_without_the_lists(adopted)
    out = run_cli("status", cwd=adopted).stdout
    assert "covered by a pass record" in out and "open:" not in out


def test_status_shows_no_open_line_where_head_is_not_covered(run_cli, adopted):
    docs_run(adopted, leaving())
    assert "open:" not in run_cli("status", cwd=adopted).stdout


# --- the run skill and the README


def run_skill():
    return frontmatter(REPO / "skills" / "run" / "SKILL.md")[1]


def test_the_run_skill_says_how_a_follow_up_starts_from_the_open_findings():
    text = run_skill().split("## 7. A follow-up from the open findings", 1)[1].split("## If the orchestrator is not available", 1)[0]
    assert "`pass` writes what the run leaves open into its pass record" in text and "`.plumbline/`, which git ignores" in text
    assert "`PLUMBLINE open [RUN] [--json]`" in text and "every passed run with `--all`" in text and "`PLUMBLINE status` shows their count" in text
    assert "**A finding that describes a failure** becomes a `fix` run, one finding per run" in text
    assert "Its failure scenario is the bug report" in text and "`plan --intent fix`" in text
    assert "**Gaps of the kinds `missing_test` and `uncovered_ac`** become a change of the `tests` row" in text and "`--row tests`" in text
    assert "**The request names where it came from**: the run id and the ids of the findings or gaps it takes up" in text
    assert "Write it to the request file (section 2), so that the new run's `request.md` shows its origin" in text
    assert "what the run leaves open: the surviving findings that are not BLOCKING and the detective's gaps (`PLUMBLINE open`, section 7)" in between(run_skill(), "## 5. When every gate has passed", "## 6. Push")


def test_the_status_skill_names_the_count_and_the_command():
    body = frontmatter(REPO / "skills" / "status" / "SKILL.md")[1]
    assert "`open: 3 findings, 4 gaps`" in body and 'plumbline.py" open' in body


def test_the_readme_documents_the_open_command_the_pass_records_lists_and_the_follow_up():
    commands = section("The command line", 3)
    row = next(line for line in commands.splitlines() if line.startswith("| `open "))
    assert row.startswith("| `open [RUN] [--all] [--json]` |") and "`--all` lists every passed run, oldest first" in row and "`recorded` (false for a pass record from before 0.5.0)" in row
    status = next(line for line in commands.splitlines() if line.startswith("| `status "))
    assert "with the count of what the covering run left open" in status
    text = section("Runs, gates and the pass record")
    paragraph = text.split("**Open findings.**", 1)[1].split("\n\n", 1)[0]
    for needed in (
        "which git ignores", "`open_findings`, each surviving finding that is not BLOCKING, of every review stage", "`gaps`, the detective's gaps", "A pass record from before 0.5.0 has neither",
        "`open: 3 findings, 4 gaps; `plumbline.py open` lists them`", "a finding that describes a failure becomes a `fix` run, one finding per run, with its failure scenario as the bug report",
        "test gaps become a change of the `tests` row", "the request names the run and the finding ids, so that the new run's `request.md` shows where it came from",
    ):
        assert needed in paragraph, needed
    assert "start a follow-up run from the open findings" in section("Skills")
