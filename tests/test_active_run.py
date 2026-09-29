"""The run in progress: `plan --intent` writes .plumbline/runs/ACTIVE, the run id and a newline, and `active_run_id` reads it. The hooks read the same
file, so its path and its format are fixed here."""
import json
import os

import pytest

import plumbline as pl
from helpers import numbered, write
from rundata import RUN, adopt_base, begin, put, run_path, spec_record

ACTIVE = ".plumbline/runs/ACTIVE"


@pytest.fixture
def adopted(repo):
    adopt_base(repo)
    write(repo / "src" / "new_module.py", numbered(10))
    return repo


def start(run_cli, repo, run_id=RUN, intent="review-only", *extra):
    return run_cli("plan", "--intent", intent, "--row", "code.S", "--run-id", run_id, *extra, cwd=repo)


def age(repo, run_id, seconds_ago):
    """Make a run look older: every file of it, and its directory."""
    stamp = 1_800_000_000 - seconds_ago
    directory = run_path(repo, run_id)
    for path in [*directory.iterdir(), directory]:
        os.utime(path, (stamp, stamp))


def test_plan_with_an_intent_writes_the_run_id_and_a_newline_to_the_active_file(run_cli, adopted):
    assert start(run_cli, adopted).returncode == 0
    assert (adopted / ACTIVE).read_bytes() == b"r1\n"


def test_the_next_run_replaces_it(run_cli, adopted):
    start(run_cli, adopted, "r1")
    start(run_cli, adopted, "r2")
    assert (adopted / ACTIVE).read_bytes() == b"r2\n"


def test_plan_without_an_intent_starts_nothing_and_writes_no_active_file(run_cli, adopted):
    assert run_cli("plan", "--row", "code.S", "--run-id", RUN, cwd=adopted).returncode == 0
    assert not (adopted / ACTIVE).exists() and not (adopted / ".plumbline").exists()


def test_a_start_that_is_refused_leaves_the_active_file_as_it_was(run_cli, adopted):
    start(run_cli, adopted, "r1")
    again = start(run_cli, adopted, "r1")  # a run that has begun is not started again
    assert again.returncode == 1 and (adopted / ACTIVE).read_bytes() == b"r1\n"
    write(adopted / ".plumbline" / "bad.json", "{not json")
    refused = start(run_cli, adopted, "r2", "spec-supplied", "--spec", ".plumbline/bad.json")
    assert refused.returncode == 1 and (adopted / ACTIVE).read_bytes() == b"r1\n"


def test_the_active_file_is_written_whole_or_not_at_all(run_cli, adopted):
    start(run_cli, adopted)
    assert sorted(p.name for p in (adopted / ".plumbline" / "runs").iterdir()) == ["ACTIVE", "r1"]  # no temporary file remains


@pytest.mark.parametrize("bad", ["ACTIVE", "active", "Active"])
def test_a_run_may_not_take_the_name_of_the_active_file(run_cli, adopted, bad):
    result = start(run_cli, adopted, bad)
    assert result.returncode == 2 and f"run id '{bad}' is taken by the file .plumbline/runs/ACTIVE; pick another --run-id" in result.stderr
    assert not (adopted / ".plumbline").exists()


def test_a_run_named_like_the_active_file_is_refused_by_every_command_that_takes_a_run(run_cli, adopted):
    for command in (("gate", "ACTIVE", "verify"), ("merge-review", "ACTIVE", "review"), ("tokens", "ACTIVE"), ("pass", "ACTIVE"), ("status", "--run", "ACTIVE")):
        result = run_cli(*command, cwd=adopted)
        assert result.returncode == 2 and "run id" in result.stderr, command


# --- active_run_id


def test_active_run_id_is_the_run_the_file_names_when_its_directory_exists(adopted):
    begin(adopted, "code.S", "older")
    begin(adopted, "code.S", "newer")
    age(adopted, "older", 1000)
    age(adopted, "newer", 0)
    assert pl.latest_run_id(adopted) == "newer" and pl.active_run_id(adopted) == "newer"
    pl.write_active(adopted, "older")
    assert pl.active_run_id(adopted) == "older" and pl.latest_run_id(adopted) == "newer"  # the file wins over the newest


def test_without_the_file_or_when_it_names_no_run_it_is_the_newest_run(adopted):
    begin(adopted, "code.S", "older")
    begin(adopted, "code.S", "newer")
    age(adopted, "older", 1000)
    age(adopted, "newer", 0)
    assert pl.active_run_id(adopted) == "newer"
    for text in ("gone\n", "", "\n", "../escape\n", "r 1\n", "older\nnewer\n", "ACTIVE\n"):
        write(adopted / ACTIVE, text)
        assert pl.active_run_id(adopted) == "newer", repr(text)


def test_the_file_may_lack_its_newline_and_may_have_trailing_whitespace(adopted):
    begin(adopted, "code.S", "older")
    begin(adopted, "code.S", "newer")
    age(adopted, "older", 1000)
    for text in ("older", "older\n", "older \n", "older\r\n"):
        write(adopted / ACTIVE, text)
        assert pl.active_run_id(adopted) == "older", repr(text)


def test_there_is_no_active_run_where_no_run_has_begun(repo):
    adopt_base(repo)
    assert pl.active_run_id(repo) is None
    (repo / ".plumbline" / "runs").mkdir(parents=True)
    write(repo / ACTIVE, "r1\n")
    assert pl.active_run_id(repo) is None


def test_the_active_file_is_no_run_to_latest_run_id(adopted):
    begin(adopted, "code.S", "r1")
    write(adopted / ACTIVE, "r1\n")
    os.utime(adopted / ACTIVE, (2_000_000_000, 2_000_000_000))
    assert pl.latest_run_id(adopted) == "r1"


def test_write_active_writes_the_run_id_and_a_newline(tmp_path):
    (tmp_path / ".plumbline" / "runs").mkdir(parents=True)
    path = pl.write_active(tmp_path, "abc-1.2")
    assert path == tmp_path / ".plumbline" / "runs" / "ACTIVE" and path.read_bytes() == b"abc-1.2\n"


# --- the commands that follow the run in progress


def test_status_shows_the_run_in_progress_not_the_newest(run_cli, adopted):
    start(run_cli, adopted, "r1")
    begin(adopted, "code.S", "stray")
    age(adopted, "r1", 1000)
    age(adopted, "stray", 0)
    out = run_cli("status", cwd=adopted).stdout
    assert "run r1:" in out and "run stray:" not in out


def test_status_without_the_file_shows_the_newest_run(run_cli, adopted):
    begin(adopted, "code.S", "older")
    begin(adopted, "code.S", "newer")
    age(adopted, "older", 1000)
    age(adopted, "newer", 0)
    assert "run newer:" in run_cli("status", cwd=adopted).stdout


def test_override_without_a_run_lists_the_stages_of_the_run_in_progress(run_cli, adopted):
    start(run_cli, adopted, "r1")  # review-only: verify and review
    begin(adopted, "code.S", "stray")  # the newest run, a feature of row code.S with more stages
    age(adopted, "r1", 1000)
    age(adopted, "stray", 0)
    result = run_cli("override", "--reason", "The pipeline cannot run offline; a one-line typo fix.", cwd=adopted)
    assert result.returncode == 0
    skipped = json.loads((adopted / ".plumbline" / "pass" / f"{pl.head_sha(adopted)}.override.json").read_text())["stages_skipped"]
    assert skipped == ["verify", "review"]


def test_the_run_in_progress_is_where_the_hook_stops_agents_records_belong(run_cli, adopted, run_stop):
    from hookdata import stop_payload

    start(run_cli, adopted, "r1", "feature")
    begin(adopted, "code.M", "stray")
    put(adopted, "plan", spec_record(), "stray", agent=False)
    age(adopted, "r1", 1000)
    age(adopted, "stray", 0)
    result = run_stop(stop_payload(adopted, message="RECORD: .plumbline/runs/stray/plan.json"), adopted)
    assert result.returncode == 2 and "the record must be in the run in progress, 'r1' (you named run 'stray')" in result.stdout
