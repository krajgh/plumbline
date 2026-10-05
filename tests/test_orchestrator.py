"""The orchestrator agent: it runs the stages of a run for the main session. Its prompt holds the stage procedure, `plan --run` gives it the
plan without the main session pasting it in, and `status` tells it the rounds each stage has used."""
import json
import re

import pytest

import plumbline as pl
from helpers import PROHIBITION, REPO, commit_all, git, numbered, prose_lines, write
from rundata import RUN, adopt_base, build_note_record, change_of, put, review_record, run_path, spec_record, verify_record, write_test_file, written_tests_record
from test_agents import AGENTS, agent
from test_manifests import between, frontmatter


def orchestrator():
    return agent("orchestrator")


# --- the agent file


def test_the_orchestrator_is_an_agent_file_that_the_run_skill_starts_and_nobody_starts_by_hand():
    assert (AGENTS / "orchestrator.md").is_file()
    fields, body = orchestrator()
    assert fields["name"] == "orchestrator"  # the agent type is `plumbline:orchestrator`
    assert fields["description"].endswith("Started by /plumbline:run, not by hand.")
    assert "until every gate has passed or a decision needs the builder" in fields["description"]
    assert ": " not in fields["description"], "a colon and space in a plain YAML scalar breaks the frontmatter"
    assert body.strip()


def test_the_orchestrator_is_pinned_to_sonnet_at_medium_effort_with_a_generous_turn_limit():
    fields, _ = orchestrator()
    assert set(fields) == {"name", "description", "model", "effort", "tools", "maxTurns"}
    assert fields["model"] == "sonnet" and fields["effort"] == "medium"
    assert re.fullmatch(r"[1-9][0-9]*", fields["maxTurns"]) and int(fields["maxTurns"]) >= 150  # a run is many launches and gates: it is not split into stages


def test_the_orchestrators_tools_launch_agents_message_them_run_commands_and_read_and_nothing_that_writes():
    tools = [t.strip() for t in orchestrator()[0]["tools"].split(",")]
    assert tools == ["Agent", "SendMessage", "Bash", "Read", "Grep", "Glob"]
    for writer in ("Edit", "Write", "NotebookEdit", "PowerShell", "Monitor", "Skill", "WebFetch", "WebSearch", "Workflow"):
        assert writer not in tools


def test_the_prompt_states_rules_as_what_to_do_and_carries_no_private_name_or_path():
    fields, body = orchestrator()
    found = [m.group(0) for line in [fields["description"], *prose_lines(body)] for m in PROHIBITION.finditer(line)]
    assert found == [], found
    assert "/ho" + "me/" not in (AGENTS / "orchestrator.md").read_text(encoding="utf-8")


def test_the_prompt_names_the_commands_it_runs_and_they_are_the_cli_s():
    _, body = orchestrator()
    commands = next(a for a in pl.build_parser()._actions if getattr(a, "choices", None)).choices
    named = {m.group(1) for m in re.finditer(r"PLUMBLINE ([a-z][a-z-]+)", body)}
    assert named == {"status", "plan", "check-diff", "gate", "merge-review"}  # the commands it spells out (tokens, check-record and open are its to run, not its procedure)
    assert named <= set(commands)
    assert "`PLUMBLINE` below stands for `python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py\"`" in body


def test_the_prompt_never_asks_for_a_command_that_starts_a_run_records_a_pass_or_an_override():
    _, body = orchestrator()
    for command in ("plan --intent", "PLUMBLINE pass", "PLUMBLINE override", "PLUMBLINE init", "--calibrate"):
        assert command not in body, command


# --- the turn discipline and where the run stands


def test_the_prompt_writes_the_turn_discipline_in():
    body = orchestrator()[1]
    discipline = between(body, "## Turn discipline", "## Where the run stands")
    assert "Launch the agents of one step together: all in ONE message, each with `run_in_background: false`, so that their reports arrive in one turn." in discipline
    assert "Put the plumbline commands that are independent of each other's output in ONE Bash call, one command to a line." in discipline
    assert "Write no narration." in discipline
    assert "Brief each agent with paths and hashes. It reads the files itself, so your briefs carry no file content." in discipline
    assert "run_in_background: true" not in body and "`run_in_background: false`" in body


def test_a_leg_starts_from_the_run_id_and_reads_where_the_run_stands_in_one_call():
    body = orchestrator()[1]
    assert "The run id. After a hand-back that needed the builder, it also gives the builder's decision" in between(body, "## What your brief gives you", "## Your tools")
    start = between(body, "## Where the run stands", "- A plan with no `reduce` stage")
    block = start.split("```\n")[1]
    assert block.splitlines() == ["PLUMBLINE status --run <run id>", "PLUMBLINE plan --run <run id> --json", "PLUMBLINE check-diff --run <run id>"]
    assert "Take the stages in order, from the first one whose state is not `pass`, `supplied` or `recorded`." in start
    for key in ("`stages`", "`role` or `kind`", "`reads` as paths", "record `path`", "`gate`", "`on_fail`", "`max_rounds`", "`lenses`", "`request` file", "`record_dir`", "`stubs_dir`", "`commands`", "`supplied`"):
        assert key in start, key  # what the plan gives it, so that the main session pastes nothing in
    assert "`status` shows the state of each stage, and the rounds each has used" in start
    assert "launch only the agents whose record is missing" in start


def test_every_key_the_prompt_says_the_plan_gives_is_in_the_plan():
    keys = {"stages", "request", "record_dir", "stubs_dir", "commands", "supplied"}
    plan = pl.build_plan(pl.load_toml(pl.PIPELINE_DIR / "default.toml"), {**intake(), "row": "code.M"}, "r1")
    assert keys <= set(plan)
    stage = plan["stages"][1]
    assert {"role", "kind", "reads", "path", "gate", "on_fail", "max_rounds", "lenses"} <= set(stage)


def intake():
    from samples import sample

    return sample("change_class")


def test_the_prompt_says_what_ends_a_leg_and_how_the_report_reads():
    body = orchestrator()[1]
    ending = between(body, "## Hand back", "## Finish")
    for case in (
        "**Every gate of the row has passed.**", "**A decision only the builder can make comes up.**", "**An error you are unable to resolve:**",
        "A hook refuses a call that you would have to work around", "The change measures larger than the row, or at size L.",
        "A stage has reached `max_rounds` (`gate` exits 3), or its `on_fail` is `main`.", "Non-blocking findings or gaps are left once every gate has passed: fix or ship.",
    ):
        assert case in ending, case
    report = ending.split("Your report is at most 15 lines:", 1)[1].split("```\n")[1]
    fields = [line.split(":", 1)[0] for line in report.splitlines()]
    assert fields == ["run <run id>", "stopped", "options", "rounds", "open"]  # the run id, where it stopped and why, the options with its recommendation, the rounds, the open findings
    assert "mark your recommendation" in report and "one line each" in report
    assert len(report.splitlines()) <= 15


def test_a_hand_back_that_returns_with_a_decision_is_acted_on_first():
    body = orchestrator()[1]
    decision = between(body, "## A decision comes back", "## Hand back")
    assert "When your brief gives the builder's decision, act on it first." in decision
    assert "**Fix the open findings**" in decision and "**Ship:** hand back with every gate passed." in decision
    assert "`gate` opened no new round" in decision and "names it; run that agent again" in decision


# --- the stage procedure, moved out of the run skill


def test_the_prompt_launches_every_plumbline_agent_without_a_model_because_each_is_pinned():
    body = orchestrator()[1]
    stage = between(body, "## An agent stage", "Every brief gives")
    assert "without `model` and without `isolation`" in stage
    assert "Sonnet for the planner, test-writer, builder, prosecutor, detective and canary; Haiku for the verifier and defender" in stage
    pins = {name: frontmatter(AGENTS / f"{name}.md")[0]["model"] for name in ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")}
    assert sorted(n for n, m in pins.items() if m == "sonnet") == sorted(["planner", "test-writer", "builder", "prosecutor", "detective", "canary"])
    assert sorted(n for n, m in pins.items() if m == "haiku") == ["defender", "verifier"]
    assert "model:" not in body  # no launch in the prompt names a model
    assert "(without `model`: the defender is pinned to Haiku)" in between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    assert "tests/_stubs" not in body


def test_the_prompt_runs_the_two_measured_gates_with_the_largest_timeout():
    stage = between(orchestrator()[1], "## An agent stage", "Every brief gives")
    assert "run those two gates with the Bash tool's `timeout` set to 600000 (its largest)" in stage
    assert "leave the files as the agent left them between its stop and the gate" in stage
    assert "`timeout` under `[commands]` in `plumbline.toml` (900 seconds by default)" in stage


def test_each_stage_agent_is_briefed_with_what_it_reads_and_the_builder_is_briefed_with_text_only():
    body = orchestrator()[1]
    briefs = between(body, "Every brief gives", "An agent that comes back marked partial")
    assert "the run id, the path of the request, the path where the agent writes its record (the stage's `path`), the paths of the records it reads, and the schema of its record, `${CLAUDE_PLUGIN_ROOT}/schemas/<record>.json`" in briefs
    builder = between(briefs, "- builder:", "- verifier:")
    assert "verbatim" in builder and "no test file, test name, assertion or tests-lens finding, and no path of a review file" in builder
    test_writer = between(briefs, "- test-writer:", "- builder:")
    for needed in (
        "the plan's `stubs_dir`", "the tests run against today's code, with the new names imported inside the test functions",
        "Stubs are for brand-new modules only: they go in `stubs_dir`, outside the change and hidden from the builder",
        '(for pytest, the test command followed by `-o pythonpath="<stubs_dir> ."`)', "Its `files_written` lists the stubs too",
    ):
        assert needed in test_writer, needed
    verifier = briefs[briefs.index("- verifier:") :]
    assert "it runs `check-diff --run <run id>`" in verifier and "hand back" in verifier
    planner = between(briefs, "- planner:", "- test-writer:")
    assert "printed under \"for the planner\", verbatim" in planner


def test_the_review_units_launch_each_step_in_one_message_in_the_foreground():
    review = between(orchestrator()[1], "## Review units", "## When a gate fails")
    assert "1. **Prosecutors.** Launch one `plumbline:prosecutor` per lens of the stage, all in one message, in the foreground." in review
    assert "Launch `defenders` `plumbline:defender` agents in one message, in the foreground" in review
    assert "launch `plumbline:detective` (foreground)" in review
    assert "run_in_background" not in review  # the foreground is the default the discipline names, so the steps say it in words
    assert "`merge-review` with no `--round` merges the highest round" in review and "`gate` creates `round-<n+1>/` itself" in review
    assert "Every brief of the round carries both, and every agent copies the `diff_sha256` into its record." in review


def test_the_review_units_carry_the_calibration_canary_after_the_prosecutors_and_the_screening_and_panel_steps():
    review = between(orchestrator()[1], "## Review units", "## When a gate fails")
    assert "a round whose stage has `defenders` also gets the canary, once the prosecutors have reported" in review
    assert "writes its record `prosecutor-<lens>-b.json` and its key `canary-key.json` in the round directory" in review
    assert "say nothing of a canary (the launch hook holds a defender's brief to that" in review
    assert "The canary is a finding to answer, so launch the defenders the round's rule names even when no prosecutor filed a finding." in review
    for needed in ("**No BLOCKING finding, and the stage has `screen_defenders`:**", "`screen-1`", "**No BLOCKING finding, and `screen_defenders` is 0:** no defender runs", "it says \"panel needed\"", "`panel_needed`", "in this round and not in the next"):
        assert needed in review, needed
    assert "Grep the round directory for `\"severity\"\\s*:\\s*\"BLOCKING\"`" in review  # how it tells a BLOCKING finding without reading the findings records one by one
    assert "In a calibration run it also prints `canary: refuted by 2 of 3 defenders` (or the like): keep that line for your report." in review


def test_the_failure_routes_are_the_skills_with_hand_backs_where_the_main_session_decides():
    failing = between(orchestrator()[1], "## When a gate fails", "## A decision comes back")
    assert '"round k of N"' in failing and "exits 1 while the stage has rounds left" in failing
    assert "`gate` exits 3 when the stage has used its rounds. When it exits 3, or when `on_fail` is `main`, hand back" in failing
    assert "`plan` and `tests` go back to their own agent" in failing and "`verify` and `review` go back to `build`" in failing
    assert "`spec-review` goes back to `plan`" in failing and "the failure goes to the main session (`on_fail` is `main`)" in failing
    assert "A review that fails with \"the screening defender claims finding X is BLOCKING\" goes back to no stage" in failing
    assert "The surviving findings under \"for the test-writer\" go to the test-writer" in failing
    assert "Send a stage back by resuming its agent with SendMessage" in failing and "either is a new attempt under the rounds rule" in failing
    assert "starts with where it comes from: `the agent's record` (what the agent typed) or `the measured run`" in failing
    assert "`no test command is declared`" in failing and "so hand back" in failing
    assert pl._typed(["x"]) == ["the agent's record: x"] and pl._measured(["x"]) == ["the measured run: x"]


def test_a_partial_agent_is_resumed_once_and_then_the_leg_hands_back():
    assert "resume it once with SendMessage, and hand back as an error when it comes back partial again" in orchestrator()[1]


def test_the_prompt_says_where_the_report_goes_when_the_harness_asks_for_it_through_subagent_handback():
    finish = orchestrator()[1].split("## Finish", 1)[1]
    assert "When the harness asks for your report through SubagentHandback, the whole report goes in that call's `message`. If that call is refused, the same report is your final message." in finish
    assert "RECORD" not in finish  # it writes no record


# --- plan --run


@pytest.fixture
def adopted_code_m(repo):
    """An adopted repository with a 100-line change: row code.M."""
    adopt_base(repo)
    write(repo / "src" / "new_module.py", numbered(100))
    return repo


def started(run_cli, repo, *args, run_id=RUN):
    result = run_cli("plan", "--base", "main", "--run-id", run_id, "--intent", *args, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def plan_run(run_cli, repo, *args, run_id=RUN):
    return run_cli("plan", "--run", run_id, *args, cwd=repo)


def fix_spec_file(repo):
    record = spec_record(planned=("AC-1",))
    record["acceptance_criteria"] = record["acceptance_criteria"][:1]
    name = ".plumbline/supplied-spec.json"
    write(repo / name, json.dumps(record))
    return name


def request_file(repo, text="Retry the fetch three times.\n"):
    name = ".plumbline/request.md"
    write(repo / name, text)
    return name


def test_plan_run_prints_the_plan_plan_intent_printed_when_the_run_began(run_cli, adopted_code_m):
    printed = started(run_cli, adopted_code_m, "feature", "--request-file", request_file(adopted_code_m))
    result = plan_run(run_cli, adopted_code_m)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == printed
    assert printed["request"] == ".plumbline/runs/r1/request.md" and printed["record_dir"] == ".plumbline/runs/r1" and printed["stubs_dir"] == ".plumbline/runs/r1/stubs"
    assert [s["id"] for s in printed["stages"]][:3] == ["intake", "plan", "spec-review"] and result.stderr == ""


def test_plan_run_with_json_says_what_it_prints_and_prints_the_same(run_cli, adopted_code_m):
    started(run_cli, adopted_code_m, "feature")
    assert plan_run(run_cli, adopted_code_m, "--json").stdout == plan_run(run_cli, adopted_code_m).stdout


def test_plan_run_names_where_a_supplied_record_came_from_as_the_start_did(run_cli, adopted_code_m):
    printed = started(run_cli, adopted_code_m, "fix", "--spec", fix_spec_file(adopted_code_m))
    assert printed["supplied"] == [{"stage": "plan", "record": "spec", "path": ".plumbline/runs/r1/plan.json", "source": ".plumbline/supplied-spec.json"}]
    assert json.loads(plan_run(run_cli, adopted_code_m).stdout) == printed
    assert next(s for s in printed["stages"] if s["id"] == "tests")["gate"] == "reproduces_on_head"  # the intent's gate is in it


def test_plan_run_of_a_run_without_a_stored_request_has_none_and_of_a_calibration_run_says_so(run_cli, adopted_code_m):
    printed = started(run_cli, adopted_code_m, "feature", "--calibrate")
    again = json.loads(plan_run(run_cli, adopted_code_m).stdout)
    assert again == printed and again["request"] is None and again["calibrate"] is True


def test_plan_run_carries_the_repositorys_commands_and_graft_setting(run_cli, repo):
    adopt_base(repo, commands={"test": "python3 -m pytest", "lint": "ruff check"})
    write(repo / "src" / "new_module.py", numbered(100))
    printed = started(run_cli, repo, "feature")
    assert printed["commands"] == {"test": ["python3 -m pytest"], "lint": ["ruff check"]} and printed["graft"] is False
    assert json.loads(plan_run(run_cli, repo).stdout) == printed


def test_plan_run_writes_nothing(run_cli, adopted_code_m):
    started(run_cli, adopted_code_m, "feature")
    files = lambda: sorted((p.relative_to(adopted_code_m).as_posix(), p.read_bytes()) for p in (adopted_code_m / ".plumbline").rglob("*") if p.is_file())
    before, status_before, head = files(), git(adopted_code_m, "status", "--porcelain"), git(adopted_code_m, "rev-parse", "HEAD")
    assert plan_run(run_cli, adopted_code_m).returncode == 0
    assert (files(), git(adopted_code_m, "status", "--porcelain"), git(adopted_code_m, "rev-parse", "HEAD")) == (before, status_before, head)


def test_plan_run_works_from_another_directory_through_project(run_cli, adopted_code_m, tmp_path):
    started(run_cli, adopted_code_m, "feature")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = run_cli("plan", "--run", RUN, "--project", str(adopted_code_m), cwd=elsewhere)
    assert result.returncode == 0 and json.loads(result.stdout)["run_id"] == RUN


@pytest.mark.parametrize(
    "extra,option",
    [
        (["--intent", "feature"], "--intent"), (["--spec", "x.json"], "--spec"), (["--request-file", "x.md"], "--request-file"), (["--calibrate"], "--calibrate"),
        (["--row", "code.M"], "--row"), (["--base", "main"], "--base"), (["--run-id", "r2"], "--run-id"),
    ],
)
def test_plan_run_takes_no_option_that_starts_a_run(run_cli, adopted_code_m, extra, option):
    started(run_cli, adopted_code_m, "feature")
    result = plan_run(run_cli, adopted_code_m, *extra)
    assert result.returncode == 2 and f"--run prints the plan of a run that has begun, so {option} does not go with it (it starts a run)" in result.stderr
    assert result.stdout == "" and not run_path(adopted_code_m, "r2").exists()


def test_json_without_run_is_refused_and_writes_nothing(run_cli, adopted_code_m):
    result = run_cli("plan", "--json", "--intent", "feature", "--run-id", "r1", cwd=adopted_code_m)
    assert result.returncode == 2 and "--json goes with --run" in result.stderr
    assert not (adopted_code_m / ".plumbline").exists()


def test_plan_run_of_a_run_that_does_not_exist_is_a_usage_error(run_cli, adopted_code_m):
    result = plan_run(run_cli, adopted_code_m, run_id="nope")
    assert result.returncode == 2 and "there is no run 'nope'" in result.stderr and result.stdout == ""


@pytest.mark.parametrize("run_id", ["ACTIVE", "../x", "-r", ""])
def test_plan_run_refuses_what_is_no_run_id(run_cli, adopted_code_m, run_id):
    started(run_cli, adopted_code_m, "feature")
    assert plan_run(run_cli, adopted_code_m, run_id=run_id).returncode == 2


def test_plan_run_refuses_a_run_whose_intake_record_was_edited_since_it_began(run_cli, adopted_code_m):
    started(run_cli, adopted_code_m, "feature")
    intake_file = run_path(adopted_code_m, RUN, "intake.json")
    record = json.loads(intake_file.read_text(encoding="utf-8"))
    record["row"] = "docs"
    intake_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    result = plan_run(run_cli, adopted_code_m)
    assert result.returncode == 2 and "the run's intake record changed after `plan --intent` wrote it" in result.stderr and result.stdout == ""


def test_plan_run_of_a_directory_that_is_no_repository_is_a_usage_error(run_cli, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run_cli("plan", "--run", RUN, cwd=plain, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert result.returncode == 2 and result.stdout == ""


def test_plan_run_is_in_the_parser_with_json_beside_it():
    plan = next(a for a in pl.build_parser()._actions if getattr(a, "choices", None)).choices["plan"]
    options = {o for action in plan._actions for o in action.option_strings}
    assert {"--run", "--json"} <= options


# --- status: the rounds each stage has used


def rounds_line(run_cli, repo):
    out = run_cli("status", "--run", RUN, cwd=repo).stdout.splitlines()
    return next((line for line in out if line.startswith("rounds:")), None)


def test_status_has_no_rounds_line_before_a_stage_has_run(run_cli, adopted_code_m):
    started(run_cli, adopted_code_m, "feature")
    assert rounds_line(run_cli, adopted_code_m) is None


def test_status_counts_the_rounds_of_each_stage_that_has_run_in_the_order_they_run(run_cli, repo):
    adopt_base(repo)
    write_test_file(repo)
    commit_all(repo, "the tests")
    started(run_cli, repo, "feature", "--row", "code.S")
    diff = change_of(repo)
    put(repo, "plan", spec_record())
    assert rounds_line(run_cli, repo) == "rounds: plan 1"
    put(repo, "tests", written_tests_record())
    put(repo, "tests", {**written_tests_record(), "files_written": ["tests/test_app.py", "tests/extra.py"]})  # a second agent before the gate is a round of its own
    put(repo, "build", build_note_record())
    put(repo, "verify", verify_record(diff=diff))
    put(repo, "review", review_record(diff=diff, round_no=2), agent=True)
    assert rounds_line(run_cli, repo) == "rounds: plan 1, tests 2, build 1, verify 1, review 2"


def test_status_leaves_the_main_sessions_stages_and_a_supplied_stage_out_of_the_rounds(run_cli, adopted_code_m):
    started(run_cli, adopted_code_m, "fix", "--spec", fix_spec_file(adopted_code_m))
    put(adopted_code_m, "build", build_note_record())
    assert rounds_line(run_cli, adopted_code_m) == "rounds: build 1"  # not intake, reduce or the plan the intent supplied


def test_the_readme_commands_table_lists_the_plan_run_and_the_status_rounds():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| `plan "))
    assert "[--run RUN [--json]]" in row and "rebuilt from its own files and writing nothing" in row
    status = next(line for line in readme.splitlines() if line.startswith("| `status "))
    assert "the rounds each stage that has run took (`rounds: plan 1, tests 2, review 1`)" in status


# --- the run skill: the main session's part only


def skill():
    return frontmatter(REPO / "skills" / "run" / "SKILL.md")


def test_the_run_skill_holds_the_main_sessions_part_and_is_much_shorter_than_the_whole_procedure_was():
    fields, body = skill()
    text = (REPO / "skills" / "run" / "SKILL.md").read_text(encoding="utf-8")
    assert len(text) < 12000  # 0.5.0's skill before the split was about 21,000 characters
    headings = re.findall(r"^## (.+)$", body, re.M)
    assert headings == [
        "0. Before you start", "1. Intake", "2. Start the run", "3. Run a leg", "4. A report that needs the builder", "5. When every gate has passed",
        "6. Push", "7. A follow-up from the open findings", "If the orchestrator is not available",
    ]
    assert "orchestrator agent" in fields["description"] and "(plan, tests, build, verify, review)" in fields["description"]
    for moved in ("plumbline:prosecutor", "plumbline:defender", "plumbline:detective", "plumbline:builder", "plumbline:planner", "PLUMBLINE gate", "PLUMBLINE merge-review", "PLUMBLINE check-diff", "round k of N", "stubs_dir"):
        assert moved not in body, moved  # the stage procedure is the orchestrator's


def test_every_section_the_run_skill_points_to_is_there():
    body = skill()[1]
    sections = set(re.findall(r"^## (\d+)\.", body, re.M))
    pointed = set(re.findall(r"\(section (\d+)\)|, section (\d+)\)|sections (\d+) and (\d+)", body))
    numbers = {n for group in pointed for n in group if n}
    assert numbers and numbers <= sections, (numbers, sections)


def test_intake_infers_and_confirms_the_intent_and_the_row_and_the_plan_command_starts_the_run():
    body = skill()[1]
    intake = between(body, "## 1. Intake", "## 2. Start the run")
    assert "Settle the intent and the row before anything is planned, state both in a sentence each, and let the builder confirm or correct them in one question." in intake
    for intent in pl.intent_ids(pl.load_toml(pl.PIPELINE_DIR / "default.toml")):
        assert f"| `{intent}` |" in intake, intent
    assert "`--row code.M` for a code change of size M" in intake and "A change that measures as size L ends before reduce" in intake
    start = between(body, "## 2. Start the run", "## 3. Run a leg")
    assert "PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>] --request-file <file> [--calibrate]" in start
    assert "Keep the `run_id` it prints: the orchestrator reads the rest of the plan itself." in start and "If the command refuses, tell the builder why, and stop." in start


def test_a_leg_is_started_in_the_background_with_the_run_id_and_waited_for_without_polling():
    leg = between(skill()[1], "## 3. Run a leg", "## 4. A report that needs the builder")
    assert "Launch the agent `plumbline:orchestrator` with the Agent tool, `run_in_background: true`, without `model` (it is pinned to Sonnet) and without `isolation`." in leg
    assert "Its brief is the run id and, after a report that needed the builder, the builder's decision in a sentence or two." in leg
    assert "Then wait for its report, without polling and without commands of your own: the completion arrives as one notification, which is one turn of yours." in leg
    assert "The report is at most 15 lines" in leg and "the open findings as ids with one line each" in leg
    assert "paste" not in leg.lower()  # the plan is read by the orchestrator, not handed to it


def test_a_report_that_needs_the_builder_is_put_with_ask_user_question_and_a_new_leg_follows_the_answer():
    asking = between(skill()[1], "## 4. A report that needs the builder", "## 5. When every gate has passed")
    for case in ("a hook refused a call that the orchestrator would have had to work around", "the change measures larger than its row or at size L", "a stage reached `max_rounds`", "non-blocking findings or gaps are left once every gate passed (fix or ship)", "an error stays unresolved"):
        assert case in asking, case
    assert "Ask with AskUserQuestion: give the options as the report lists them, with the orchestrator's recommendation first." in asking
    assert "start the next leg (section 3) with the builder's decision in its brief" in asking and "To ship, go on to section 5." in asking
    assert "`/plumbline:override` is the builder's command, typed by the builder" in asking and "override --reason" not in asking


def test_when_every_gate_has_passed_the_main_session_commits_records_the_pass_and_reports_the_cost_of_the_legs_apart():
    reduce = between(skill()[1], "## 5. When every gate has passed", "## 6. Push")
    assert "Commit it yourself" in reduce and "`PLUMBLINE pass <run_id>`" in reduce and "then start a leg again, which runs from `verify`" in reduce
    assert "`PLUMBLINE status --run <run_id>` (it ends with the rounds each stage took)" in reduce
    assert "`by_model` is what the stage agents used and `orchestration` what the orchestrator's legs used, which sat in your own session before" in reduce
    assert "`output_lower_bound` is above 0 is at least that" in reduce and "`PLUMBLINE open`" in reduce and "what the canary measured" in reduce


def test_the_push_is_the_builders_and_the_skill_points_to_the_procedure_when_the_agent_is_not_there():
    body = skill()[1]
    assert "Leave the push to the builder, until the builder asks you to push." in between(body, "## 6. Push", "## 7. A follow-up")
    fallback = body.split("## If the orchestrator is not available", 1)[1]
    assert "When the Agent tool has no `plumbline:orchestrator`, follow the procedure in `${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.md` yourself, in this session" in fallback
    assert "put to the builder what it would hand back" in fallback
    assert (REPO / "agents" / "orchestrator.md").is_file()


def test_the_skills_launch_and_the_hooks_launch_pins_agree_on_the_orchestrator():
    import pre_tool_use as pre

    assert pre.pinned_model(pl, "orchestrator") == "sonnet" == frontmatter(AGENTS / "orchestrator.md")[0]["model"]
    assert "(it is pinned to Sonnet)" in skill()[1]
