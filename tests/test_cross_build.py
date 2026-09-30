"""Where the two halves of 0.4.0 meet. The gates that measure and trace (the CLI, the SubagentStop hook) and the hooks that hold (the
PreToolUse hook) were built apart, so each side's rule is tested here against the other's:

  - .plumbline/runs/ACTIVE, which `plan --intent` writes and every hook reads, is written by nothing else;
  - a review that fails with blockers standing and rounds left gets its next round's directory from `gate`, and the round the hook holds
    the review agents to, the round `merge-review` merges and the round `gate` counts are one number;
  - the hook's check of a `git commit` reads the change on a copy of the index and leaves the real one as it was;
  - the builder reads none of the junit files a measured run writes into a run's directory.

The hook is called in process, with PLUMBLINE_HOOK_DEBUG set, so an error inside a rule fails the test instead of passing for an allow."""
import hashlib
import itertools
import json
import os

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import CLI, commit_all, git, write
from hookdata import bash_payload, stop_payload, tool_payload
from rundata import (
    CONTROLLED, RUN, adopt, adopt_base, ledger, now_hash, put, put_part, read, run_path, spec_record, verify_record, written_tests_record,
)
from samples import sample

ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective")
ACTIVE = ".plumbline/runs/ACTIVE"


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt_base(repo, commands={"test": CONTROLLED})  # adopted long ago: what changes afterwards is measured from a base that has plumbline.toml
    return repo


def begin(run_cli, repo, row="code.S", run_id=RUN):
    """Start a run the way /plumbline:run does."""
    result = run_cli("plan", "--run-id", run_id, "--intent", "feature", "--row", row, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr


def agent(role):
    return f"plumbline:{role}" if role else None


def write_denial(repo, role, path, tool="Write", key="file_path"):
    """The reason the hook gives an agent of `role` (None: the main session) for a write to `path`, or None when it lets it through."""
    target = path if os.path.isabs(str(path)) else str(repo / path)
    return pre.decide(tool_payload(repo, tool, {key: target, "content": "x", "old_string": "a", "new_string": "b"}, agent_type=agent(role)))


def ask(repo, command, role=None, cwd=None):
    return pre.decide(bash_payload(cwd or repo, command, agent_type=agent(role)))


def read_denial(repo, tool, tool_input, role="builder"):
    return pre.decide(tool_payload(repo, tool, tool_input, agent_type=agent(role)))


# ------------------------------------------------------------------ .plumbline/runs/ACTIVE is written by `plan --intent` and nothing else


@pytest.fixture
def started(adopted, run_cli):
    begin(run_cli, adopted)
    assert (adopted / ACTIVE).read_bytes() == b"r1\n"
    return adopted


@pytest.mark.parametrize("role", [*ROLES, None])
@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_no_agent_and_not_the_main_session_writes_the_active_file(started, role, tool, key):
    reason = write_denial(started, role, ACTIVE, tool, key)
    assert reason and "is written only by plumbline.py commands" in reason and ".plumbline/runs/ACTIVE" in reason, (role, tool)
    assert "plan --intent" in reason  # and the reason says what writes it


@pytest.mark.parametrize("name", ["ACTIVE", "active", "Active"])
def test_the_active_file_is_protected_whatever_the_case_of_its_name(started, name):
    assert write_denial(started, None, f".plumbline/runs/{name}")


def test_a_path_that_leads_to_the_active_file_counts_as_the_active_file(started):
    os.symlink("../.plumbline/runs/ACTIVE", started / "src" / "alias")
    os.symlink("../.plumbline/runs", started / "src" / "runs")
    for role in (None, "builder", "verifier"):
        assert write_denial(started, role, "src/alias"), role
        assert write_denial(started, role, "src/runs/ACTIVE"), role
    assert write_denial(started, None, ".plumbline/runs/../runs/ACTIVE")


PROTECTED_WRITES = [
    "echo r2 > .plumbline/runs/ACTIVE",
    "echo r2 >> .plumbline/runs/ACTIVE",
    "echo r2 >| .plumbline/runs/ACTIVE",
    "printf r2 > .plumbline/runs/ACTIVE",
    "echo r2 | tee .plumbline/runs/ACTIVE",
    "echo r2 | tee -a .plumbline/runs/ACTIVE",
    "cp /tmp/other .plumbline/runs/ACTIVE",
    "cp -t .plumbline/runs /tmp/ACTIVE",
    "cp /tmp/ACTIVE .plumbline/runs/",
    "mv /tmp/other .plumbline/runs/ACTIVE",
    "install /tmp/other .plumbline/runs/ACTIVE",
    "ln -sf r2 .plumbline/runs/ACTIVE",
    "rm .plumbline/runs/ACTIVE",
    "rm -f .plumbline/runs/ACTIVE",
    "mv .plumbline/runs/ACTIVE /tmp/gone",  # moving it away removes it
    "truncate -s 0 .plumbline/runs/ACTIVE",
    "touch .plumbline/runs/ACTIVE",
    "dd if=/tmp/other of=.plumbline/runs/ACTIVE",
    "sed -i s/r1/r2/ .plumbline/runs/ACTIVE",
    "cd .plumbline/runs && echo r2 > ACTIVE",
    "cd .plumbline && echo r2 > runs/ACTIVE",
    "bash -c 'echo r2 > .plumbline/runs/ACTIVE'",
    "eval 'echo r2 > .plumbline/runs/ACTIVE'",
    "true && echo r2 > ./.plumbline/runs/ACTIVE",
    "echo r2 > .plumbline/runs/../runs/ACTIVE",
    "echo r2 > .plumbline/runs/active",
    "curl -o .plumbline/runs/ACTIVE http://example.invalid/x",
    "cp /tmp/staged/* .plumbline/runs/",  # the names that land are not written down: the directory is what is named
]


@pytest.mark.parametrize("command", PROTECTED_WRITES)
def test_a_bash_command_that_writes_the_active_file_is_denied_to_the_main_session(started, command):
    reason = ask(started, command)
    assert reason and "is written only by plumbline.py commands" in reason, command


@pytest.mark.parametrize("role", ROLES)
def test_a_bash_command_that_writes_the_active_file_is_denied_to_every_agent_too(started, role):
    for command in ("echo r2 > .plumbline/runs/ACTIVE", "rm .plumbline/runs/ACTIVE", "cd .plumbline/runs && echo r2 > ACTIVE"):
        assert ask(started, command, role), (role, command)


@pytest.mark.parametrize(
    "command",
    [
        "cat .plumbline/runs/ACTIVE",
        "ls -la .plumbline/runs",
        "grep r1 .plumbline/runs/ACTIVE",
        "cp .plumbline/runs/ACTIVE /tmp/active.copy",
        "echo r2 > .plumbline/runs/r1/notes.txt",
        "echo r2 > .plumbline/ACTIVE",
        "echo r2 > docs/ACTIVE",
        "echo r2 > .plumbline/runs/r1/ACTIVE",
        "echo ACTIVE > notes.txt",
        f"python3 {CLI} plan --intent feature --row code.S --run-id r2",
        f"python3 {CLI} status",
    ],
)
def test_reading_the_active_file_and_writing_other_files_are_allowed(started, command):
    assert ask(started, command) is None, command


def test_plan_intent_writes_the_active_file_and_the_hook_does_not_stand_in_its_way(started, run_cli):
    assert ask(started, f"python3 {CLI} plan --intent feature --row code.S --run-id r2") is None
    assert run_cli("plan", "--run-id", "r2", "--intent", "feature", "--row", "code.S", cwd=started).returncode == 0
    assert (started / ACTIVE).read_bytes() == b"r2\n"
    assert ask(started, "cat .plumbline/runs/ACTIVE") is None


def test_a_repository_that_has_not_adopted_plumbline_is_left_alone(repo):
    write(repo / ".plumbline" / "runs" / "ACTIVE", "r1\n")
    assert write_denial(repo, None, ACTIVE) is None
    assert ask(repo, "echo r2 > .plumbline/runs/ACTIVE") is None


# ----------------------------------------------------------- review rounds: `gate` opens the next round's directory


def unit_dir(repo, stage="review", run_id=RUN):
    return run_path(repo, run_id, stage)


def finding(fid, lens, severity="BLOCKING"):
    return {
        "id": fid, "lens": lens, "file": "src/retry.py", "line": 3, "claim": f"claim {fid}", "failure_scenario": "a concrete input",
        "rule": "AC-1", "evidence": "return url", "outside_code": None, "severity": severity,
    }


def review_round(repo, run_cli, n, stage="review", lenses=("correctness", "tests"), defenders=3, blocking=True):
    """The agents of round `n` write where the hook lets them (which the test asks it first), and `merge-review` merges the round it
    finds highest, as /plumbline:run runs it: with no --round."""
    for lens in lenses:
        found = [finding(f"{lens}-{n}", lens)] if blocking and lens == lenses[0] else []
        path = f"{stage}/round-{n}/prosecutor-{lens}.json"
        assert write_denial(repo, "prosecutor", f".plumbline/runs/{RUN}/{path}") is None, path
        put_part(repo, stage, f"prosecutor-{lens}", {"lens": lens, "findings": found}, round_no=n)
    for k in range(1, defenders + 1):
        name = f"defender-{k}"
        found = [finding(f"{lenses[0]}-{n}", lenses[0])] if blocking else []
        defenses = [{"finding_id": f["id"], "defender": name, "verdict": "conceded", "quote": "", "reason": "the code has it"} for f in found]
        assert write_denial(repo, "defender", f".plumbline/runs/{RUN}/{stage}/round-{n}/{name}.json") is None
        put_part(repo, stage, name, {"defender": name, "defenses": defenses}, round_no=n)
    result = run_cli("merge-review", RUN, stage, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert read(repo, stage)["round"] == n  # the highest round directory is the round merged, and it is round n


def gate(run_cli, repo, stage="review"):
    return run_cli("gate", RUN, stage, cwd=repo)


def only_this_round_is_open(repo, stage, n):
    """The hook holds the stage's review agents to round n: the highest directory is n, and no other round takes a write."""
    assert pre.current_round(pl, repo, RUN, stage) == n
    for role, name in (("prosecutor", "prosecutor-x.json"), ("defender", "defender-9.json"), ("detective", "detective.json")):
        assert write_denial(repo, role, f".plumbline/runs/{RUN}/{stage}/round-{n}/{name}") is None, (role, n)
        assert write_denial(repo, role, f".plumbline/runs/{RUN}/{stage}/round-{n + 1}/{name}"), (role, n + 1)
        if n > 1:
            reason = write_denial(repo, role, f".plumbline/runs/{RUN}/{stage}/round-{n - 1}/{name}")
            assert reason and f"current round of {stage} (round-{n})" in reason, (role, n - 1)


def test_a_failed_review_opens_round_two_and_the_hook_merge_review_and_gate_all_count_round_two(run_cli, adopted):
    begin(run_cli, adopted)
    assert not unit_dir(adopted).exists()
    only_this_round_is_open(adopted, "review", 1)  # a stage with no round directory is in round 1
    review_round(adopted, run_cli, 1)
    failed = gate(run_cli, adopted)
    assert failed.returncode == 1, failed.stdout
    assert "1 blocker(s) survive: correctness-1" in failed.stdout and "round 1 of 3" in failed.stdout
    assert "round 2 of 3 is open: .plumbline/runs/r1/review/round-2/" in failed.stdout
    assert (unit_dir(adopted) / "round-2").is_dir() and not (unit_dir(adopted) / "round-3").exists()
    only_this_round_is_open(adopted, "review", 2)
    review_round(adopted, run_cli, 2)  # merge-review, with no --round, merges round 2: the highest directory the CLI made
    assert [e["round"] for e in ledger(adopted) if e["kind"] == "merge"] == [1, 2]
    failed = gate(run_cli, adopted)
    assert failed.returncode == 1 and "round 2 of 3" in failed.stdout and "round 3 of 3 is open: .plumbline/runs/r1/review/round-3/" in failed.stdout
    only_this_round_is_open(adopted, "review", 3)
    review_round(adopted, run_cli, 3)
    exhausted = gate(run_cli, adopted)
    assert exhausted.returncode == 3, exhausted.stdout
    assert "round 3 of 3" in exhausted.stdout and "has used its rounds" in exhausted.stdout and "is open" not in exhausted.stdout
    assert not (unit_dir(adopted) / "round-4").exists()  # no fourth round is opened, and the hook still holds the agents to the third
    only_this_round_is_open(adopted, "review", 3)
    refused = run_cli("merge-review", RUN, "review", "--round", "4", cwd=adopted)
    assert refused.returncode == 2 and "past the 3 rounds" in refused.stderr


def test_the_round_after_a_rebuild_passes_when_it_is_clean_and_opens_nothing(run_cli, adopted):
    begin(run_cli, adopted)
    review_round(adopted, run_cli, 1)
    assert gate(run_cli, adopted).returncode == 1
    review_round(adopted, run_cli, 2, blocking=False)
    passed = gate(run_cli, adopted)
    assert passed.returncode == 0, passed.stdout
    assert "is open" not in passed.stdout and not (unit_dir(adopted) / "round-3").exists()
    assert {e["kind"] for e in ledger(adopted) if e.get("stage") == "review"} >= {"merge", "gate"}
    assert [e["passed"] for e in ledger(adopted) if e["kind"] == "gate" and e["stage"] == "review"] == [False, True]


def test_the_review_of_the_tests_has_two_rounds_and_opens_the_second_only(run_cli, adopted):
    begin(run_cli, adopted, "code.M")
    review_round(adopted, run_cli, 1, stage="test-review", lenses=("tests",), defenders=0)
    first = gate(run_cli, adopted, "test-review")
    assert first.returncode == 1 and "round 1 of 2" in first.stdout and "round 2 of 2 is open: .plumbline/runs/r1/test-review/round-2/" in first.stdout
    only_this_round_is_open(adopted, "test-review", 2)
    review_round(adopted, run_cli, 2, stage="test-review", lenses=("tests",), defenders=0)
    second = gate(run_cli, adopted, "test-review")
    assert second.returncode == 3 and "has used its rounds" in second.stdout
    assert not (unit_dir(adopted, "test-review") / "round-3").exists()


def test_the_gate_run_again_on_the_same_round_opens_no_further_round(run_cli, adopted):
    begin(run_cli, adopted)
    review_round(adopted, run_cli, 1)
    for _ in range(3):
        assert gate(run_cli, adopted).returncode == 1
    assert sorted(p.name for p in unit_dir(adopted).glob("round-*")) == ["round-1", "round-2"]


def test_a_gate_that_fails_for_the_ledgers_reason_opens_no_round(run_cli, adopted):
    begin(run_cli, adopted)
    missing = gate(run_cli, adopted)  # no review record yet
    assert missing.returncode == 1 and "is open" not in missing.stdout and not unit_dir(adopted).exists()
    review_round(adopted, run_cli, 1)
    record = read(adopted, "review")
    write(run_path(adopted, RUN, "review.json"), json.dumps({**record, "blockers_surviving": 0}))  # edited by hand after merge-review wrote it
    edited = gate(run_cli, adopted)
    assert edited.returncode == 1 and "changed after merge-review wrote it" in edited.stdout and "is open" not in edited.stdout
    assert sorted(p.name for p in unit_dir(adopted).glob("round-*")) == ["round-1"]
    review_round(adopted, run_cli, 1)  # merged again: the record is the merge's once more
    part = unit_dir(adopted) / "round-1" / "prosecutor-tests.json"
    part.write_text(part.read_text() + "\n")  # an agent's record touched after merge-review read it
    touched = gate(run_cli, adopted)
    assert touched.returncode == 1 and "changed after merge-review read it" in touched.stdout and "is open" not in touched.stdout
    assert sorted(p.name for p in unit_dir(adopted).glob("round-*")) == ["round-1"]


def test_a_review_that_fails_for_a_stale_change_alone_is_run_again_in_its_round(run_cli, adopted):
    begin(run_cli, adopted)
    review_round(adopted, run_cli, 1, blocking=False)
    write(adopted / "src" / "retry.py", "def retry(url):\n    return url\n")  # the change was edited after the review
    stale = gate(run_cli, adopted)
    assert stale.returncode == 1 and "the change was edited after this review" in stale.stdout
    assert "is open" not in stale.stdout and sorted(p.name for p in unit_dir(adopted).glob("round-*")) == ["round-1"]


def test_only_a_review_stage_opens_a_round(run_cli, adopted):
    begin(run_cli, adopted)
    put(adopted, "plan", spec_record())
    assert gate(run_cli, adopted, "plan").returncode == 0
    put(adopted, "verify", verify_record(green=False, diff=now_hash(adopted)))
    failed = gate(run_cli, adopted, "verify")
    assert failed.returncode == 1 and failed.stderr == "" and "FAIL" in failed.stdout and "round 1 of 3" in failed.stdout  # reported as ever, and no crash
    assert "is open" not in failed.stdout and not any(p.is_dir() for p in run_path(adopted, RUN).iterdir())


def test_open_next_round_holds_to_its_conditions(adopted):
    from dataclasses import replace

    project = pl.load_project(adopted)
    stages = {s["id"]: s for s in project.pipeline["stage"]}
    failing = pl.GateOutcome("review", "no_surviving_blockers", False, ["1 blocker(s) survive: F-1"], ".plumbline/runs/r1/review.json", None, sample("review_record"), True)
    assert pl.standing_blockers(failing.data) == ["F-1"]
    rounds = lambda: sorted(p.name for p in unit_dir(adopted).glob("round-*"))  # noqa: E731
    # a review that ended with a blocker standing, with a round left: the next round's directory
    for refused in (
        (stages["review"], failing, 3, 3),  # its rounds are used
        (stages["review"], failing, 1, None),  # a stage with no round limit
        (stages["review"], replace(failing, checked=False), 1, 3),  # the ledger's problem, not the gate's
        (stages["review"], replace(failing, data=None), 1, 3),  # no record
        (stages["review"], replace(failing, data={**failing.data, "survivors": [], "blockers_surviving": 0}), 1, 3),  # no blocker stands
        (stages["review"], replace(failing, data={**failing.data, "survivors": ["F-2"], "blockers_surviving": 0}), 1, 3),  # only a MAJOR finding stands
        (stages["verify"], failing, 1, 3),  # no review stage
    ):
        assert pl.open_next_round(adopted, RUN, *refused) is None, refused[1:]
        assert rounds() == []
    opened = pl.open_next_round(adopted, RUN, stages["review"], failing, 1, 3)
    assert opened == unit_dir(adopted) / "round-2" and opened.is_dir()
    assert pl.open_next_round(adopted, RUN, stages["review"], failing, 1, 3) == opened  # opening it again changes nothing
    assert rounds() == ["round-2"]


def test_both_builds_read_round_directories_the_same_way(tmp_path, repo):
    """The hook's `current_round` and the CLI's round scan take the same names for rounds, and the highest number wins."""
    root = tmp_path / "unit"
    for name in ("round-1", "round-2", "round-9", "round-10", "round-0", "round-01", "round-x", "rounds-11", "Round-12", "round-3.bak"):
        (root / name).mkdir(parents=True)
    write(root / "round-11", "a file, not a directory")
    found = sorted(n for n in (pl._round_number(p) for p in root.glob("round-*")) if n is not None)
    assert found == [1, 2, 9, 10]
    write_dir = repo / ".plumbline" / "runs" / RUN / "review"
    for name in ("round-1", "round-2", "round-9", "round-10", "round-0", "round-01", "round-x", "round-3.bak"):
        (write_dir / name).mkdir(parents=True)
    write(write_dir / "round-11", "a file, not a directory")
    assert pre.current_round(pl, repo, RUN, "review") == max(found) == 10
    assert pre.current_round(pl, repo, RUN, "no-such-stage") == 1  # a stage with no directory is in round 1


def test_the_stop_of_a_review_agent_in_the_round_gate_opened_is_let_go_and_merge_review_takes_the_record(run_cli, run_stop, adopted):
    begin(run_cli, adopted)
    review_round(adopted, run_cli, 1)
    assert gate(run_cli, adopted).returncode == 1  # round 2 is open
    record = {"lens": "correctness", "findings": [], "diff_sha256": now_hash(adopted)}
    path = run_path(adopted, RUN, "review", "round-2", "prosecutor-correctness.json")
    assert write_denial(adopted, "prosecutor", str(path)) is None
    write(path, json.dumps(record))
    stop = run_stop(stop_payload(adopted, "plumbline:prosecutor", f"done\nRECORD: {path.relative_to(adopted)}", agent_id="pro-2"), adopted)
    assert stop.returncode == 0 and stop.stdout == "", stop.stdout + stop.stderr  # SubagentStop lets the agent go
    entry = [e for e in ledger(adopted) if e["kind"] == "agent" and e["agent_id"] == "pro-2"]
    assert len(entry) == 1 and entry[0]["valid"] is True and entry[0]["record"] == ".plumbline/runs/r1/review/round-2/prosecutor-correctness.json"
    put_part(adopted, "review", "prosecutor-tests", {"lens": "tests", "findings": []}, round_no=2)
    for k in (1, 2, 3):
        put_part(adopted, "review", f"defender-{k}", {"defender": f"defender-{k}", "defenses": []}, round_no=2)
    merged = run_cli("merge-review", RUN, "review", cwd=adopted)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert read(adopted, "review")["round"] == 2 and gate(run_cli, adopted).returncode == 0


# ---------------------------------------------- the commit checks read the change on a copy of the index


_clock = itertools.count(2_100_000_000, 7)


def index_state(repo):
    path = repo / ".git" / "index"
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def stat_dirty(repo):
    """Make the index's stat data stale for two files without changing what they hold: git would refresh the index."""
    for name in ("README.md", "src/app.py"):
        stamp = next(_clock)
        os.utime(repo / name, (stamp, stamp))


def refreshing(repo, *git_args) -> bool:
    """Does this git command rewrite the index of `repo`, from an index that is up to date and whose stat data is then made stale?"""
    git(repo, "status", "--porcelain")
    stat_dirty(repo)
    before = index_state(repo)
    git(repo, *git_args)
    return index_state(repo) != before


HOME_PATH = "/ho" + "me/somebody/notes.txt"  # built from pieces: no file of the repository may hold an absolute home path
SECRET = "sk-ant-" + "a" * 24


def held_back(problems) -> str:
    return f"plumbline: this commit is held back. It {'; '.join(problems)}. Remove them, stage the change again, and commit again."


def scenario_staged(repo):
    write(repo / "src" / "app.py", f"PATH = '{HOME_PATH}'\n")
    git(repo, "add", "src/app.py")
    return "git commit -m x", "index"


def scenario_unstaged(repo):
    write(repo / "src" / "app.py", f"KEY = '{SECRET}'\n")
    return "git commit -a -m x", "tracked"


def scenario_untracked(repo):
    write(repo / "src" / "new.py", f"KEY = '{SECRET}'\n")
    os.symlink("../README.md", repo / "src" / "link.py")
    return "git add -A && git commit -m x", "all"


def scenario_clean(repo):
    write(repo / "src" / "app.py", "def main():\n    return 2\n")
    git(repo, "add", "src/app.py")
    return "git commit -m x", "index"


def scenario_clean_all(repo):
    write(repo / "src" / "app.py", "def main():\n    return 3\n")
    return "git commit -a -m x", "tracked"


@pytest.mark.parametrize("scenario", [scenario_staged, scenario_unstaged, scenario_untracked, scenario_clean, scenario_clean_all])
def test_the_hooks_commit_check_says_what_it_said_on_the_real_index_and_leaves_that_index_alone(adopted, scenario):
    command, scope = scenario(adopted)
    git(adopted, "status", "--porcelain")  # the index is up to date; then its stat data goes stale without any change of content
    stat_dirty(adopted)
    before = index_state(adopted)
    reason = ask(adopted, command)
    assert index_state(adopted) == before, "the check rewrote the real index"
    expected = pre.commit_problems(pl, adopted, scope)  # the same check on the real index (which it may rewrite: this is the reference)
    assert reason == (held_back(expected) if expected else None)
    assert bool(reason) == (scenario in (scenario_staged, scenario_unstaged, scenario_untracked))


def test_a_check_on_the_real_index_would_rewrite_it_which_is_why_the_hook_works_on_a_copy(adopted):
    if not refreshing(adopted, "diff", "HEAD"):
        pytest.skip("this git does not rewrite the index on `git diff`: the test above has nothing to protect")
    git(adopted, "status", "--porcelain")
    stat_dirty(adopted)
    before = index_state(adopted)
    pre.commit_problems(pl, adopted, "tracked")
    assert index_state(adopted) != before


@pytest.mark.parametrize("failure", [OSError("no space left on device"), pl.PlumblineError("git rev-parse failed")], ids=["os-error", "plumbline-error"])
def test_a_commit_check_that_cannot_make_a_copy_of_the_index_still_runs_on_the_index_itself(adopted, monkeypatch, failure):
    def refuse(root):
        raise failure

    monkeypatch.setattr(pl, "index_copy", refuse)
    write(adopted / "src" / "app.py", f"KEY = '{SECRET}'\n")
    assert ask(adopted, "git commit -a -m x") == held_back(["adds a key-shaped secret: src/app.py:1 (the value is not shown)"])
    write(adopted / "src" / "app.py", "def main():\n    return 4\n")
    assert ask(adopted, "git commit -a -m x") is None


def test_the_commit_check_leaves_no_copy_of_the_index_behind(adopted):
    write(adopted / "src" / "app.py", f"KEY = '{SECRET}'\n")
    listing = {p.name for p in adopted.parent.iterdir()} | {p.name for p in (adopted / ".git").iterdir()}
    assert ask(adopted, "git commit -a -m x")
    assert listing == {p.name for p in adopted.parent.iterdir()} | {p.name for p in (adopted / ".git").iterdir()}


def test_a_repository_with_no_commit_yet_is_checked_too(tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "main")
    adopt(fresh, commit=False)
    write(fresh / "notes.txt", f"KEY = '{SECRET}'\n")
    git(fresh, "add", "notes.txt")
    assert ask(fresh, "git commit -m x") == held_back(["adds a key-shaped secret: notes.txt:1 (the value is not shown)"])
    assert not (fresh / ".git" / "index.lock").exists()


def test_commit_problems_needs_nothing_of_the_module_but_git(adopted):
    """The contract `check-diff` and the hook rely on: whatever is handed in as `pl` need offer only `_git`."""

    class OnlyGit:
        def _git(self, root, *args, timeout=120):
            return pl._git(root, *args, timeout=timeout)

    write(adopted / "src" / "app.py", f"KEY = '{SECRET}'\nPATH = '{HOME_PATH}'\n")
    found = pre.commit_problems(OnlyGit(), adopted, "tracked")
    assert sorted(found) == ["adds a key-shaped secret: src/app.py:1 (the value is not shown)", "adds an absolute home path: src/app.py:2"]
    assert found == pre.commit_problems(pl, adopted, "tracked")


def test_check_diff_and_the_hook_share_the_stand_in_for_the_module(adopted):
    with pl.index_copy(adopted) as index:
        stand_in = pl.OnIndexCopy(index)
        assert stand_in.CONFIG_FILE == pl.CONFIG_FILE  # everything but `_git` is the module's own
        assert stand_in._git(adopted, "rev-parse", "--git-dir").returncode == 0
    with pl.index_copy(adopted) as index:
        assert pre.commit_problems(pl.OnIndexCopy(index), adopted, "tracked") == pre.commit_problems(pl, adopted, "tracked") == []


# --------------------------------- the builder cannot read what a measured run writes into a run's directory

FAKE_PYTEST = """#!/bin/sh
# a stand-in for pytest that writes the junit file it is asked for, with an assertion in it
for arg in "$@"; do
  case $arg in --junitxml=*) out=${arg#--junitxml=} ;; esac
done
printf '%s' '<testsuites><testsuite name="t" tests="2" failures="1" errors="0" skipped="0"><testcase classname="t" name="test_a"><failure message="assert 1 == 2"/></testcase><testcase classname="t" name="test_b"/></testsuite></testsuites>' > "$out"
exit 1
"""


@pytest.fixture
def measured(repo, run_cli):
    """A run whose tests and verify stages `gate` measured with a command that is pytest by name, so that `gate` wrote junit files."""
    write(repo / "pytest", FAKE_PYTEST)
    commit_all(repo, "a test runner")
    adopt_base(repo, commands={"test": "sh pytest"})
    begin(run_cli, repo)
    put(repo, "plan", spec_record())
    put(repo, "tests", written_tests_record())
    gate(run_cli, repo, "tests")
    put(repo, "verify", verify_record(diff=now_hash(repo)))
    gate(run_cli, repo, "verify")
    write(repo / ".plumbline" / "runs" / "r0" / "junit-verify.xml", "<testsuite/>")  # an earlier run's
    return repo


JUNIT = [".plumbline/runs/r1/junit-tests.xml", ".plumbline/runs/r1/junit-verify.xml", ".plumbline/runs/r0/junit-verify.xml"]


def test_the_measured_runs_leave_their_junit_files_in_the_run_directory(measured):
    for name in JUNIT[:2]:
        text = (measured / name).read_text()
        assert "assert 1 == 2" in text  # the file holds assertion text: it is what the builder must not see
    runs = [e for e in ledger(measured) if e["kind"] == "run"]
    assert {e["stage"] for e in runs} == {"tests", "verify"}
    assert all(e["commands"][0]["junit"] == {"tests": 2, "failures": 1, "errors": 0, "skipped": 0} for e in runs)  # only the counts reach the ledger


@pytest.mark.parametrize("name", JUNIT)
def test_the_builder_reads_no_junit_file_of_a_run_directory(measured, name):
    reason = read_denial(measured, "Read", {"file_path": str(measured / name)})
    assert reason and "under .plumbline/" in reason and "is another of plumbline's files" in reason, name
    assert read_denial(measured, "Grep", {"pattern": "assert", "path": str(measured / name)})
    assert read_denial(measured, "Glob", {"pattern": "*.xml", "path": str(measured / name).rsplit("/", 1)[0]})


def test_the_builder_cannot_search_the_run_directory_that_holds_them_either(measured):
    for tool in ("Grep", "Glob"):
        for path in (".plumbline/runs/r1", ".plumbline/runs", ".plumbline"):
            assert read_denial(measured, tool, {"pattern": "junit", "path": str(measured / path)}), (tool, path)
    assert read_denial(measured, "Glob", {"pattern": ".plumbline/runs/*/junit-*.xml", "path": str(measured)})
    assert read_denial(measured, "Glob", {"pattern": "junit-*.xml", "path": str(measured / ".plumbline" / "runs" / "r1")})


def test_the_builder_still_reads_what_it_is_briefed_with_from_the_same_directory(measured):
    for name in ("intake.json", "plan.json", "build.json"):
        assert read_denial(measured, "Read", {"file_path": str(measured / ".plumbline" / "runs" / "r1" / name)}) is None, name


@pytest.mark.parametrize("role", ["verifier", "prosecutor", "defender", "detective", "planner", "test-writer", None])
def test_the_other_agents_and_the_main_session_read_the_junit_files(measured, role):
    for name in JUNIT:
        assert read_denial(measured, "Read", {"file_path": str(measured / name)}, role) is None, (role, name)
