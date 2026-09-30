"""The README says what the plugin has and what it does, and the tests below check that against the code by running it: a name in the README
is one the code has, a number is the code's own, a field a table lists is one the ledger or a schema holds, a rule it states is one the hooks
apply, and a limit it lists is a limit today (a test that finds one closed says which line of the README to take out)."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import plumbline as pl
import pre_tool_use as pre
import session_start
import subagent_stop
from helpers import CLI, DEFAULT_TOML, REPO, clean_env, commit_all, git, write
from hookdata import add_origin, bash_payload, start_run, stop_payload, tool_payload
from rundata import CONTROLLED, RUN, adopt, adopt_base, build_note_record, genuine_pass, put, put_part, spec_record, verify_record

README = (REPO / "README.md").read_text(encoding="utf-8")
HOOKS = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
MATCHER = HOOKS["PreToolUse"][0]["matcher"]
REGISTERED = HOOKS["PreToolUse"][0]["hooks"][0]["command"]
PIPELINE = pl.load_toml(DEFAULT_TOML)


def section(title: str, level: int = 2) -> str:
    """The text under a heading, up to the next heading of the same or a higher level."""
    marks = "#" * level
    match = re.search(rf"^{marks} {re.escape(title)}\n(.*?)(?=^#{{1,{level}}} |\Z)", README, re.M | re.S)
    assert match, f"the README has no section '{title}'"
    return match.group(1)


def rows(text: str) -> list[list[str]]:
    """The cells of each row of the markdown tables in `text`, without the header row and the rule under it."""
    found = []
    for line in text.splitlines():
        if line.startswith("|") and not re.fullmatch(r"\|[ :|-]+\|", line):
            found.append([cell.strip() for cell in line.strip().strip("|").split(" | ")])
    return found[1:]


def ticks(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


# ------------------------------------------------------------------------------------------------ what the plugin has


def command_parsers():
    return next(a for a in pl.build_parser()._actions if getattr(a, "choices", None)).choices


def test_every_command_of_the_cli_is_in_the_commands_table_with_every_option_and_argument_it_has():
    table = {}
    for line in README.split("| Command | What it does |", 1)[1].split("\n\n", 1)[0].splitlines():
        match = re.match(r"\| `([a-z-]+)((?: [^`]*)?)` \| ", line)
        if match:
            table[match.group(1)] = match.group(1) + match.group(2)
    assert set(table) == set(command_parsers())
    for name, sub in command_parsers().items():
        for action in sub._actions:
            if action.option_strings == ["-h", "--help"] or action.option_strings == ["--project"]:
                continue  # every command but two takes --project: the README says so once, below the table
            token = action.option_strings[-1] if action.option_strings else (action.metavar or action.dest).upper()
            assert token in table[name], (name, token)
    without_project = {name for name, sub in command_parsers().items() if "--project" not in [o for a in sub._actions for o in a.option_strings]}
    assert without_project == {"check-record", "render"}
    assert "Every command except `check-record` and `render` takes `--project PATH`" in README


def test_the_readme_says_the_cli_takes_no_abbreviations_and_it_takes_none():
    assert "the command line takes no abbreviation of an option, so `--rea` is an error and never `--reason`" in README
    assert pl.build_parser().allow_abbrev is False and all(sub.allow_abbrev is False for sub in command_parsers().values())


def test_every_agent_and_skill_is_named():
    for role in pl.AGENT_ROLES:
        assert f"`plumbline:{role}`" in README, role
    for skill in ("run", "override", "status", "init", "subagent-discipline"):
        assert f"/plumbline:{skill}" in README, skill


def frontmatter_of(path: Path) -> dict:
    head = path.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    return dict(line.split(": ", 1) for line in head.splitlines()[1:] if ": " in line)


def test_the_agents_table_gives_the_model_the_tools_and_the_record_of_each_agent_file():
    table = {r[0].strip("`"): r for r in rows(section("Agents"))}
    assert set(table) == {f"plumbline:{role}" for role in pl.AGENT_ROLES}
    for role in pl.AGENT_ROLES:
        fields = frontmatter_of(REPO / "agents" / f"{role}.md")
        _name, model, tools, _writes, record = table[f"plumbline:{role}"]
        assert model.lower() == fields["model"], role
        assert [t.strip() for t in re.sub(r"\(.*\)", "", tools).split(",") if t.strip()] == [t.strip() for t in fields["tools"].split(",")], role
        assert ("no Bash" in tools) == ("Bash" not in fields["tools"]), role
        assert record.strip("`") == pl.AGENT_RECORDS[role], role


def schema_names(record: str) -> set[str]:
    """Every property name in a record's schema, nested ones included."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            found.update(node.get("properties", {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(pl.load_schema(record))
    return found


def test_every_record_type_is_in_the_records_table_and_the_fields_it_names_are_the_schemas():
    table = {r[0].strip("`"): r for r in rows(section("Records"))}
    assert set(table) == set(pl.record_types())
    for name in pl.record_types():
        for token in ticks(table[name][2]):
            assert token in schema_names(name), (name, token)
        if "diff_sha256" in pl.load_schema(name)["required"]:
            assert "diff_sha256" in table[name][2], name  # every record that covers a change says so


def test_every_intent_and_gate_is_named():
    for intent in pl.intent_ids(PIPELINE):
        assert f"`{intent}`" in README, intent
    for gate in pl.KNOWN_GATES:
        assert gate in README, gate


def test_the_readmes_default_rounds_and_gates_are_the_default_pipelines():
    sentence = re.search(r"In the default pipeline (.*?)\n", README).group(1)
    stated = {stage: int(count) for stage, count in re.findall(r"`([a-z-]+)` (?:has )?(\d+)", sentence.split(". The gate")[0])}
    stages = {s["id"]: s for s in PIPELINE["stage"]}
    assert stated == {sid: s["max_rounds"] for sid, s in stages.items() if "max_rounds" in s}
    assert "The gate of `plan` is `spec_complete`, the gate of `tests` is `tests_fail_on_stub`" in sentence
    assert (stages["plan"]["gate"], stages["tests"]["gate"]) == ("spec_complete", "tests_fail_on_stub")
    assert (stages["plan"]["on_fail"], stages["tests"]["on_fail"]) == ("plan", "tests")  # both run their own agent again


def test_the_intents_lenses_join_the_rows_and_the_readme_says_so():
    assert "add lenses to the review of the diff (`lenses`, joined to the row's own)" in README
    assert pl.effective_row(PIPELINE, "code.S", "refactor").lenses == ["correctness", "tests", "boundaries"]
    assert pl.effective_row(PIPELINE, "config", "refactor").lenses == ["security", "boundaries", "correctness"]


def test_the_one_time_setups_are_documented():
    assert "PONYTAIL_SUBAGENT_MATCHER" in README and "SubagentStart" in README and "hooks/ponytail-subagent.js" in README
    assert "[commands]" in README and "pre_tool_use.sh" in README and "subagent_stop.sh" in README


def test_the_matcher_the_readme_recommends_reaches_exactly_the_making_agents_and_spares_other_subagents():
    value = re.search(r'"PONYTAIL_SUBAGENT_MATCHER": "([^"]+)"', README).group(1)
    assert value == session_start.MATCHER_EXAMPLE == "^(?!plumbline:)|^plumbline:(planner|test-writer|builder)$"
    assert session_start.matcher_reach(value) == (True, ["planner", "test-writer", "builder"])
    for other in ("probe:echo", "general-purpose", "Explore"):
        assert re.search(value, other, re.I), other  # ponytail stays on for every subagent that is no plumbline agent
    shorter = "^plumbline:(planner|test-writer|builder)$"
    assert f"`{shorter}`" in README and session_start.matcher_reach(shorter) == (True, ["planner", "test-writer", "builder"])
    assert not re.search(shorter, "probe:echo", re.I)  # and the shorter one switches it off for them


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_named_group_is_valid_in_the_node_that_runs_ponytails_hook_and_is_not_in_pythons_spelling():
    assert "(`(?<name>...)` is a group there, and `(?P<name>...)` is an error)" in README
    node = shutil.which("node")
    assert session_start.node_reach(node, "^plumbline:(?<r>planner|test-writer|builder)$") == (True, ["planner", "test-writer", "builder"])
    assert session_start.node_reach(node, "^plumbline:(?P<r>planner)$") == (False, [])


def test_the_status_line_gives_the_manifests_version_and_the_phase():
    status = section("Status").strip()
    version = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert version == "0.4.1" and status.startswith(f"Version {version}.")
    assert "Phase 2, agents and enforcement, is done" in status
    assert "The next phase is phase 3, light testing in a real session" in status
    assert "Phase 2b of 5" not in README


# ------------------------------------------------------------------------------ the commands table, the gates, the ledger


def test_the_commands_table_section_states_the_defaults_and_the_guarded_files_of_the_code():
    text = section("The `[commands]` table", 3)
    assert f"({pl.DEFAULT_COMMAND_TIMEOUT} by default)" in text and pl.DEFAULT_COMMAND_TIMEOUT == 900
    for name in pl.GUARDED_FILES:
        assert f"`{name}`" in text, name  # plumbline.toml is on the list too
    assert "`.git/config` and `.git/hooks/`" in text and "the repository's own pipeline file" in text
    assert "`--junitxml=.plumbline/runs/<run-id>/junit-<stage>.xml`" in text and "Only the `test` command gets this" in text
    assert "under `sh -c`" in text and "Its output is discarded" in text and "SIGTERM, SIGINT or SIGHUP" in text
    assert all(f"`{name}`" in text for name in pl.CONFIG_COMMANDS)


def test_the_readme_says_each_problem_of_a_measured_gate_starts_with_its_source_and_the_code_does_that():
    gates = section("Runs, gates and the pass record")
    assert "Each problem of a measured gate starts with its source, so that a failure says which part failed" in gates
    assert "`the agent's record` (what the agent typed, read against the spec and the files it names) or `the measured run` (what `gate` saw when it ran the commands)" in gates
    assert pl._typed(["x"]) == ["the agent's record: x"] and pl._measured(["x"]) == ["the measured run: x"]


def test_the_readme_says_a_size_problem_names_the_files_that_contribute_most_and_check_diff_does(repo, run_cli):
    text = section("Runs, gates and the pass record")
    assert "names the files that contribute most to the size: up to 5, each with its changed lines, untracked files included" in text
    assert "a problem that names the files contributing most to the size" in README and pl.MAX_NAMED_FILES == 5
    adopt_base(repo)
    start_run(repo)
    write(repo / "src" / "big.py", "x = 1\n" * 450)
    result = run_cli("check-diff", "--run", RUN, cwd=repo)
    assert result.returncode == 1 and "The files that contribute most: src/big.py (450 lines)." in json.loads(result.stdout)["problems"][0]


def test_the_stubs_paragraph_says_where_stubs_go_and_that_the_change_never_holds_them(repo):
    text = section("Runs, gates and the pass record")
    stubs = re.search(r"\*\*Stubs\.\*\*(.*?)\n\n", text, re.S).group(1)
    for phrase in (
        "pytest exits 1, never 2", "under the run's `stubs/` directory", '`-o pythonpath="<stubs dir> ."`', "its `files_written` lists the stubs",
        "the size measure does not count them, the hash of the change leaves them out, and no commit takes them", "The builder does not see the directory", "without the stubs",
    ):
        assert phrase in stubs, phrase
    assert ".plumbline/runs/<run-id>/stubs/" in text and "on the import of a name the change has yet to add" in text
    adopt_base(repo)  # what the paragraph says, done: a stub in the run is no part of the change
    start_run(repo)
    write(repo / "src" / "feature.py", "x = 1\n")
    merge_base = git(repo, "merge-base", "main", "HEAD").strip()
    before = pl.change_hash(repo, merge_base)
    write(repo / ".plumbline" / "runs" / RUN / "stubs" / "newmod.py", "def add(a, b):\n    return 0\n" * 50)
    assert pl.change_hash(repo, merge_base) == before
    assert [f["path"] for f in pl.classify(repo, pl.load_project(repo).pipeline, "main")["files"]] == ["src/feature.py"]


def test_a_measured_command_keeps_that_it_ran_and_not_what_it_printed_and_only_a_pytest_command_is_a_pytest_run(repo):
    result = pl.run_declared_command(repo, "echo assertion text; echo more >&2; exit 3", 5)
    assert set(result) == {"cmd", "exit_code", "seconds"} and result["exit_code"] == 3  # no output travels
    slow = pl.run_declared_command(repo, "sleep 5", 1)
    assert slow["timed_out"] is True and slow["exit_code"] == 124 and slow["seconds"] < 4  # killed at the timeout
    assert pl.is_pytest("python3 -m pytest -q") and pl.is_pytest("uv run pytest") and pl.is_pytest("pytest") and not pl.is_pytest("sh run_tests.sh")


def test_the_exit_statuses_of_gate_and_of_a_pytest_run_are_the_codes_the_readme_gives():
    subcommands = next(a for a in pl.build_parser()._actions if getattr(a, "choices", None))
    helps = {a.dest: a.help for a in subcommands._choices_actions}
    phrase = "exit 0 if it passes, 1 if not, 3 when the stage has used its rounds"
    assert phrase in README and phrase in helps["gate"]
    assert "2 is a collection or usage error, and 5 collected no tests" in README and "any other command fails with a status other than 0, 126 and 127" in README
    source = (REPO / "scripts" / "plumbline.py").read_text(encoding="utf-8")
    assert "if code in (126, 127):" in source and "elif code == 2:" in source and "elif code == 5:" in source and "elif code != 1:" in source


@pytest.fixture
def flow(repo, run_cli, run_stop):
    """A refactor run through the real CLI and the real SubagentStop hook, from `plan --intent` to `pass`: every kind of ledger entry."""
    adopt_base(repo, commands={"test": CONTROLLED})
    write(repo / "src" / "retry.py", "def retry(url):\n    return url\n")

    def cli(*args):
        result = run_cli(*args, cwd=repo)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    cli("plan", "--run-id", RUN, "--intent", "refactor", "--row", "code.S")
    run = repo / ".plumbline" / "runs" / RUN
    write(run / "build.json", json.dumps(build_note_record()))
    stop = run_stop(stop_payload(repo, "plumbline:builder", "built\nRECORD: .plumbline/runs/r1/build.json", agent_id="b-1"), repo)
    assert stop.returncode == 0 and stop.stdout == "", stop.stdout
    digest = json.loads(cli("check-diff", "--run", RUN).stdout)["diff_sha256"]
    write(run / "verify.json", json.dumps(verify_record(diff=digest)))
    stop = run_stop(stop_payload(repo, "plumbline:verifier", "checked\nRECORD: .plumbline/runs/r1/verify.json", agent_id="v-1"), repo)
    assert stop.returncode == 0 and stop.stdout == "", stop.stdout
    cli("gate", RUN, "verify")
    for lens in ("correctness", "tests", "boundaries"):  # the refactor's lenses: the row's, and the intent's that are new
        put_part(repo, "review", f"prosecutor-{lens}", {"lens": lens, "findings": []})
    cli("merge-review", RUN, "review")
    cli("gate", RUN, "review")
    commit_all(repo, "retry")
    cli("pass", RUN)
    return pl.read_ledger(repo, RUN)


def test_the_ledger_table_names_every_kind_of_entry_a_run_writes_and_only_fields_the_entries_hold(flow):
    text = section("Runs, gates and the pass record").split("**The ledger**", 1)[1].split("**Provenance.**", 1)[0]
    table = {r[0].strip("`"): r for r in rows(text)}
    assert set(table) == {entry["kind"] for entry in flow} == {"intake", "supplied", "agent", "merge", "run", "gate", "pass"}
    nested = {key for entry in flow if entry["kind"] == "run" for command in entry["commands"] for key in command}
    assert nested == {"name", "cmd", "exit_code", "seconds"}  # `timed_out` and `junit` are written only where they apply (tested with the commands table)
    for kind, (_kind, written_by, holds) in table.items():
        seen = set().union(*(entry.keys() for entry in flow if entry["kind"] == kind))
        for token in ticks(holds):
            assert token in seen or (kind == "run" and (token in nested or token in ("timed_out", "junit"))), (kind, token)
        assert "at" in seen  # every entry says when it was written
    assert "SubagentStop" in table["agent"][1] and "merge-review" in table["merge"][1] and "`pass`" in table["pass"][1]
    assert "plan --intent" in table["intake"][1] and "plan --intent" in table["supplied"][1] and "`gate`" in table["run"][1]
    assert {e["stage"] for e in flow if e["kind"] == "run"} == {"verify"}  # `gate` runs commands for a verify or tests stage
    assert {e["stage"] for e in flow if e["kind"] == "gate"} >= {"plan", "verify", "review"}  # the supplied plan's gate is entered by `plan --intent`


def test_a_record_no_agent_left_has_no_ledger_entry_and_its_gate_fails_as_the_provenance_paragraph_says(repo, run_cli, run_stop):
    adopt_base(repo, commands={"test": CONTROLLED})
    assert run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.S", cwd=repo).returncode == 0
    provenance = re.search(r"\*\*Provenance\.\*\*(.*?)\n\n", section("Runs, gates and the pass record"), re.S).group(1)
    for kind in ("agent", "merge", "supplied", "intake"):
        assert f"`{kind}`" in provenance, kind
    assert "has no such entry, and its gate fails" in provenance
    put(repo, "plan", spec_record(), agent=False)  # written by hand
    by_hand = run_cli("gate", RUN, "plan", cwd=repo)
    assert by_hand.returncode == 1 and "has no entry from plumbline:planner" in by_hand.stdout
    stop = run_stop(stop_payload(repo, "plumbline:planner", "planned\nRECORD: .plumbline/runs/r1/plan.json", agent_id="p-1"), repo)
    assert stop.returncode == 0 and stop.stdout == ""
    assert run_cli("gate", RUN, "plan", cwd=repo).returncode == 0  # the same file, once the planner has stopped with it
    (repo / ".plumbline" / "runs" / RUN / "plan.json").write_text((repo / ".plumbline" / "runs" / RUN / "plan.json").read_text() + " ")
    edited = run_cli("gate", RUN, "plan", cwd=repo)
    assert edited.returncode == 1 and "changed after plumbline:planner stopped" in edited.stdout


def test_the_readme_counts_a_stages_rounds_by_its_attempts_and_the_code_does(repo, run_cli, run_stop):
    text = section("Runs, gates and the pass record")
    rounds = re.search(r"\*\*Rounds\.\*\*(.*?)\n\n", text, re.S).group(1)
    assert "the number of its attempts since its gate last passed" in rounds
    assert "an agent that stops again before the gate for the same report is still one attempt" in rounds
    assert "the same agent resumed after a failed gate makes the next" in rounds
    assert "The `rounds` of a stage in the pass record count its attempts over the whole run" in rounds
    assert "A stop that leaves the `record`, `record_sha256` and `valid` that the agent's latest entry already holds is not entered again" in text
    adopt_base(repo, commands={"test": CONTROLLED})
    assert run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.S", cwd=repo).returncode == 0
    put(repo, "plan", spec_record(), agent=False)
    for _ in range(4):  # one planner, stopping four times with the same plan
        stop = run_stop(stop_payload(repo, "plumbline:planner", "planned\nRECORD: .plumbline/runs/r1/plan.json", agent_id="p-1"), repo)
        assert stop.returncode == 0 and stop.stdout == ""
    assert len([e for e in pl.read_ledger(repo, RUN) if e["kind"] == "agent"]) == 1
    gated = run_cli("gate", RUN, "plan", cwd=repo)
    assert gated.returncode == 0 and "round 1 of 2" in gated.stdout, gated.stdout


def test_the_readme_says_where_the_hook_looks_for_a_report_and_the_hook_looks_there():
    hook = section("SubagentStop", 3)
    assert "The report is its final message; when that message has no such line, the hook reads the `message` of the agent's last SubagentHandback call" in hook
    assert "from the end of its transcript, at most 2 MiB of it" in hook and subagent_stop.MAX_TRANSCRIPT_BYTES == 2 << 20
    assert subagent_stop.HANDBACK_TOOL == "SubagentHandback"
    assert "unless the agent's latest entry holds the same record, sha256 and validity already" in hook
    assert "each ending its report, its final message or the message of its SubagentHandback call, with `RECORD: <path>`" in section("Agents")


# ------------------------------------------------------------------------- the hooks: names, lists and quoted words


def test_the_hooks_section_gives_the_registered_matcher_and_names_every_tool_in_it():
    text = section("Hooks")
    assert f"`{MATCHER}`" in text
    for tool in MATCHER.split("|"):
        assert tool in text, tool
    assert "Monitor" in MATCHER.split("|") and "A Monitor runs a shell command, so it is read as Bash" in text


def test_every_lane_file_test_config_file_and_test_output_the_hook_knows_is_in_the_readme():
    text = section("Hooks")
    shown = {"claude.md": "CLAUDE.md", "claude.local.md": "CLAUDE.local.md", "agents.md": "AGENTS.md"}
    for name in (*pre.LANE_DIRS, *pre.LANE_FILES, *pre.TEST_CONFIG_FILES):
        assert shown.get(name, name) in text, name
    for pattern in pre.TEST_OUTPUT:
        assert pattern.replace("**/", "").replace("/**", "") in text, pattern


def test_the_review_file_names_of_the_hook_are_the_ones_the_readme_gives():
    text = section("Runs, gates and the pass record")
    for name in pre.REVIEW_FILE_NAMES.values():
        assert f"`{name}`" in text, name


def test_the_python_requirement_is_stated_and_the_scripts_check_it():
    install = section("Install")
    assert "`python3` 3.11 or newer" in install and "plumbline needs python3 on PATH" in install
    for script in ("plumbline.py", "pre_tool_use.py"):
        assert "sys.version_info < (3, 11)" in (REPO / "scripts" / script).read_text(encoding="utf-8"), script
    assert "plumbline needs python3 on PATH" in (REPO / "scripts" / "pre_tool_use.sh").read_text(encoding="utf-8")


def test_the_prefilter_words_of_the_readme_are_the_ones_the_sh_scripts_look_for():
    text = section("Hooks")
    script = (REPO / "scripts" / "pre_tool_use.sh").read_text(encoding="utf-8")
    for word in ("plumbline", "override", "git-", "git.", "git>"):
        assert word in script, word
    assert "a dash, a dot or a redirection" in text and "`override`" in text and "`plumbline.toml`" in text
    assert "*'plumbline:'*" in (REPO / "scripts" / "subagent_stop.sh").read_text(encoding="utf-8") and "(SubagentStop: `plumbline:`)" in text


# ---------------------------------------------------------- the hooks apply what the README says, in an adopted repository


@pytest.fixture
def adopted(repo):
    adopt(repo)
    write(repo / "README.md", "# demo\nan unreviewed change\n")
    commit_all(repo, "unreviewed")
    return repo


def ask(repo, command, role=None, cwd=None):
    return pre.decide(bash_payload(cwd or repo, command, agent_type=f"plumbline:{role}" if role else None))


def registered(home, repo, payload):
    """The command hooks/hooks.json registers, as Claude Code runs it: through a shell, the sh filter first."""
    env = clean_env(home, CLAUDE_PLUGIN_ROOT=str(REPO), GIT_CEILING_DIRECTORIES=str(repo.parent))
    result = subprocess.run(REGISTERED, shell=True, cwd=repo, capture_output=True, text=True, input=json.dumps(payload), env=env)
    assert result.returncode == 0 and result.stderr == "", result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if result.stdout.strip() else None


def test_the_push_gate_rules_the_readme_states_are_the_hooks(adopted):
    hooks = section("Hooks")
    for phrase in ("push one reviewed branch at a time", "`--all`, `--mirror`, `--tags`", "Deleting a remote ref", "`--dry-run`, `-n` and `--help` are not pushes"):
        assert phrase in hooks, phrase
    for command in ("git push --all origin", "git push --mirror origin", "git push --tags", "git push origin 'refs/heads/*:refs/heads/*'", "git push origin :"):
        assert "push one reviewed branch at a time" in ask(adopted, command), command
    for command in ("git push origin :feature", "git push --delete origin feature", "git push --dry-run origin feature", "git push -n origin feature", "git push --help"):
        assert ask(adopted, command) is None, command
    for command in ("git push origin feature", "git -c alias.p=push p origin feature", "env GIT_TRACE=1 git push origin feature", "sudo git push origin feature", "git-push origin feature"):
        assert "has no pass or override record" in ask(adopted, command), command
    assert "changes HEAD or a ref" in ask(adopted, "git commit --allow-empty -m x && git push origin feature")
    assert "has no pass or override record" in ask(adopted, "gh pr create --fill") and "has no pass or override record" in ask(adopted, "gh pr new --fill")
    git(adopted, "checkout", "-q", "-b", "elsewhere")
    assert "has no pass or override record" in ask(adopted, "git push origin feature", cwd=adopted / ".git")  # a working directory inside .git is checked


def test_a_covered_head_is_pushed_and_a_branch_made_in_the_same_line_is_covered_too(repo):
    adopt(repo)
    write(repo / "README.md", "# demo\nreviewed docs\n")
    commit_all(repo, "docs")
    genuine_pass(repo)
    assert ask(repo, "git push origin feature") is None
    assert ask(repo, "git switch -c x && git push origin x") is None  # the new branch points where HEAD does
    assert "changes HEAD or a ref" in ask(repo, "git commit --allow-empty -m x && git push origin feature")


def test_the_abbreviations_of_override_options_are_denied_and_the_other_words_are_not(adopted):
    assert "any abbreviation of its options that argparse would take (`--rea`, `--re`, `--proj`)" in section("Hooks")
    for option in ("--rea", "--re", "--reason"):
        assert "is the builder's command" in ask(adopted, f'python3 {CLI} override {option} "twenty characters or more of reason"'), option
    assert "is the builder's command" in ask(adopted, f'python3 {CLI} override --reason "twenty characters or more of reason" --proj {adopted}')
    assert ask(adopted, "echo override --run r1") is None


def test_the_agent_launch_rules_of_the_readme_are_the_hooks(adopted):
    text = section("Hooks").split("**On Agent:**", 1)[1].split("**On Read", 1)[0]
    for phrase in ("any `isolation` value is denied", "another model is denied", "(with `/` or `\\`, in any case)"):
        assert phrase in text, phrase

    def launch(**fields):
        return pre.decide(tool_payload(adopted, "Agent", {"description": "x", "prompt": "y", **fields}))

    assert launch(subagent_type="plumbline:builder") is None
    assert "without `isolation`" in launch(subagent_type="plumbline:builder", isolation="remote")
    assert launch(subagent_type="plumbline:builder", isolation=None) is None
    assert "pinned to the sonnet model" in launch(subagent_type="plumbline:builder", model="haiku")
    assert launch(subagent_type="plumbline:builder", model="Sonnet") is None and launch(subagent_type="plumbline:defender", model="HAIKU") is None
    for brief in ("read .plumbline/runs/r1/plan.json", "read .PLUMBLINE\\runs\\r1"):
        assert "plumbline:* agents" in launch(subagent_type="general-purpose", prompt=brief), brief
        assert "plumbline:* agents" in launch(prompt=brief), brief
    assert launch(subagent_type="general-purpose", prompt="read the readme") is None


def test_the_read_only_git_and_search_rules_of_the_readme_are_the_hooks(adopted):
    text = section("Hooks")
    for phrase in ("`--no-pager`, `-P`, `--no-optional-locks`, `--literal-pathspecs` and `--no-replace-objects`", "and every abbreviation of them (`--ope`, `--outp`)", "Neither takes a `VAR=value` before it"):
        assert phrase in text, phrase
    for command in ("git log --oneline -3", "git -C . status", "git --no-pager diff", "git grep -n retry", "find . -name '*.py'", "rg retry", "sed -n 1,3p README.md", "cat README.md > /dev/null"):
        assert ask(adopted, command, "verifier") is None, command
    for command in (
        "git -c core.pager=x log", "git diff --output=x", "git diff --outp=x", "git grep --ope='touch x' y", "git grep -O less y", "git diff --ext-diff", "git show --textconv HEAD",
        "GIT_EXTERNAL_DIFF=x git diff", "find . -exec rm {} ;", "find . -delete", "rg --pre x y", "sed -i s/a/b/ README.md", "cat README.md > out.txt",
    ):
        assert ask(adopted, command, "verifier"), command


def test_the_lane_the_test_harness_and_the_record_rules_of_the_readme_are_the_hooks(repo):
    adopt(repo)
    start_run(repo)

    def denied(role, path):
        return pre.decide(tool_payload(repo, "Write", {"file_path": str(repo / path), "content": "x"}, agent_type=f"plumbline:{role}" if role else None))

    lane = (".git/config", ".claude/settings.json", ".github/workflows/ci.yml", ".husky/pre-commit", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md",
            ".gitattributes", ".worktreeinclude", ".pre-commit-config.yaml", "src/.GIT/x", "docs/claude.md")
    for role in ("planner", "builder", "prosecutor"):
        for path in lane:
            assert "main session" in denied(role, path), (role, path)
    for path in ("conftest.py", "sub/conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "noxfile.py"):
        assert "main session" in denied("builder", path), path
    assert denied("builder", "pyproject.toml") is None and denied("builder", "Makefile") is None and denied("builder", "package.json") is None
    assert denied("test-writer", "tests/conftest.py") is None
    assert denied("planner", ".plumbline/runs/r1/plan.json") is None and denied("planner", ".plumbline/runs/r2/plan.json")
    assert denied("builder", "plumbline.toml") and denied("builder", "tests/test_x.py") and denied("builder", "src/app.py") is None
    assert "the active run's `stubs/` directory (`.plumbline/runs/<run-id>/stubs/`, never another run's)" in section("Hooks")
    assert denied("test-writer", ".plumbline/runs/r1/stubs/newmod.py") is None and denied("test-writer", ".plumbline/runs/r2/stubs/newmod.py")
    assert denied("planner", ".plumbline/runs/r1/stubs/newmod.py") and denied("builder", ".plumbline/runs/r1/stubs/newmod.py")
    for own, other in (("prosecutor", "defender-1.json"), ("defender", "prosecutor-x.json"), ("detective", "defender-1.json")):
        assert denied(own, f".plumbline/runs/r1/review/round-1/{other}"), (own, other)
    assert denied("prosecutor", ".plumbline/runs/r1/review/round-1/prosecutor-security.json") is None
    assert denied(None, ".plumbline/runs/ACTIVE") and denied("builder", ".plumbline/runs/ACTIVE") and denied(None, ".plumbline/pass/x.json")


# ------------------------------------------------------------------------------------ the five statements C-21 listed


def context(home, cwd):
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "session_start.py")], cwd=cwd, capture_output=True, text=True, env=clean_env(home, GIT_CEILING_DIRECTORIES=str(cwd.parent))
    )
    assert result.returncode == 0
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_c21_without_plumbline_toml_a_session_gets_the_note_a_hint_and_the_ponytail_line_in_any_directory(repo, tmp_path, home):
    assert "gives a session the discipline note, a one-line hint inside git repositories, and, where ponytail is not enabled, the line that says how to enable it (that line shows in any directory)" in README
    assert "only the discipline note" not in README
    plain = tmp_path / "plain"
    plain.mkdir()
    for where, hint in ((plain, False), (repo, True)):
        text = context(home, where)
        assert "Standing rule" in text and "ponytail is not enabled" in text, where
        assert ("plumbline: not adopted in this repository" in text) is hint, where
        assert "PONYTAIL_SUBAGENT_MATCHER" not in text  # the matcher line is for adopted repositories


def test_c21_a_refutation_is_a_quote_of_the_change_or_of_the_findings_file_and_not_words():
    assert "a quote of at least 6 characters, whitespace aside, that occurs in the change's diff (added, removed and context lines) or in the current content of the file the finding names" in section("Runs, gates and the pass record")
    assert "a quote of the code" not in README and pl.MIN_QUOTE == 6


def test_c21_pre_tool_use_and_subagent_stop_are_silent_outside_adopted_repositories_and_session_start_is_not(repo, run_pre, run_stop, home):
    assert "The PreToolUse and SubagentStop hooks are silent, and allow everything, in a repository without `plumbline.toml`" in README
    assert "SessionStart prints its note in every directory" in README and "All three hooks are silent" not in README
    assert run_pre(bash_payload(repo, "git push origin feature"), repo).stdout == ""
    stop = run_stop(stop_payload(repo, "plumbline:planner", "no record line"), repo)
    assert stop.returncode == 0 and stop.stdout == ""
    assert "Standing rule" in context(home, repo)


def test_c21_the_review_roles_write_their_own_files_and_nobody_elses(repo):
    adopt(repo)
    start_run(repo)
    assert "A review role that writes another's file, or another round's, is denied" in section("Hooks") and "their own record only" not in README
    for role, own in (("prosecutor", "prosecutor-security.json"), ("defender", "defender-2.json"), ("detective", "detective.json")):
        path = repo / ".plumbline" / "runs" / RUN / "review" / "round-1" / own
        assert pre.decide(tool_payload(repo, "Write", {"file_path": str(path), "content": "{}"}, agent_type=f"plumbline:{role}")) is None
        other = path.with_name("detective.json" if role != "detective" else "prosecutor-x.json")
        assert pre.decide(tool_payload(repo, "Write", {"file_path": str(other), "content": "{}"}, agent_type=f"plumbline:{role}"))


def test_c21_git_pu_quote_sh_is_recognised_and_g_quote_it_push_is_the_gap(adopted, home):
    limits = section("Limits")
    assert "A name spelled with an empty pair of quotes in the middle never passes the `sh` filter" in limits
    assert '`git pu""sh` is recognised and denied' in limits and 'pu""sh` is not recognised' not in README
    denied = registered(home, adopted, bash_payload(adopted, 'git pu""sh origin feature'))
    assert denied and "has no pass or override record" in denied
    assert registered(home, adopted, bash_payload(adopted, 'g""it push origin feature')) is None  # the filter never starts Python for it
    assert "has no pass or override record" in registered(home, adopted, bash_payload(adopted, "git push origin feature"))


# ---------------------------------------------------------------------------------------- the limits are real, today


def test_the_limits_section_is_framed_as_guard_rails_that_ci_backs_up():
    limits = section("Limits")
    assert "The hooks are guard rails" in limits and "They are not a sandbox" in limits
    assert "what stops that is CI" in limits and "phase 4" in limits


INDIRECT_PUSHES = [
    ("a pipe into a shell", "echo 'git push origin feature' | sh"),
    ("a heredoc or here-string into a shell", "bash <<'EOF'\ngit push origin feature\nEOF"),
    ("a heredoc or here-string into a shell", "sh <<< 'git push origin feature'"),
    ("a variable or command substitution that supplies the command name", "G=git; $G push origin feature"),
    ("a variable or command substitution that supplies the command name", "$(echo git) push origin feature"),
    ("`xargs git push`", "echo feature | xargs git push origin"),
    ("`find -exec git push`", "find . -maxdepth 0 -exec git push origin feature \\;"),
    ("a script file", "printf 'git push origin feature' > push.sh; sh push.sh"),
    ("`python3` with `subprocess`", "python3 -c \"import subprocess; subprocess.run(['git','push','origin','feature'])\""),
    ("`git rebase -x`", "git rebase -x 'git push origin feature' HEAD~1"),
    ("`git bisect run`", "git bisect run sh -c 'git push origin feature'"),
    ("`git submodule foreach`", "git submodule foreach 'git push origin feature'"),
    ("an `eval` of a variable", "C='git push origin feature'; eval \"$C\""),
    ("Brace expansion of the command word", "{git,push,origin,feature}"),
    ("shell nesting deeper than three levels", "sh -c \"sh -c 'sh -c \\\"sh -c \\\\\\\"git push origin feature\\\\\\\"\\\"'\""),
]


@pytest.mark.parametrize("claim,command", INDIRECT_PUSHES, ids=[c for _, c in INDIRECT_PUSHES])
def test_each_indirect_push_the_readme_lists_is_not_seen(adopted, claim, command):
    assert claim in section("Limits"), claim
    assert ask(adopted, command) is None, f"the hook stops this now: take it off the list of limits in the README, and off this list ({command!r})"


def test_the_forms_the_parser_does_read_are_the_ones_the_readme_says_it_reads(adopted):
    assert "It reads `sh -c`, `eval`, `$( )`, backticks, heredocs and `env -S` when the text is there, up to three shells deep" in section("Limits")
    for command in (
        "sh -c 'git push origin feature'", "eval 'git push origin feature'", "x=$(git push origin feature)", "echo `git push origin feature`", "env -S 'git push origin feature'",
        "cat <<EOF\nx\nEOF\ngit push origin feature", "sh -c \"sh -c 'git push origin feature'\"",
    ):
        assert "has no pass or override record" in ask(adopted, command), command


def test_the_tools_the_hook_does_not_cover_are_the_ones_outside_its_matcher():
    limits = section("Limits")
    assert "an MCP GitHub tool" in limits and "the older tool name `Task`" in limits
    tools = MATCHER.split("|")
    assert "Task" not in tools and not [t for t in tools if t.startswith("mcp__")] and "Agent" in tools


def test_a_published_commit_is_not_checked_against_the_remote_and_the_readme_says_so(repo, tmp_path):
    adopt(repo)
    write(repo / "README.md", "# demo\nunreviewed\n")
    commit_all(repo, "unreviewed")
    add_origin(repo, tmp_path)
    git(repo, "push", "-q", "origin", "feature")  # the commit is published
    assert "The gate never asks the remote" in section("Limits")
    assert "has no pass or override record" in ask(repo, "git push origin feature")


def test_a_repository_whose_plumbline_toml_is_moved_aside_is_not_adopted_and_a_pre_adoption_branch_has_no_adoption(repo):
    limits = section("Limits")
    assert "`mv plumbline.toml plumbline.toml.off` in a call of its own" in limits and "A branch or commit from before `plumbline.toml` existed" in limits
    adopt(repo)
    write(repo / "README.md", "# demo\nunreviewed\n")
    commit_all(repo, "unreviewed")
    assert ask(repo, "mv plumbline.toml plumbline.toml.off") is None
    (repo / "plumbline.toml").rename(repo / "plumbline.toml.off")
    assert ask(repo, "git push origin feature") is None
    (repo / "plumbline.toml.off").rename(repo / "plumbline.toml")
    assert ask(repo, "git push origin feature")
    git(repo, "checkout", "-q", "-b", "old", "HEAD~2")  # before the adoption: no plumbline.toml here
    assert not (repo / "plumbline.toml").exists() and ask(repo, "git push origin old") is None


def test_a_symlink_alias_hides_the_pass_directory_from_the_text_of_a_command_and_from_the_sh_filter(adopted, home):
    assert "A link named anything, pointing at `.plumbline/pass/` or at a ledger" in section("Limits")
    (adopted / ".plumbline" / "pass").mkdir(parents=True)
    (adopted / "safelink").symlink_to(".plumbline/pass")
    assert ask(adopted, "echo '{}' > safelink/x.json") is None  # the text names no plumbline path
    payload = tool_payload(adopted, "Write", {"file_path": str(adopted / "safelink" / "x.json"), "content": "{}"})
    assert "written only by plumbline.py commands" in pre.decide(payload)  # the write tools follow the link
    assert registered(home, adopted, payload) is None  # and the registered command never starts Python for a call that does not name plumbline


def test_an_honest_command_outside_the_role_classes_is_refused_and_the_readme_names_them(adopted):
    assert "`test`, `[` and `sort` are not among them" in section("Limits")
    for command in ("sort README.md", "test -f README.md", "[ -f README.md ]", "case x in a) echo 1;; b) echo 2;; esac", "sed -n '1p;2p' README.md"):
        assert "may run only" in ask(adopted, command, "verifier"), command


def test_the_writers_the_hook_does_not_model_write_where_the_hook_says_nothing(adopted):
    assert "Any other program that writes a file is not" in section("Limits")
    (adopted / ".plumbline" / "pass").mkdir(parents=True)
    assert ask(adopted, "python3 -c \"open('.plumbline/pass/x.json', 'w').write('{}')\"") is None
    for command in (
        "echo x > .plumbline/pass/x.json", "tee .plumbline/pass/x.json", "rm .plumbline/pass/x.json", "cp /tmp/x .plumbline/pass/x.json", "dd of=.plumbline/pass/x.json",
        "sed -i s/a/b/ .plumbline/pass/x.json", "curl -o .plumbline/pass/x.json http://example.invalid/x",
    ):  # the writers it does read
        assert "written only by plumbline.py commands" in ask(adopted, command), command


def test_the_builder_can_still_edit_the_files_the_readme_names_and_the_guarded_hashes_leave_them_out(repo):
    adopt(repo)
    start_run(repo)
    assert "The builder can still edit `pyproject.toml`, the `Makefile` and `package.json`" in section("Limits")
    for name in ("pyproject.toml", "Makefile", "package.json"):
        assert pre.decide(tool_payload(repo, "Write", {"file_path": str(repo / name), "content": "x"}, agent_type="plumbline:builder")) is None, name
    assert not [name for name in pl.guarded_snapshot(pl.load_project(repo)) if name in ("pyproject.toml", "Makefile", "package.json")]


def test_the_measured_commands_run_with_the_sessions_rights_and_the_skills_pre_approve_any_python3():
    limits = section("Limits")
    assert "with the rights of the main session" in limits and "That is not a sandbox" in limits and "`Bash(python3 *)`" in limits
    for skill in ("run", "status", "init", "override"):
        assert "Bash(python3 *)" in (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"), skill
    source = (REPO / "scripts" / "plumbline.py").read_text(encoding="utf-8")
    assert '["sh", "-c", full]' in source and "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" in source  # sh -c, no sandbox, no output kept
