#!/usr/bin/env python3
"""SessionStart hook for plumbline.

Prints exactly one JSON object on stdout, {"hookSpecificOutput": {"hookEventName":
"SessionStart", "additionalContext": "..."}}, and always exits 0. The context is:

  - the subagent-discipline note, always;
  - inside a git repository, one line saying whether plumbline is adopted there
    (a plumbline.toml at the top level), not adopted, or has an invalid config;
  - a warning when the ponytail plugin is not enabled in any settings file, and, when it is and
    the repository has adopted plumbline, a line unless its subagent matcher (the
    PONYTAIL_SUBAGENT_MATCHER environment variable, set through the `env` block of a settings
    file) reaches exactly plumbline's planner, test-writer and builder among plumbline's agents.
    The matcher is checked the way ponytail's hook uses it, `new RegExp(value, 'i')` under node,
    and with Python's re where node is missing. plumbline writes no settings itself: the line
    says what to add, once.

Nothing here may fail the session: every check is isolated, and if one breaks
the note still goes out.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DISCIPLINE_NOTE = (
    "Standing rule: do the work through subagents sized to the job - Haiku "
    "for mechanical steps (commands, scripted edits, plumbing, collecting "
    "numbers), Sonnet where judgement matters (code that needs its "
    "surroundings read, reviews, judging). This session keeps the brief, "
    "the review of what comes back, and the decisions, and it alone writes "
    "the files the whole project shares. Details: the "
    "plumbline:subagent-discipline skill."
)

NOT_ADOPTED = "plumbline: not adopted in this repository; /plumbline:init adopts it."

PONYTAIL_WARNING = (
    "ponytail is not enabled, and plumbline requires it: "
    "claude plugin marketplace add DietrichGebert/ponytail, "
    "then claude plugin install ponytail@ponytail."
)

# ponytail's SubagentStart hook (hooks/ponytail-subagent.js) injects its ruleset into every subagent, unless the
# PONYTAIL_SUBAGENT_MATCHER environment variable holds a regular expression: then only a subagent whose agent_type
# matches it (unanchored, case-insensitive) gets the ruleset. plumbline wants it on the agents that make the change.
MATCHER_VAR = "PONYTAIL_SUBAGENT_MATCHER"
# The first alternative keeps ponytail on for every subagent that is not a plumbline agent; the second picks the three that make the change.
MATCHER_EXAMPLE = "^(?!plumbline:)|^plumbline:(planner|test-writer|builder)$"
MAKING_AGENTS = ("planner", "test-writer", "builder")
ALL_AGENTS = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary", "orchestrator")


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def adoption_line(plumbline, root: Path) -> str:
    """The line for a repository whose top level is `root`."""
    try:
        project = plumbline.load_project(root)
    except Exception as exc:  # the config could not even be examined
        return one_line(f"plumbline: could not read {plumbline.CONFIG_FILE} ({exc}).")
    if not project.adopted:
        return NOT_ADOPTED
    if project.errors:
        problem = project.errors[0].removeprefix(f"{plumbline.CONFIG_FILE}: ")
        more = f" (and {len(project.errors) - 1} more)" if len(project.errors) > 1 else ""
        return one_line(f"plumbline: {plumbline.CONFIG_FILE} is invalid: {problem}{more}")
    name = project.pipeline.get("name", plumbline.DEFAULT_PIPELINE)
    graft = "on" if project.graft_enabled else "off"
    return (
        f"plumbline: adopted in this repository (pipeline '{name}', graft {graft}). "
        "Changes go through the pipeline before they are pushed: /plumbline:run."
    )


def ponytail_enabled(project_dirs: list) -> bool:
    """True when an `enabledPlugins` key starting `ponytail@` is true in the user
    settings or in a project's settings.json or settings.local.json."""
    home = Path(os.environ.get("HOME") or Path.home())
    files = [home / ".claude" / "settings.json"]
    for directory in project_dirs:
        files += [directory / ".claude" / "settings.json", directory / ".claude" / "settings.local.json"]
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        enabled = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(enabled, dict) and any(
            isinstance(key, str) and key.startswith("ponytail@") and value is True
            for key, value in enabled.items()
        ):
            return True
    return False


def settings_env(project_dirs: list, name: str) -> str | None:
    """The value of `name` in the `env` block of the settings files: a project's local settings, then its shared
    ones, then the user's."""
    home = Path(os.environ.get("HOME") or Path.home())
    files = []
    for directory in project_dirs:
        files += [directory / ".claude" / "settings.local.json", directory / ".claude" / "settings.json"]
    files.append(home / ".claude" / "settings.json")
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        env = data.get("env") if isinstance(data, dict) else None
        value = env.get(name) if isinstance(env, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


NODE_SCRIPT = (
    "let re; try { re = new RegExp(process.argv[1], 'i'); } catch (e) { process.exit(3); } "
    "console.log(JSON.stringify(process.argv.slice(2).map(t => re.test(t))));"
)


def node_reach(node: str, value: str) -> tuple[bool, list[str]] | None:
    """Ask node, which runs ponytail's hook, whether `value` is a regular expression and which plumbline agents it reaches
    (`new RegExp(value, 'i').test(agent_type)`); None when node cannot answer."""
    types = [f"plumbline:{name}" for name in ALL_AGENTS]
    try:
        result = subprocess.run([node, "-e", NODE_SCRIPT, "--", value, *types], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 3:
        return False, []
    try:
        flags = json.loads(result.stdout)
    except ValueError:
        return None
    if result.returncode != 0 or not isinstance(flags, list) or len(flags) != len(types):
        return None
    return True, [name for name, hit in zip(ALL_AGENTS, flags) if hit is True]


def matcher_reach(value: str) -> tuple[bool, list[str]]:
    """(whether `value` is a valid regular expression, the plumbline agents it reaches). Javascript's and Python's regular
    expressions differ (`(?P<n>..)` is Python's, `(?<n>..)` is Javascript's), and ponytail's hook is Javascript, so node
    decides where it is installed; Python's re is the fallback."""
    node = shutil.which("node")
    answer = node_reach(node, value) if node else None
    if answer is not None:
        return answer
    try:
        pattern = re.compile(value, re.IGNORECASE)
    except re.error:
        return False, []
    return True, [name for name in ALL_AGENTS if pattern.search(f"plumbline:{name}")]


def matcher_line(project_dirs: list) -> str | None:
    """None when ponytail's subagent matcher reaches exactly plumbline's planner, test-writer and builder among plumbline's
    agents; otherwise one line saying what is wrong and what to set."""
    value = os.environ.get(MATCHER_VAR) or settings_env(project_dirs, MATCHER_VAR)
    setting = f'"env": {{"{MATCHER_VAR}": "{MATCHER_EXAMPLE}"}}'
    if not value:
        return (
            "ponytail reaches every subagent, plumbline's reviewers included. Scope it to the agents that make the change, once: "
            f"add {setting} to ~/.claude/settings.json (plumbline changes none of your settings). "
            "That value keeps ponytail on for every subagent that is not a plumbline agent."
        )
    valid, reached = matcher_reach(value)
    if not valid:
        return f'{MATCHER_VAR} ("{value}") is not a valid regular expression, so ponytail reaches every subagent. Set it to "{MATCHER_EXAMPLE}".'
    if reached == [name for name in ALL_AGENTS if name in MAKING_AGENTS]:
        return None
    missed = [name for name in MAKING_AGENTS if name not in reached]
    extra = [name for name in reached if name not in MAKING_AGENTS]
    said = "; ".join(
        ([f"it misses {', '.join(missed)}"] if missed else []) + ([f"it also reaches {', '.join(extra)}"] if extra else [])
    )
    return f'{MATCHER_VAR} ("{value}") should reach exactly plumbline\'s planner, test-writer and builder: {said}. Set it to "{MATCHER_EXAMPLE}".'


def build_context() -> str:
    parts = [DISCIPLINE_NOTE]
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    root = None
    adopted = False
    try:
        import plumbline

        root = plumbline.git_toplevel(project, timeout=5)
        if root is not None:
            parts.append(adoption_line(plumbline, root))
            adopted = (root / plumbline.CONFIG_FILE).is_file()
    except (Exception, SystemExit) as exc:  # e.g. a Python too old for plumbline.py
        parts.append(one_line(f"plumbline: could not check this repository ({exc})."))
    try:
        dirs = [project] + ([root] if root is not None and root != project else [])
        if not ponytail_enabled(dirs):
            parts.append(PONYTAIL_WARNING)
        elif adopted:  # the matcher matters where plumbline runs its agents: in a repository that has adopted it
            line = matcher_line(dirs)
            if line:
                parts.append(line)
    except Exception:
        pass
    return "\n\n".join(parts)


def main() -> None:
    try:
        context = build_context()
    except BaseException:
        context = DISCIPLINE_NOTE
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
