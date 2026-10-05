"""merge-review checks quotes and evidence against the change (C-03), keeps tests-lens findings apart from the rest (C-06, C-07), refuses parts
made against another change (C-09), and holds a review to its stage's rounds (C-08)."""
import json

import pytest

import plumbline as pl
from helpers import commit_all, git, write
from rundata import RUN, adopt, begin, ledger, now_hash, put, put_part, read, run_path

APP = "def fetch(url):\n    for attempt in range(3):\n        try:\n            return get(url)\n        except OSError:\n            continue\n    return None\n"


@pytest.fixture
def unit(repo):
    """An adopted repository with a code.S run and a change in progress: src/app.py, edited and not committed. The stage has the lenses
    correctness and tests, 3 defenders and a survival threshold of 2."""
    adopt(repo)
    begin(repo, "code.S")
    write(repo / "src" / "app.py", APP)
    return repo


def finding(fid, lens="correctness", severity="BLOCKING", file="src/app.py", evidence="except OSError:"):
    return {
        "id": fid, "lens": lens, "file": file, "line": 5, "claim": f"claim {fid}", "failure_scenario": f"scenario of {fid}",
        "rule": "AC-1", "evidence": evidence, "outside_code": None, "severity": severity,
    }


def parts(repo, findings_by_lens, refuting=(), quote="return get(url)", round_no=1):
    """The prosecutors' records and three defenders, those in `refuting` refuting every finding with `quote`."""
    lenses = {"correctness": [], "tests": [], **findings_by_lens}
    for lens, findings in lenses.items():
        put_part(repo, "review", f"prosecutor-{lens}", {"lens": lens, "findings": findings}, round_no)
    every = [f for findings in lenses.values() for f in findings]
    for k in (1, 2, 3):
        name = f"defender-{k}"
        defenses = [
            {"finding_id": f["id"], "defender": name, "verdict": "refuted" if k in refuting else "conceded", "quote": quote if k in refuting else "", "reason": "read it"} for f in every
        ]
        put_part(repo, "review", name, {"defender": name, "defenses": defenses}, round_no)


def merge(run_cli, repo, *extra):
    return run_cli("merge-review", RUN, "review", *extra, cwd=repo)


def survivors(repo):
    return read(repo, "review")["survivors"]


# --- a defender's quote counts when the change or the finding's file holds it


@pytest.mark.parametrize(
    "quote,counts",
    [
        ("return get(url)", True),  # a line of the change
        ("except OSError:\n            continue", True),  # two lines: their line break and indentation are no part of it
        ("   return   get(url)   ", True),  # whitespace is normalised
        ("for attempt in range(3):", True),
        ("return get(url) except OSError: continue", True),  # the same words in one run of whitespace
        ("return get(urls)", False),  # not in the code
        ("the author obviously meant this", False),  # prose
        ("x", False),  # too short to prove anything, and in most files
        ("try:", False),  # under six characters
        ("     ", False),
        ("", False),
        ("def fetch(url):\n    raise ValueError", False),  # the first line is there; the rest is not
    ],
)
def test_a_refutation_counts_only_when_its_quote_occurs_in_the_change_or_the_file(run_cli, unit, quote, counts):  # C-03
    parts(unit, {"correctness": [finding("correctness-1")]}, refuting=(1, 2, 3), quote=quote)
    result = merge(run_cli, unit)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (survivors(unit) == []) is counts, result.stderr


def test_three_defenders_refuting_with_the_quote_x_do_not_remove_a_blocker(run_cli, unit):  # C-03: the reviewer's reproduction
    parts(unit, {"correctness": [finding("correctness-1")]}, refuting=(1, 2, 3), quote="x")
    result = merge(run_cli, unit)
    assert survivors(unit) == ["correctness-1"] and read(unit, "review")["blockers_surviving"] == 1
    assert "defender 'defender-1' refuted 'correctness-1' with a quote of fewer than 6 characters, which does not count" in result.stderr


def test_three_defenders_refuting_with_prose_do_not_remove_a_blocker(run_cli, unit):  # C-03
    parts(unit, {"correctness": [finding("correctness-1")]}, refuting=(1, 2, 3), quote="the author obviously meant this to be fine")
    result = merge(run_cli, unit)
    assert survivors(unit) == ["correctness-1"]
    assert "with a quote that is in neither the change nor src/app.py, which does not count" in result.stderr


def test_a_refutation_without_any_quote_still_says_so(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1")]}, refuting=(1, 2), quote="")
    assert "refuted 'correctness-1' without quoting code, which does not count" in merge(run_cli, unit).stderr


def test_the_quote_may_come_from_the_finding_file_where_the_file_is_unchanged(run_cli, unit):
    commit_all(unit, "the change is committed; the finding is about a file no diff touches")
    write(unit / "src" / "other.py", "x = 1\n")
    put(unit, "intake", read(unit, "intake") | {"merge_base": git(unit, "rev-parse", "HEAD").strip()})
    write(unit / "src" / "untouched.py", "value = compute(1)\n")
    commit_all(unit, "untouched")
    put(unit, "intake", read(unit, "intake") | {"merge_base": git(unit, "rev-parse", "HEAD").strip()})
    write(unit / "src" / "other.py", "x = 2\n")  # the change is now only other.py
    parts(unit, {"correctness": [finding("correctness-1", file="src/untouched.py")]}, refuting=(1, 2, 3), quote="value = compute(1)")
    assert merge(run_cli, unit).returncode == 0
    assert survivors(unit) == []  # the quote is in the finding's file, which the diff leaves alone


def test_a_quote_from_another_file_than_the_findings_does_not_count_unless_the_diff_has_it(run_cli, unit):
    write(unit / "src" / "elsewhere.py", "secret_line = 1\n")
    commit_all(unit, "elsewhere")
    put(unit, "intake", read(unit, "intake") | {"merge_base": git(unit, "rev-parse", "HEAD").strip()})
    write(unit / "src" / "app.py", APP + "# touched\n")
    parts(unit, {"correctness": [finding("correctness-1")]}, refuting=(1, 2, 3), quote="secret_line = 1")
    merge(run_cli, unit)
    assert survivors(unit) == ["correctness-1"]


def test_a_quote_from_a_removed_line_of_the_diff_counts(run_cli, unit):
    commit_all(unit, "app.py as it is")
    put(unit, "intake", read(unit, "intake") | {"merge_base": git(unit, "rev-parse", "HEAD").strip()})
    write(unit / "src" / "app.py", "def fetch(url):\n    return get_once(url)\n")  # the retry loop is gone
    parts(unit, {"correctness": [finding("correctness-1", evidence="return get(url)")]}, refuting=(1, 2, 3), quote="for attempt in range(3):")
    merge(run_cli, unit)
    assert survivors(unit) == []


def test_a_finding_about_a_deleted_or_missing_file_is_still_judged_by_the_diff(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1", file="src/ghost.py")]}, refuting=(1, 2, 3), quote="return get(url)")
    assert merge(run_cli, unit).returncode == 0 and survivors(unit) == []  # the quote is in the diff of app.py


@pytest.mark.parametrize("file", ["../outside.py", "/etc/hostname", "src/../../outside.py"])
def test_a_finding_file_outside_the_repository_is_not_read(run_cli, unit, tmp_path, file):
    write(tmp_path / "outside.py", "quote_only_outside = True\n")
    parts(unit, {"correctness": [finding("correctness-1", file=file)]}, refuting=(1, 2, 3), quote="quote_only_outside = True")
    merge(run_cli, unit)
    assert survivors(unit) == ["correctness-1"]


# --- a prosecutor's evidence is checked the same way, and a finding whose evidence is not found stays, marked


def test_a_finding_whose_evidence_is_in_the_change_is_marked_verified(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1", evidence="except OSError:\n            continue")]})
    result = merge(run_cli, unit)
    assert result.returncode == 0 and [f["evidence_unverified"] for f in read(unit, "review")["findings"]] == [False]
    assert "evidence found in neither" not in result.stdout


def test_a_finding_whose_evidence_is_in_neither_place_stays_and_is_marked_and_listed(run_cli, unit):  # C-03
    parts(unit, {"correctness": [finding("correctness-1", evidence="the loop swallows every error"), finding("correctness-2", evidence="return get(url)")]})
    result = merge(run_cli, unit)
    assert result.returncode == 0
    record = read(unit, "review")
    assert [(f["id"], f["evidence_unverified"]) for f in record["findings"]] == [("correctness-1", True), ("correctness-2", False)]
    assert record["survivors"] == ["correctness-1", "correctness-2"] and record["blockers_surviving"] == 2  # the finding stays: fail closed
    assert "evidence found in neither the change nor its file (the findings stay, marked evidence_unverified): correctness-1 (src/app.py:5)" in result.stdout
    assert pl.check_record("review_record", record) == []


def test_short_evidence_is_found_by_the_same_test_without_the_minimum_a_refutation_needs(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1", evidence="get(")]})
    merge(run_cli, unit)
    assert read(unit, "review")["findings"][0]["evidence_unverified"] is False


def test_the_evidence_of_a_finding_about_an_untouched_file_is_found_in_that_file(run_cli, unit):
    commit_all(unit, "app.py committed")
    put(unit, "intake", read(unit, "intake") | {"merge_base": git(unit, "rev-parse", "HEAD").strip()})
    write(unit / "src" / "new.py", "y = 2\n")
    parts(unit, {"correctness": [finding("correctness-1", evidence="return get(url)")]})
    merge(run_cli, unit)
    assert read(unit, "review")["findings"][0]["evidence_unverified"] is False


def test_evidence_survives_a_diff_line_that_holds_form_feeds_and_odd_whitespace(run_cli, unit):
    write(unit / "src" / "app.py", "def f():\n    x = 1\x0c  y = 2\n")
    parts(unit, {"correctness": [finding("correctness-1", evidence="x = 1 y = 2")]})
    merge(run_cli, unit)
    assert read(unit, "review")["findings"][0]["evidence_unverified"] is False


def test_change_diff_lines_are_the_text_of_added_removed_and_context_lines_without_the_headers(unit):
    commit_all(unit, "app.py")
    base = git(unit, "rev-parse", "HEAD").strip()
    write(unit / "src" / "app.py", APP.replace("range(3)", "range(5)") + "+ starts with a plus\n-- and dashes\n@@ not a hunk header\n")
    lines = pl.change_diff_lines(unit, base, pl.worktree_tree(unit))
    assert "    for attempt in range(3):" in lines and "    for attempt in range(5):" in lines and "    return None" in lines  # removed, added, context
    assert "+ starts with a plus" in lines and "-- and dashes" in lines and "@@ not a hunk header" in lines
    assert not any(line.startswith(("diff --git", "index ", "--- ", "+++ ")) for line in lines)


# --- routing: the review record and merge-review's output keep tests-lens findings apart


def test_the_record_and_the_output_keep_tests_lens_findings_apart_from_the_rest(run_cli, unit):  # C-06
    write(unit / "tests" / "test_app.py", "def test_fetch():\n    assert fetch('u') == 'body'\n")
    parts(
        unit,
        {
            "correctness": [finding("correctness-1"), finding("correctness-2", severity="MAJOR", file="tests/test_app.py", evidence="assert fetch('u') == 'body'")],
            "tests": [finding("tests-1", "tests", evidence="assert fetch('u') == 'body'", file="tests/test_app.py"), finding("tests-2", "tests", severity="MINOR", file="src/app.py")],
        },
    )
    result = merge(run_cli, unit)
    assert result.returncode == 0, result.stdout
    record = read(unit, "review")
    assert record["routes"] == {"builder": ["correctness-1"], "test-writer": ["correctness-2", "tests-1", "tests-2"], "planner": []}
    lines = result.stdout.splitlines()
    builder = lines.index("surviving findings for the builder (give the builder this text and nothing else):")
    writer = next(i for i, line in enumerate(lines) if line.startswith("surviving findings for the test-writer"))
    assert builder < writer
    builder_text, writer_text = "\n".join(lines[builder + 1 : writer]), "\n".join(lines[writer + 1 :])
    assert "correctness-1 [BLOCKING] src/app.py:5: claim correctness-1 Failure: scenario of correctness-1 Rule: AC-1" in builder_text
    for ident in ("correctness-2", "tests-1", "tests-2"):
        assert ident not in builder_text and ident in writer_text
    assert "the tests lens, and findings about test files: the builder never sees these" in lines[writer]


def test_the_builders_text_holds_no_quote_of_code_no_evidence_and_no_review_path(run_cli, unit):  # C-06
    write(unit / "tests" / "test_app.py", "def test_fetch():\n    assert fetch('u') == 'the secret expected value'\n")
    parts(unit, {"correctness": [finding("correctness-1", evidence="return get(url)")], "tests": [finding("tests-1", "tests", file="tests/test_app.py", evidence="the secret expected value")]})
    out = merge(run_cli, unit).stdout
    builder_block = out[out.index("surviving findings for the builder") : out.index("surviving findings for the test-writer")]
    assert "return get(url)" not in builder_block and "the secret expected value" not in builder_block
    assert ".plumbline" not in builder_block and "review.json" not in builder_block and "round-1" not in builder_block


def test_a_round_with_survivors_for_one_reader_prints_that_list_only(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1")]})
    out = merge(run_cli, unit).stdout
    assert "surviving findings for the builder" in out and "surviving findings for the test-writer" not in out
    parts(unit, {"tests": [finding("tests-1", "tests", file="tests/x.py")]})
    out = merge(run_cli, unit).stdout
    assert "surviving findings for the test-writer" in out and "surviving findings for the builder" not in out


def test_a_round_without_survivors_prints_neither_list(run_cli, unit):
    parts(unit, {})
    out = merge(run_cli, unit).stdout
    assert "surviving findings" not in out and out.startswith("wrote .plumbline/runs/r1/review.json: round 1, 0 findings, 0 surviving")


def test_only_surviving_findings_are_routed(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1")], "tests": [finding("tests-1", "tests")]}, refuting=(1, 2, 3))
    merge(run_cli, unit)
    assert read(unit, "review")["routes"] == {"builder": [], "test-writer": [], "planner": []}


@pytest.mark.parametrize(
    "lens,file,who",
    [("tests", "src/app.py", "test-writer"), ("correctness", "tests/test_x.py", "test-writer"), ("security", "web/app.test.ts", "test-writer"),
     ("data", "pkg/test_data.py", "test-writer"), ("correctness", "src/app.py", "builder"), ("docs", "docs/testing.md", "builder"), ("boundaries", "src/contest.py", "builder")],
)
def test_route_of(unit, lens, file, who):
    project = pl.load_project(unit)
    assert pl.route_of(project.pipeline, finding("x", lens, file=file)) == who


def test_the_route_reads_the_tests_type_from_the_repositorys_own_types(unit):
    write(unit / "plumbline.toml", (unit / "plumbline.toml").read_text() + '\n[[type]]\nid = "tests"\npaths = ["checks/**"]\n')
    project = pl.load_project(unit)
    assert pl.route_of(project.pipeline, finding("x", "correctness", file="checks/cases.py")) == "test-writer"


# --- the parts carry the hash of the change they saw (C-09), and merge-review refuses those that saw another


def test_a_part_made_against_another_change_is_refused_and_the_files_it_names_are_listed(run_cli, unit):  # C-09: the reviewer's reproduction
    parts(unit, {"correctness": [finding("correctness-1")]})
    old = json.loads(run_path(unit, RUN, "review", "round-1", "prosecutor-correctness.json").read_text())["diff_sha256"]
    write(unit / "src" / "app.py", APP + "# edited after the prosecutors read the diff\n")
    result = merge(run_cli, unit)
    assert result.returncode == 1
    now = now_hash(unit)
    assert old != now
    for name in ("prosecutor-correctness", "prosecutor-tests", "defender-1", "defender-2", "defender-3"):
        assert f".plumbline/runs/r1/review/round-1/{name}.json: it covers the change {old[:12]}, but the files now hash to {now[:12]}; the change was edited after this agent read it, so run the agent again" in result.stdout
    assert not run_path(unit, RUN, "review.json").exists()


def test_an_old_round_merged_again_after_an_edit_is_not_stamped_with_the_new_hash(run_cli, unit):  # C-09
    parts(unit, {})
    assert merge(run_cli, unit).returncode == 0
    stamped = read(unit, "review")["diff_sha256"]
    write(unit / "src" / "app.py", APP + "# changed\n")
    assert merge(run_cli, unit, "--round", "1").returncode == 1
    assert read(unit, "review")["diff_sha256"] == stamped  # the record on disk is still the one of the change the round saw


def test_the_record_is_stamped_with_the_hash_the_parts_carry(run_cli, unit):
    parts(unit, {})
    merge(run_cli, unit)
    carried = {json.loads(p.read_text())["diff_sha256"] for p in run_path(unit, RUN, "review", "round-1").glob("*.json")}
    assert carried == {read(unit, "review")["diff_sha256"]} == {now_hash(unit)}


def test_one_stale_part_is_enough(run_cli, unit):
    parts(unit, {})
    stale = run_path(unit, RUN, "review", "round-1", "defender-2.json")
    record = json.loads(stale.read_text())
    record["diff_sha256"] = "d" * 64
    put_part(unit, "review", "defender-2", record)  # re-entered as its agent's own, so that only the hash is wrong
    result = merge(run_cli, unit)
    assert result.returncode == 1 and "defender-2.json: it covers the change dddddddddddd" in result.stdout and "defender-1.json: it covers" not in result.stdout


def test_a_part_without_the_hash_is_not_a_valid_record(run_cli, unit):
    parts(unit, {})
    path = run_path(unit, RUN, "review", "round-1", "prosecutor-tests.json")
    record = json.loads(path.read_text())
    del record["diff_sha256"]
    put_part(unit, "review", "prosecutor-tests", json.dumps(record))
    result = merge(run_cli, unit)
    assert result.returncode == 1 and "prosecutor-tests.json: not a valid findings_record: $.diff_sha256: missing required key" in result.stdout


def test_the_hash_of_a_part_is_compared_with_the_change_at_the_merge_not_with_head(run_cli, unit):
    parts(unit, {})
    commit_all(unit, "the change, committed after the agents read it")  # the same change, now in HEAD: the same hash
    assert merge(run_cli, unit).returncode == 0


# --- a review holds to its stage's rounds


def test_merge_review_refuses_a_round_past_the_stages_max_rounds(run_cli, unit):  # C-08
    result = merge(run_cli, unit, "--round", "4")
    assert result.returncode == 2 and "round 4 is past the 3 rounds stage 'review' has: stop, and bring the findings and the failing criteria to the builder" in result.stderr


def test_merge_review_of_the_latest_round_directory_is_held_to_the_cap_too(run_cli, unit):
    parts(unit, {}, round_no=4)
    result = merge(run_cli, unit)
    assert result.returncode == 2 and "round 4 is past the 3 rounds" in result.stderr


def test_the_third_round_is_the_last_one_merge_review_takes(run_cli, unit):
    parts(unit, {}, round_no=3)
    assert merge(run_cli, unit, "--round", "3").returncode == 0 and read(unit, "review")["round"] == 3


def test_the_test_review_has_two_rounds(run_cli, repo):
    adopt(repo)
    begin(repo, "code.M")
    assert run_cli("merge-review", RUN, "test-review", "--round", "3", cwd=repo).returncode == 2


def test_the_ledger_entry_of_a_merge_lists_what_was_merged_and_the_round(run_cli, unit):
    parts(unit, {"correctness": [finding("correctness-1")]}, round_no=2)
    merge(run_cli, unit, "--round", "2")
    [entry] = [e for e in ledger(unit) if e["kind"] == "merge"]
    assert entry["round"] == 2 and len(entry["parts"]) == 5 and all(set(p) == {"path", "sha256"} for p in entry["parts"])
    assert all("/round-2/" in p["path"] for p in entry["parts"])
