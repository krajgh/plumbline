"""The plugin's manifests, hooks, skills and licence files."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import plumbline as pl
from helpers import PROHIBITION, REPO, clean_env, prose_lines
from hookdata import bash_payload, stop_payload
from rundata import adopt, put

PLUGIN = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
HOOKS = REPO / "hooks" / "hooks.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter(path):
    """A SKILL.md's frontmatter as a dict of single-line `key: value` pairs, and its body."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: the frontmatter must open on the first line"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, f"{path}: the frontmatter is never closed"
    fields = {}
    for line in head.splitlines():
        key, colon, value = line.partition(": ")
        assert colon and key and not key.startswith(" "), f"{path}: not a simple 'key: value' line: {line!r}"
        assert key not in fields, f"{path}: {key} appears twice"
        fields[key] = value
    return fields, body


# --- plugin.json and marketplace.json


def test_plugin_json_says_what_the_task_specified():
    plugin = load(PLUGIN)
    assert plugin["name"] == "plumbline"
    assert plugin["version"] == "0.4.2"
    assert plugin["author"] == {"name": "krajgh", "url": "https://github.com/krajgh"}
    assert plugin["homepage"] == "https://github.com/krajgh/plumbline"
    assert plugin["repository"] == "https://github.com/krajgh/plumbline"
    assert plugin["license"] == "Apache-2.0"
    assert plugin["dependencies"] == [{"name": "ponytail", "marketplace": "ponytail"}]
    assert isinstance(plugin["description"], str) and plugin["description"].strip()


def test_the_plugin_description_is_one_sentence():
    description = load(PLUGIN)["description"]
    assert description.endswith(".") and len(re.findall(r"[.!?](?:\s|$)", description)) == 1


def test_the_ponytail_dependency_is_unpinned():
    # ponytail tags vX.Y.Z, not ponytail--vX.Y.Z, so a version range could not resolve
    [dependency] = load(PLUGIN)["dependencies"]
    assert "version" not in dependency


def test_marketplace_json_is_the_plumbline_marketplace_with_the_plugin_at_its_root():
    market = load(MARKETPLACE)
    assert market["name"] == "plumbline"
    assert market["owner"] == {"name": "krajgh"}
    assert market["allowCrossMarketplaceDependenciesOn"] == ["ponytail"]
    assert market["description"].strip()
    [entry] = market["plugins"]
    assert entry["name"] == load(PLUGIN)["name"]  # an entry name that differs from the manifest name breaks installs
    assert entry["source"] == "./"
    assert entry["description"].strip()


def test_the_dependency_marketplace_is_on_the_allowlist():
    dependency = load(PLUGIN)["dependencies"][0]
    assert dependency["marketplace"] in load(MARKETPLACE)["allowCrossMarketplaceDependenciesOn"]


# --- hooks


def hook_commands():
    for event, groups in load(HOOKS)["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, group, hook


def hook_of(event):
    [(_, _, hook)] = [entry for entry in hook_commands() if entry[0] == event]
    return hook


def test_hooks_json_registers_session_start_pre_tool_use_and_subagent_stop():
    hooks = load(HOOKS)["hooks"]
    assert list(hooks) == ["SessionStart", "PreToolUse", "SubagentStop"]
    [group] = hooks["SessionStart"]
    assert "matcher" not in group
    [hook] = group["hooks"]
    assert hook["type"] == "command" and hook["timeout"] == 10
    [group] = hooks["PreToolUse"]
    assert group["matcher"] == "Bash|PowerShell|Monitor|Read|Grep|Glob|Edit|Write|NotebookEdit|Agent"  # letters and | only: an exact list of tool names; Monitor runs a shell command as Bash does
    [hook] = group["hooks"]
    assert hook["type"] == "command" and hook["timeout"] == 30
    [group] = hooks["SubagentStop"]
    assert "matcher" not in group  # the script tells plumbline agents from the others
    [hook] = group["hooks"]
    assert hook["type"] == "command" and hook["timeout"] == 30


def test_every_hook_command_ends_in_or_true():
    commands = [hook["command"] for _, _, hook in hook_commands()]
    assert len(commands) == 3
    for command in commands:
        assert command.endswith(" || true"), command


def test_each_hook_command_runs_its_script_from_the_plugin_root_quoted():
    # session start runs Python once per session; the hooks that run on every tool call and every agent stop go through an sh filter
    assert hook_of("SessionStart")["command"] == 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_start.py" || true'
    for event, script in (("PreToolUse", "pre_tool_use"), ("SubagentStop", "subagent_stop")):
        assert hook_of(event)["command"] == f'sh "${{CLAUDE_PLUGIN_ROOT}}/scripts/{script}.sh" || true'
        assert (REPO / "scripts" / f"{script}.sh").is_file() and (REPO / "scripts" / f"{script}.py").is_file()


def plugin_root_with_a_space(tmp_path):
    root = tmp_path / "with space" / "plumbline"
    for name in ("scripts", "schemas", "pipeline"):
        shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def run_registered(event, payload, root, cwd, home, tmp_path):
    """Exactly as Claude Code runs it: the command string through a shell, the plugin root in the environment."""
    return subprocess.run(
        hook_of(event)["command"], shell=True, cwd=cwd, capture_output=True, text=True,
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(root), CLAUDE_PROJECT_DIR=str(cwd), GIT_CEILING_DIRECTORIES=str(tmp_path)),
        input=payload if isinstance(payload, str) else json.dumps(payload),
    )


def test_the_hook_command_runs_under_sh_from_a_plugin_root_with_a_space(tmp_path, home):
    project = tmp_path / "project"
    project.mkdir()
    result = run_registered("SessionStart", {"hook_event_name": "SessionStart", "source": "startup"}, plugin_root_with_a_space(tmp_path), project, home, tmp_path)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "plumbline:subagent-discipline" in context


def test_the_registered_pre_tool_use_command_denies_through_the_shell(tmp_path, home, repo):
    adopt(repo)
    result = run_registered("PreToolUse", bash_payload(repo, "git push origin feature"), plugin_root_with_a_space(tmp_path), repo, home, tmp_path)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny" and "has no pass or override record" in output["permissionDecisionReason"]


def test_the_registered_subagent_stop_command_blocks_through_the_or_true_as_json_on_stdout(tmp_path, home, repo):
    # `|| true` turns the script's exit status 2 into 0: what blocks is the decision on stdout
    adopt(repo)
    put(repo, "plan", {"goal": ""})
    payload = stop_payload(repo, message="RECORD: .plumbline/runs/r1/plan.json")
    result = run_registered("SubagentStop", payload, plugin_root_with_a_space(tmp_path), repo, home, tmp_path)
    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block" and "not a valid spec" in decision["reason"]


def test_the_registered_commands_let_everything_through_when_nothing_applies(tmp_path, home, repo):
    root = plugin_root_with_a_space(tmp_path)  # the repository has not adopted plumbline
    for event, payload in (("PreToolUse", bash_payload(repo, "git push")), ("SubagentStop", stop_payload(repo, message="no record"))):
        result = run_registered(event, payload, root, repo, home, tmp_path)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), event


@pytest.mark.parametrize("event", ["SessionStart", "PreToolUse", "SubagentStop"])
def test_the_or_true_keeps_a_broken_hook_from_failing_the_session(tmp_path, home, event):
    result = subprocess.run(
        hook_of(event)["command"], shell=True, cwd=tmp_path, capture_output=True, text=True,
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(tmp_path / "nowhere")), input="{}",
    )
    assert result.returncode == 0  # the script was not found (python exits 2 for that), and the hook still exits 0
    assert result.stdout == ""  # and says nothing that could be read as a decision


@pytest.mark.parametrize("script", ["pre_tool_use", "subagent_stop"])
def test_a_hook_script_that_cannot_import_plumbline_still_exits_0_and_says_nothing(tmp_path, home, script):
    broken = tmp_path / "broken"
    broken.mkdir()
    shutil.copy(REPO / "scripts" / f"{script}.py", broken / f"{script}.py")
    (broken / "plumbline.py").write_text("raise SystemExit('this Python is too old')\n", encoding="utf-8")
    payload = bash_payload(tmp_path, "git push") if script == "pre_tool_use" else stop_payload(tmp_path, message="no record")
    result = subprocess.run(
        ["python3", str(broken / f"{script}.py")], cwd=tmp_path, capture_output=True, text=True, env=clean_env(home), input=json.dumps(payload),
    )
    assert (result.returncode, result.stdout) == (0, "")


# --- skills


def skill_dirs():
    return sorted(p.parent for p in (REPO / "skills").glob("*/SKILL.md"))


def test_the_five_skills_exist():
    assert [d.name for d in skill_dirs()] == ["init", "override", "run", "status", "subagent-discipline"]


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda d: d.name)
def test_skills_have_valid_frontmatter(directory):
    fields, body = frontmatter(directory / "SKILL.md")
    assert fields["name"] == directory.name
    assert fields["description"].strip()
    assert ": " not in fields["description"], "a colon and space in a plain YAML scalar breaks the frontmatter"
    assert not fields["description"].startswith(("'", '"', "[", "{", "&", "*", "!", "|", ">", "%", "@", "`", "#"))
    assert " #" not in fields["description"]
    assert body.strip()


def test_the_init_skill_is_user_invocable_only_and_may_run_python():
    fields, body = frontmatter(REPO / "skills" / "init" / "SKILL.md")
    assert fields["disable-model-invocation"] == "true"
    assert fields["allowed-tools"] == "Bash(python3 *)"
    assert set(fields) == {"name", "description", "disable-model-invocation", "allowed-tools"}
    assert 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" init' in body
    assert "plumbline.toml" in body and ".gitignore" in body and "commit" in body.lower()
    assert "!`" not in body, "a bang and backtick would run a command when the skill loads"


def test_the_discipline_skill_keeps_the_wording_it_moved_with():
    fields, body = frontmatter(REPO / "skills" / "subagent-discipline" / "SKILL.md")
    assert fields["name"] == "subagent-discipline"
    assert "disable-model-invocation" not in fields  # Claude loads it when the work begins
    for heading in ("# Appropriately sized subagents, always", "## Sizing", "## Briefs", "## Shared files: map and reduce", "## Running them"):
        assert heading in body
    assert "smaller models substitute their own model name" in body


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda d: d.name)
def test_the_skills_state_rules_as_what_to_do(directory):  # C-23
    fields, body = frontmatter(directory / "SKILL.md")
    found = [m.group(0) for line in [fields["description"], *prose_lines(body)] for m in PROHIBITION.finditer(line)]
    assert found == [], f"{directory.name}: {found}"


def run_skill():
    return frontmatter(REPO / "skills" / "run" / "SKILL.md")[1]


def between(text, start, end):
    return text[text.index(start) : text.index(end)]


def test_the_run_skill_briefs_the_defender_with_the_merge_base_and_the_hash():  # C-24
    body = run_skill()
    defenders = between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    assert "the merge base" in defenders and "diff_sha256" in defenders and "the paths of the findings records" in defenders
    prompt = (REPO / "agents" / "defender.md").read_text(encoding="utf-8")
    assert "the merge base" in prompt and "`diff_sha256`" in prompt  # what the prompt expects, the brief gives


def test_the_run_skill_launches_every_plumbline_agent_without_a_model_because_each_is_pinned():
    body = run_skill()
    paragraph = between(body, "**Launch every plumbline agent", "**Every brief**")
    assert "without `model`" in paragraph and "each is pinned in its definition" in paragraph and "Leave `isolation` out as well" in paragraph
    assert "Sonnet for the planner, test-writer, builder, prosecutor, detective and canary; Haiku for the verifier and defender" in paragraph
    pins = {name: frontmatter(REPO / "agents" / f"{name}.md")[0]["model"] for name in pl.AGENT_ROLES}  # what the agent files say, which is what the paragraph repeats
    assert sorted(n for n, model in pins.items() if model == "sonnet") == sorted(["planner", "test-writer", "builder", "prosecutor", "detective", "canary"])
    assert sorted(n for n, model in pins.items() if model == "haiku") == ["defender", "verifier"]
    assert "(without `model`: the defender is pinned to Haiku)" in between(body, "2. **Defenders.**", "3. `PLUMBLINE merge-review")
    assert "model:" not in body  # no launch in the skill names a model


def test_the_run_skill_briefs_the_test_writer_on_how_the_tests_run_and_where_the_stubs_go():
    body = run_skill()
    brief = between(body, "- test-writer:", "- builder: the plan record.")
    for needed in (
        "the plan's `stubs_dir`", "the tests run against today's code, with the new names imported inside the test functions",
        "so that each test fails when it runs (on the import of a name the change has yet to add, or on an assertion) and not at collection",
        "Stubs are for brand-new modules only: they go in `stubs_dir`, outside the change and hidden from the builder",
        '(for pytest, the test command followed by `-o pythonpath="<stubs_dir> ."`)', "Its `files_written` lists the stubs too",
    ):
        assert needed in brief, needed
    assert "the `stubs_dir` of the run" in between(body, "## 3. Start the run", "## 4. The stages")
    assert "tests/_stubs" not in body


def test_the_run_skill_briefs_the_detective_with_the_merge_base_the_spec_and_the_tests_record():  # C-24
    body = run_skill()
    detective = between(body, "4. **Detective.**", "5. `PLUMBLINE gate")
    for needed in ("the merge base", "the path of the spec", "of the tests record", "diff_sha256", "the merged record's path", "its record path"):
        assert needed in detective, needed
    prompt = (REPO / "agents" / "detective.md").read_text(encoding="utf-8")
    assert "the merge base" in prompt and "the paths of the spec and of the tests record" in prompt


def test_the_run_skill_gives_every_review_agent_the_hash_of_the_change_from_check_diff():  # C-09
    body = run_skill()
    assert "PLUMBLINE check-diff --run <run_id>" in body and "`merge_base` and the `diff_sha256`" in body
    for start, end in (("1. **Prosecutors.**", "2. **Defenders.**"), ("2. **Defenders.**", "3. `PLUMBLINE merge-review"), ("4. **Detective.**", "5. `PLUMBLINE gate")):
        assert "`diff_sha256`" in between(body, start, end), start
    for agent in ("prosecutor", "defender", "detective"):
        prompt = (REPO / "agents" / f"{agent}.md").read_text(encoding="utf-8")
        assert prompt.count("diff_sha256") >= 2, agent  # in what the brief gives, and in the record


def test_the_run_skill_routes_tests_lens_findings_to_the_test_writer_and_gives_the_builder_text_only():  # C-06, C-07
    body = run_skill()
    failing = between(body, "## 6. When a gate fails", "## 7. Reduce")
    assert "The surviving findings under \"for the test-writer\" go to the test-writer" in failing
    assert "\"for the builder\" text of the surviving findings" in failing
    brief = between(body, "- builder: the plan record.", "- verifier:")
    assert "verbatim" in brief and "no test file, test name, assertion or tests-lens finding, and no path of a review file" in brief
    assert "review-file" not in brief and "by path" not in brief  # the old brief handed the builder the review record by its path


def test_the_run_skill_says_how_a_run_in_progress_is_resumed():  # C-30
    body = run_skill()
    resume = between(body, "**A run may be in progress already**", "## 1. The intent")
    assert ".plumbline/runs/ACTIVE" in resume and "PLUMBLINE status" in resume
    assert "continue that run" in resume and "from the first one that is not `pass`, `supplied` or `recorded`" in resume
    status = frontmatter(REPO / "skills" / "status" / "SKILL.md")[1]
    assert "`/plumbline:run` continues it" in status


def test_the_run_skill_says_what_the_rounds_and_exit_3_mean_and_where_plan_and_tests_go_back_to():  # C-08
    failing = between(run_skill(), "## 6. When a gate fails", "## 7. Reduce")
    assert '"round k of N"' in failing and "exits 3 when the stage has used its rounds" in failing
    assert "`plan` and `tests` go back to their own agent" in failing


def test_the_run_skill_says_where_a_problem_of_the_tests_and_verify_gates_comes_from():
    failing = between(run_skill(), "## 6. When a gate fails", "## 7. Reduce")
    assert "starts with where it comes from: `the agent's record` (what the agent typed) or `the measured run` (what `gate` saw when it ran the repository's commands)" in failing
    assert "A problem of the record goes back to the agent that wrote it" in failing and "`no test command is declared`" in failing
    assert pl._typed(["x"]) == ["the agent's record: x"] and pl._measured(["x"]) == ["the measured run: x"]  # the words the skill quotes


def test_the_run_skill_says_gate_opens_each_later_rounds_directory_and_asks_for_no_step_of_the_main_session():  # the hook's round and the CLI's meet here
    body = run_skill()
    units = between(body, "## 5. Review units", "Start each round with")
    assert "the hook holds them to the highest round directory their stage has" in units and "Round 1's directory appears when its first agent writes" in units
    assert "`gate` creates `round-<n+1>/` itself" in units and "blockers standing" in units and "rounds left" in units
    assert "`merge-review` with no `--round` merges the highest round" in units
    assert "the one whose directory `gate` opened" in between(body, "## 6. When a gate fails", "## 7. Reduce")
    assert "mkdir" not in body and "create the directory" not in body.lower() and "create the round" not in body.lower()


def test_the_run_skill_names_the_active_file_and_the_measured_row():
    body = run_skill()
    assert "names the run in `.plumbline/runs/ACTIVE`" in body
    assert "plumbline measures the change again" in body and "`pass` refuses a run whose row lacks a stage the measured row selects" in body


def plumbline_mentions():
    """Every `plumbline.py` subcommand a skill or an agent prompt names, with the options written on the same line."""
    found = []
    for path in [*(REPO / "skills").glob("*/SKILL.md"), *(REPO / "agents").glob("*.md")]:
        for line in path.read_text(encoding="utf-8").splitlines():
            for match in re.finditer(r"(?:PLUMBLINE|plumbline\.py\"?) ([a-z][a-z-]+)((?: [^\s`]+)*)", line):
                words = match.group(2).split()
                found.append((path.name if path.name != "SKILL.md" else path.parent.name, match.group(1), [w for w in words if w.startswith("--")]))
    return found


def test_every_command_and_option_the_skills_and_agents_name_is_one_of_the_cli():
    commands = next(a for a in pl.build_parser()._actions if getattr(a, "choices", None)).choices
    mentions = plumbline_mentions()
    assert len(mentions) > 20
    for source, command, options in mentions:
        assert command in commands, f"{source}: plumbline.py {command}"
        known = {opt for action in commands[command]._actions for opt in action.option_strings}
        for option in options:
            assert option.split("=")[0] in known, f"{source}: plumbline.py {command} {option}"


def test_the_skills_allowed_tools_still_reach_only_the_commands_they_run():
    # `Bash(python3 *)` is as narrow as it gets: the plugin's install path differs from one user to the next, and only the bodies of a skill
    # are known to get ${CLAUDE_PLUGIN_ROOT} substituted, so no pattern naming the script's path can be shown to match its quoted form
    for name in ("init", "override", "run", "status"):
        fields, body = frontmatter(REPO / "skills" / name / "SKILL.md")
        assert fields["allowed-tools"].startswith("Bash(python3 *)")
        assert "python3 " in body


# --- licence and notice


def test_license_is_the_canonical_apache_text():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("Apache License\n")
    assert "Version 2.0, January 2004" in text
    assert "http://www.apache.org/licenses/" in text
    assert text.count("END OF TERMS AND CONDITIONS") == 1
    assert text.endswith("limitations under the License.\n")
    assert len(text) == 11357


def test_notice_is_exactly_the_specified_text():
    assert (REPO / "NOTICE").read_text(encoding="utf-8") == (
        "plumbline\n"
        "Copyright 2026 Krishnaraj Ajay Gharpure\n"
        "\n"
        "This product includes software developed by Krishnaraj Ajay Gharpure (https://github.com/krajgh).\n"
    )


def test_gitignore_ignores_python_caches():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "__pycache__/" in lines and ".pytest_cache/" in lines


# --- layout and hygiene


def test_the_layout_matches_the_spec():
    for path in (
        ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "LICENSE", "NOTICE", "README.md",
        "hooks/hooks.json", "scripts/plumbline.py", "scripts/session_start.py", "scripts/subagent_stop.py", "scripts/pre_tool_use.py",
        "scripts/pre_tool_use.sh", "scripts/subagent_stop.sh", "pipeline/default.toml", "pipeline/templates/refactor.json",
        "skills/subagent-discipline/SKILL.md", "skills/init/SKILL.md", "skills/run/SKILL.md", "skills/override/SKILL.md", "skills/status/SKILL.md",
        *(f"agents/{name}.md" for name in ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")),
    ):
        assert (REPO / path).is_file(), path
    assert sorted(p.name for p in (REPO / "agents").iterdir()) == sorted(  # the eight agents, and nothing else
        f"{name}.md" for name in ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")
    )
    assert sorted(p.name for p in (REPO / "schemas").glob("*.json")) == sorted(
        f"{n}.json" for n in (
            "change_class", "spec", "tests_record", "build_note", "verify_record", "review_record", "pass_record", "override_record",
            "findings_record", "defense_record", "gaps_record",
        )
    )


def tracked_or_untracked_text_files():
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, text=True,
    )
    if listed.returncode != 0:  # not a checkout: walk the tree instead
        return [p for p in REPO.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts]
    return [REPO / name for name in listed.stdout.split("\0") if name]


# Resolved when this module is imported, before the autouse fixture gives each test a throw-away HOME:
# resolved inside the test, the maintainer's list would never be found.
PRIVATE_WORDS = Path(os.environ.get("PLUMBLINE_PRIVATE_WORDS") or Path.home() / ".config" / "plumbline" / "private-words.txt")

def test_nothing_names_a_private_project_person_or_path():
    # built from pieces so that this file does not contain what it forbids; a maintainer's own
    # private words, one per line, are read from a file outside the repository when it exists
    forbidden = ["/ho" + "me/", "@" + "gmail"]
    if PRIVATE_WORDS.is_file():
        forbidden += [w.strip() for w in PRIVATE_WORDS.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]
    offenders = []
    for path in tracked_or_untracked_text_files():
        if path.suffix in (".pyc",) or path == REPO / "tests" / "test_manifests.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [f"{path.relative_to(REPO)}: {word}" for word in forbidden if word in text]
    assert offenders == []


def test_no_file_uses_an_absolute_home_path_or_an_env_file():
    names = [p.name for p in tracked_or_untracked_text_files()]
    assert not [n for n in names if n == ".env" or n.startswith(".env.")]
