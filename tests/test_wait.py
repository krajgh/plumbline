"""An agent that was resumed is working again. SendMessage wakes it in the background and returns at once, so the orchestrator's hook enters a
`resume` in the run's ledger, `wait` blocks until the ledger shows the agent's next stop, and a gate that meets the agent in between runs nothing
(the trace of a record is checked before any measured command: tests/test_measured_gates.py)."""
import json
import subprocess
import threading
import time

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import CLI, REPO, clean_env
from hookdata import activate_run, bash_payload, denial, start_run, stop_payload, tool_payload
from rundata import RUN, adopt, begin, exits, ledger, put, put_part, run_path, spec_record, verify_now, verify_record, write_ledger
from test_manifests import HOOKS, hook_of, load
from test_measured_gates import CommandRunner

ORCHESTRATOR = "plumbline:orchestrator"
PLUMBLINE = f"python3 {CLI}"


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


def agent_row(agent_id, stage="verify", **fields):
    """The ledger row of a stop, as the SubagentStop hook enters it."""
    return {"kind": "agent", "agent_id": agent_id, "agent_type": "plumbline:verifier", "stage": stage, "record": f".plumbline/runs/{RUN}/{stage}.json", "valid": True, **fields}


def resume_row(agent_id):
    return {"kind": "resume", "agent_id": agent_id}


def waits(run_cli, repo, agent_id, *extra):
    return run_cli("wait", RUN, agent_id, *extra, cwd=repo)


@pytest.fixture
def started(repo):
    """An adopted repository with a run begun (the docs row), and nothing in its ledger but the intake."""
    adopt(repo, commands={"test": exits(0)})
    begin(repo, "docs")
    return repo


# --- wait: it returns when the ledger shows a stop of the agent after its latest resume


def test_wait_returns_at_once_when_the_agent_stopped_after_its_resume_even_before_wait_began(run_cli, started):
    write_ledger(started, [agent_row("a1"), resume_row("a1"), agent_row("a1")])  # the agent was fast: it stopped before `wait` started
    result = waits(run_cli, started, "a1")
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "agent a1 (plumbline:verifier) has stopped; its record is .plumbline/runs/r1/verify.json, valid\n"


def test_wait_ignores_a_stop_older_than_the_latest_resume_and_times_out(run_cli, started):
    write_ledger(started, [agent_row("a1"), resume_row("a1")])  # it stopped once, was resumed, and has not stopped since
    began = time.monotonic()
    result = waits(run_cli, started, "a1", "--timeout", "1")
    assert result.returncode == 1 and result.stderr == ""
    assert result.stdout == "agent a1 has not stopped after 1 s; run `wait` again or hand back\n"
    assert 0.9 <= time.monotonic() - began < 20


def test_wait_looks_at_the_latest_resume_so_an_earlier_round_trip_does_not_answer_for_the_new_one(run_cli, started):
    write_ledger(started, [agent_row("a1"), resume_row("a1"), agent_row("a1"), resume_row("a1")])
    assert waits(run_cli, started, "a1", "--timeout", "1").returncode == 1
    write_ledger(started, [agent_row("a1", valid=False)])
    result = waits(run_cli, started, "a1", "--timeout", "1")
    assert result.returncode == 0 and result.stdout.endswith("its record is .plumbline/runs/r1/verify.json, not valid\n")  # a stop is a stop: the gate says what is wrong with it


def test_wait_counts_only_the_stops_of_the_agent_it_was_given(run_cli, started):
    write_ledger(started, [agent_row("a1"), resume_row("a1"), agent_row("a2", stage="tests"), {"kind": "leg", "agent_id": "a1"}])
    assert waits(run_cli, started, "a1", "--timeout", "1").returncode == 1  # another agent stopped, and a leg is no stop of a stage agent
    write_ledger(started, [agent_row("a1")])
    assert waits(run_cli, started, "a1", "--timeout", "1").returncode == 0


def test_wait_returns_when_the_stop_is_entered_while_it_waits(started, monkeypatch, capsys):
    monkeypatch.setattr(pl, "WAIT_POLL_SECONDS", 0.05)
    write_ledger(started, [agent_row("a1"), resume_row("a1")])
    timer = threading.Timer(0.4, lambda: write_ledger(started, [agent_row("a1")]))
    timer.start()
    began = time.monotonic()
    code = pl.main(["wait", RUN, "a1", "--timeout", "30", "--project", str(started)])
    timer.join()
    assert code == 0 and 0.3 <= time.monotonic() - began < 10
    assert capsys.readouterr().out.startswith("agent a1 (plumbline:verifier) has stopped")


def test_without_a_resume_entry_wait_counts_the_stops_after_it_began(started, monkeypatch, capsys):
    monkeypatch.setattr(pl, "WAIT_POLL_SECONDS", 0.05)
    write_ledger(started, [agent_row("a1")])  # a stop from before, and no resume entry says it was resumed since
    assert pl.main(["wait", RUN, "a1", "--timeout", "0", "--project", str(started)]) == 1
    timer = threading.Timer(0.3, lambda: write_ledger(started, [agent_row("a1")]))
    timer.start()
    code = pl.main(["wait", RUN, "a1", "--timeout", "30", "--project", str(started)])
    timer.join()
    assert code == 0 and capsys.readouterr().out.count("has stopped") == 1


def test_a_resume_entered_after_the_wait_began_counts_from_that_entry(started, monkeypatch):
    monkeypatch.setattr(pl, "WAIT_POLL_SECONDS", 0.05)
    write_ledger(started, [agent_row("a1")])

    def later():
        write_ledger(started, [resume_row("a1")])
        time.sleep(0.2)
        write_ledger(started, [agent_row("a1")])

    timer = threading.Timer(0.2, later)
    timer.start()
    assert pl.main(["wait", RUN, "a1", "--timeout", "30", "--project", str(started)]) == 0
    timer.join()


def test_wait_writes_nothing(run_cli, started):
    write_ledger(started, [agent_row("a1"), resume_row("a1"), agent_row("a1")])
    before = run_path(started, RUN, "ledger.jsonl").read_bytes()
    assert waits(run_cli, started, "a1").returncode == 0
    assert run_path(started, RUN, "ledger.jsonl").read_bytes() == before


def test_wait_polls_every_two_seconds_and_waits_540_by_default_under_the_bash_tools_ten_minutes():
    assert pl.WAIT_POLL_SECONDS == 2 and pl.DEFAULT_WAIT_TIMEOUT == 540 < 600
    wait = next(a for a in pl.build_parser()._actions if getattr(a, "choices", None)).choices["wait"]
    assert wait.parse_args(["r1", "a1"]).timeout == 540 and wait.parse_args(["r1", "a1", "--timeout", "30"]).timeout == 30
    assert {o for a in wait._actions for o in a.option_strings} >= {"--timeout", "--project"} and wait.allow_abbrev is False


@pytest.mark.parametrize(
    "args,said",
    [
        (["nope", "a1"], "there is no run 'nope'"),
        ([RUN, "../x"], "agent id '../x' must be letters, digits"),
        ([RUN, ""], "agent id '' must be letters, digits"),
        ([RUN, "a1", "--timeout", "-1"], "--timeout is a number of seconds, 0 or more"),
        (["ACTIVE", "a1"], "is taken by the file"),
    ],
)
def test_wait_refuses_what_is_no_run_or_agent_or_timeout(run_cli, started, args, said):
    result = run_cli("wait", *args, cwd=started)
    assert result.returncode == 2 and said in result.stderr and result.stdout == ""


def test_wait_takes_no_abbreviation_of_its_option(run_cli, started):
    result = run_cli("wait", RUN, "a1", "--time", "1", cwd=started)
    assert result.returncode == 2 and "unrecognized arguments: --time" in result.stderr


def test_wait_works_from_another_directory_through_project(run_cli, started, tmp_path):
    write_ledger(started, [agent_row("a1"), resume_row("a1"), agent_row("a1")])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_cli("wait", RUN, "a1", "--project", str(started), cwd=elsewhere).returncode == 0


# --- the orchestrator's Bash runs `wait`, and no other agent's does


def runs(repo, command, role="orchestrator"):
    return pre.decide(bash_payload(repo, command, agent_type=f"plumbline:{role}" if role else None))


@pytest.mark.parametrize(
    "command",
    [
        f"{PLUMBLINE} wait r1 a00c9fd37f4b4ada1", f"{PLUMBLINE} wait r1 a00c9fd37f4b4ada1 --timeout 540", f"{PLUMBLINE} wait r1 a1 --timeout=30 --project .",
        f"{PLUMBLINE} wait r1 a1 && {PLUMBLINE} gate r1 tests", f"{PLUMBLINE} wait r1 a1\n{PLUMBLINE} status --run r1",
    ],
)
def test_the_hook_allows_wait_for_the_orchestrator(started, command):
    assert runs(started, command) is None, command


def test_wait_is_a_command_of_the_plumbline_run_class_and_of_no_other_role(started):
    assert pre.PLUMBLINE_RUN[-1] == "wait"
    for role in ("planner", "test-writer", "verifier", "prosecutor", "defender", "detective", "canary"):
        assert "may run only" in runs(started, f"{PLUMBLINE} wait r1 a1", role), role
    assert "has no Bash" in runs(started, f"{PLUMBLINE} wait r1 a1", "builder")
    assert runs(started, f"{PLUMBLINE} wait r1 a1", None) is None  # the main session runs it too


def test_the_orchestrators_denial_names_wait_among_the_commands_and_a_variable_before_it_is_refused(started):
    assert "check-record, open and wait, and `plan --run RUN --json`" in runs(started, "sleep 30")
    assert "no VAR=value before them" in runs(started, f"R=r1 {PLUMBLINE} wait r1 a1")


# --- SendMessage: the orchestrator resumes only the agents its run knows, and the hook enters the resume


def message(repo, to="a1", sender=ORCHESTRATOR, **fields):
    tool_input = {"to": to, "summary": "the gate failed", "message": "fix it", **fields}
    if to is None:
        del tool_input["to"]
    return tool_payload(repo, "SendMessage", tool_input, agent_type=sender)


def resumes(repo, run=RUN):
    return [e for e in ledger(repo, run) if e["kind"] == "resume"]


@pytest.fixture
def stopped_agents(started):
    """A run whose ledger holds the stop of one stage agent, a1."""
    write_ledger(started, [agent_row("a1")])
    return started


def test_the_orchestrator_may_message_an_agent_the_runs_ledger_records_and_the_hook_enters_the_resume(stopped_agents):
    assert pre.decide(message(stopped_agents, "a1")) is None
    [entry] = resumes(stopped_agents)
    assert entry["kind"] == "resume" and entry["agent_id"] == "a1" and entry["at"] and set(entry) == {"kind", "agent_id", "at"}
    assert pre.decide(message(stopped_agents, "a1")) is None and len(resumes(stopped_agents)) == 2  # each message that wakes the agent is one


@pytest.mark.parametrize("to", ["a2", "a1 ", "A1", "", "*", "main", "team-lead", "a1,a2", None, 5, ["a1"]])
def test_the_orchestrator_may_message_no_one_else_and_the_denial_is_a_plain_sentence(stopped_agents, to):
    reason = pre.decide(message(stopped_agents, to))
    assert reason and reason.startswith("plumbline: the orchestrator sends a message only to an agent of its own run, by the id its launch gave, and ") and "is not one" in reason
    assert "no `agent` entry of the ledger of run r1 holds that id" in reason and "launch the stage's agent again" in reason
    assert resumes(stopped_agents) == []


def test_the_denial_names_the_id_it_refused(stopped_agents):
    assert "and `a2` is not one" in pre.decide(message(stopped_agents, "a2"))
    assert "and the message's `to` is not one" in pre.decide(message(stopped_agents, None))


def test_an_id_only_a_leg_a_resume_or_a_gate_names_is_not_an_agent_of_the_run(stopped_agents):
    write_ledger(stopped_agents, [{"kind": "leg", "agent_id": "o1"}, resume_row("r9"), {"kind": "gate", "stage": "verify", "gate": "verify_green", "passed": True, "agent_id": "g1"}])
    for to in ("o1", "r9", "g1"):
        assert pre.decide(message(stopped_agents, to)), to
    assert len(resumes(stopped_agents)) == 1  # the one the ledger was given: the hook entered nothing


def test_the_orchestrator_may_message_an_agent_of_the_active_run_and_not_of_another(started):
    activate_run(started, "r1")
    start_run(started, "r2", activate=False)
    write_ledger(started, [agent_row("a1")])  # in r1
    assert pre.decide(message(started, "a1")) is None and len(resumes(started, "r1")) == 1 and resumes(started, "r2") == []
    activate_run(started, "r2")
    assert "the ledger of run r2" in pre.decide(message(started, "a1"))
    write_ledger(started, [agent_row("b1", stage="tests")], "r2")
    assert pre.decide(message(started, "b1")) is None and [e["agent_id"] for e in resumes(started, "r2")] == ["b1"]


def test_with_no_run_in_progress_the_orchestrator_has_no_agent_to_message(repo):
    adopt(repo)
    reason = pre.decide(message(repo, "a1"))
    assert reason and "the ledger of the run in progress (no run is in progress)" in reason and not (repo / ".plumbline").exists()


def test_the_main_sessions_send_message_is_its_own_unrestricted_and_not_entered(stopped_agents):
    for to in ("a1", "a2", "", None, "team-lead"):
        assert pre.decide(message(stopped_agents, to, sender=None)) is None, to
    assert resumes(stopped_agents) == []  # `wait` counts the stops after it began, for the main session


def test_the_other_plumbline_agents_and_other_agents_send_message_unrestricted(stopped_agents):
    for sender in ("plumbline:planner", "plumbline:builder", "plumbline:wizard", "general-purpose", "probe:echo"):
        assert pre.decide(message(stopped_agents, "a2", sender=sender)) is None, sender
    assert resumes(stopped_agents) == []


def test_send_message_is_not_looked_at_where_plumbline_is_not_adopted(repo):
    assert pre.decide(message(repo, "a2")) is None and not (repo / ".plumbline").exists()


def test_a_message_without_a_working_directory_or_an_input_is_let_through(stopped_agents):
    payload = message(stopped_agents, "a2")
    assert pre.decide({**payload, "cwd": ""}) is None and pre.decide({**payload, "tool_input": "x"}) is None


def test_the_resume_rule_works_through_the_hook_process_and_the_registered_command(run_pre, stopped_agents, home, tmp_path):
    assert denial(run_pre(message(stopped_agents, "a2"), stopped_agents))
    assert denial(run_pre(message(stopped_agents, "a1"), stopped_agents)) is None and len(resumes(stopped_agents)) == 1
    registered = subprocess.run(
        hook_of("PreToolUse")["command"], shell=True, cwd=stopped_agents, capture_output=True, text=True, input=json.dumps(message(stopped_agents, "a1")),
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(REPO), GIT_CEILING_DIRECTORIES=str(tmp_path)),
    )
    assert registered.returncode == 0 and registered.stdout == ""
    assert len(resumes(stopped_agents)) == 2  # the sh filter let the message through, because the orchestrator's agent type names plumbline
    denied = subprocess.run(
        hook_of("PreToolUse")["command"], shell=True, cwd=stopped_agents, capture_output=True, text=True, input=json.dumps(message(stopped_agents, "a2")),
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(REPO), GIT_CEILING_DIRECTORIES=str(tmp_path)),
    )
    assert "sends a message only to an agent of its own run" in json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_hooks_matcher_names_send_message_and_the_hook_acts_on_it():
    matcher = load(HOOKS)["hooks"]["PreToolUse"][0]["matcher"]
    assert "SendMessage" in matcher.split("|") and matcher.endswith("|SendMessage")


# --- the SubagentStop hook: a resumed agent's next stop is entered, whatever it changed


def stop(run_stop, repo, agent_id="p-1", message="planned\nRECORD: .plumbline/runs/r1/plan.json", agent_type="plumbline:planner"):
    result = run_stop(stop_payload(repo, agent_type, message, agent_id=agent_id), repo)
    assert result.returncode == 0 and result.stdout == "", result.stdout
    return [e for e in ledger(repo) if e["kind"] == "agent" and e["agent_id"] == agent_id]


@pytest.fixture
def planned(repo):
    adopt(repo)
    start_run(repo)
    put(repo, "plan", spec_record(), agent=False)
    return repo


def test_a_resumed_agent_that_stops_with_nothing_changed_is_entered_again_so_that_wait_can_see_it(run_stop, planned):
    assert len(stop(run_stop, planned)) == 1
    assert len(stop(run_stop, planned)) == 1  # the harness asks again for the report it did not get: still one entry, one round
    write_ledger(planned, [resume_row("p-1")])
    assert len(stop(run_stop, planned)) == 2  # resumed, and it left the record as it was: its stop is the answer
    assert len(stop(run_stop, planned)) == 2  # and asked again for its report, it adds no more


def test_wait_sees_the_stop_of_a_resumed_agent_that_changed_nothing(run_cli, run_stop, planned):
    stop(run_stop, planned)
    write_ledger(planned, [resume_row("p-1")])
    assert waits(run_cli, planned, "p-1", "--timeout", "1").returncode == 1
    stop(run_stop, planned)
    assert waits(run_cli, planned, "p-1", "--timeout", "1").returncode == 0


def test_resumed_since_and_stop_after_resume_read_the_order_of_the_ledger():
    rows = [agent_row("a1"), resume_row("a1"), agent_row("a2"), agent_row("a1"), resume_row("a2")]
    assert pl.resumed_since(rows, rows[0]) is True  # a1 was resumed after its first stop
    assert pl.resumed_since(rows, rows[3]) is False  # and it stopped again after that
    assert pl.resumed_since(rows, rows[2]) is True
    assert pl.resumed_since(rows, None) is False and pl.resumed_since(rows, {"kind": "agent"}) is False and pl.resumed_since(rows, dict(rows[0])) is False  # an entry of this ledger
    assert pl.stop_after_resume(rows, "a1", 99) is rows[3] and pl.stop_after_resume(rows, "a2", 99) is None
    assert pl.stop_after_resume([agent_row("a3")], "a3", 0) is not None and pl.stop_after_resume([agent_row("a3")], "a3", 1) is None


# --- gate: an agent that is working again


@pytest.fixture
def runner(monkeypatch):
    stub = CommandRunner()
    monkeypatch.setattr(pl, "run_declared_command", stub)
    return stub


def gate(repo, capsys, stage="verify"):
    code = pl.main(["gate", RUN, stage, "--project", str(repo)])
    return code, capsys.readouterr().out


def stop_of(repo):
    [entry] = [e for e in ledger(repo) if e["kind"] == "agent"]
    return entry


def test_a_gate_that_meets_an_agent_that_was_resumed_and_has_not_stopped_runs_nothing_and_says_to_wait(started, runner, capsys):
    verify_now(started)
    agent_id = stop_of(started)["agent_id"]
    write_ledger(started, [resume_row(agent_id)])  # the record still hashes as it did at the stop: the agent is about to change it, or the files it names
    code, out = gate(started, capsys)
    assert code == 1 and runner.calls == []
    assert f"plumbline:verifier (agent {agent_id}) was resumed after it stopped and has not stopped since, so its record may still change; run `plumbline.py wait {RUN} {agent_id}` first" in out
    assert not [e for e in ledger(started) if e["kind"] == "run"]


def test_once_the_agent_has_stopped_again_the_gate_runs(started, runner, capsys):
    verify_now(started)
    entry = stop_of(started)
    write_ledger(started, [resume_row(entry["agent_id"]), {k: v for k, v in entry.items() if k != "at"}])
    code, _out = gate(started, capsys)
    assert code == 0 and len(runner.calls) == 1


def test_a_resumed_agent_fails_status_and_the_evaluation_pass_makes_because_its_record_is_not_final(started, capsys):
    verify_now(started)
    write_ledger(started, [resume_row(stop_of(started)["agent_id"])])
    assert pl.main(["status", "--run", RUN, "--project", str(started)]) == 0
    out = capsys.readouterr().out
    assert "FAIL" in out and "was resumed after it stopped and has not stopped since" in out
    project = pl.load_project(started)
    outcome = pl.evaluate_stage(project, RUN, next(s for s in project.pipeline["stage"] if s["id"] == "verify"))
    assert not outcome.passed and "has not stopped since" in outcome.problems[0]


def test_a_review_part_whose_agent_was_resumed_is_not_merged_until_it_has_stopped(started):
    project = pl.load_project(started)
    part = put_part(started, "review", "prosecutor-docs", {"lens": "docs", "findings": []})
    entry = stop_of(started)
    write_ledger(started, [resume_row(entry["agent_id"])])
    result = pl.merge_round(project, RUN, "review")
    assert result.record is None
    assert any("was resumed after it stopped and has not stopped since" in problem and part.name in problem for problem in result.problems)
    write_ledger(started, [{k: v for k, v in entry.items() if k != "at"}])
    assert pl.merge_round(project, RUN, "review").problems == []


# --- the README says what the code does


def test_the_readme_documents_wait_the_resume_entry_and_the_send_message_rule():
    from test_readme import rows, section

    commands = {r[0].split("`")[1].split(" ")[0]: r for r in rows(section("The command line", 3))}
    wait = commands["wait"]
    assert wait[0] == "`wait RUN AGENT_ID [--timeout SECONDS]`"
    assert "540 by default, under the 10 minutes the Bash tool allows a command" in wait[1] and "agent X has not stopped after N s; run `wait` again or hand back" in wait[1]
    assert "reads the ledger file every 2 seconds and calls no model" in wait[1] and "an agent that stopped before `wait` began is seen" in wait[1]
    assert "counts the stops after it began" in wait[1] and str(pl.DEFAULT_WAIT_TIMEOUT) in wait[1] and str(pl.WAIT_POLL_SECONDS) in wait[1]
    assert "exits 0; exit 1 when `--timeout` seconds pass first" in wait[1]
    gate = commands["gate"][1]
    assert "once the record is traced to its agent" in gate and "fails the gate at once, and nothing is run" in gate
    ledger_rows = {r[0].strip("`"): r for r in rows(section("Runs, gates and the pass record").split("**The ledger**", 1)[1].split("**Provenance.**", 1)[0])}
    assert ledger_rows["resume"][1] == "the PreToolUse hook, on the orchestrator's SendMessage" and "`agent_id`" in ledger_rows["resume"][2]
    text = section("Runs, gates and the pass record")
    assert "(a `resume` entry follows it) has not delivered its record yet" in text and "was resumed after it stopped and has not stopped since" in text
    assert "fails the gate at once, and nothing is run; so does an agent that was resumed and has not stopped since" in gate
    hooks = section("Hooks")
    assert "**On SendMessage, for `plumbline:orchestrator`:**" in hooks and "that is to a stage agent that has stopped in the run" in hooks
    assert "`{kind: \"resume\", agent_id, at}`" in hooks and "The main session's SendMessage is its own" in hooks and "makes no entry" in hooks
    assert "`check-record`, `open` and `wait`" in section("The pipeline file")
    flow = text.split("**The run flow: the main session, the orchestrator's legs, the stage agents.**", 1)[1].split("\n\n", 1)[0]
    assert "after a SendMessage it runs `wait` for that agent before any gate" in flow and "no shell variable, function or `sleep`, which the hook refuses" in flow
    assert "states item by item whether that decision is fully met, and recommends shipping only when every item is" in flow


def test_the_limit_the_readme_lists_for_wait_is_real_today(started, monkeypatch):
    from test_readme import section

    limits = section("Limits")
    assert "- **`wait` depends on the hook's `resume` entry.**" in limits and "so `wait` counts only the stops after it began: an agent that stopped before `wait` started is not seen, and `wait` times out." in limits
    write_ledger(started, [agent_row("a1")])  # it stopped, and the message that woke it left no resume entry
    assert pl.main(["wait", RUN, "a1", "--timeout", "0", "--project", str(started)]) == 1
