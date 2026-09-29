#!/usr/bin/env python3
"""SessionStart hook for plumbline.

Prints exactly one JSON object on stdout, {"hookSpecificOutput": {"hookEventName":
"SessionStart", "additionalContext": "..."}}, and always exits 0. The context is:

  - the subagent-discipline note, always;
  - inside a git repository, one line saying whether plumbline is adopted there
    (a plumbline.toml at the top level), not adopted, or has an invalid config;
  - a warning when the ponytail plugin is not enabled in any settings file.

Nothing here may fail the session: every check is isolated, and if one breaks
the note still goes out.
"""
import json
import os
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


def build_context() -> str:
    parts = [DISCIPLINE_NOTE]
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    root = None
    try:
        import plumbline

        root = plumbline.git_toplevel(project, timeout=5)
        if root is not None:
            parts.append(adoption_line(plumbline, root))
    except (Exception, SystemExit) as exc:  # e.g. a Python too old for plumbline.py
        parts.append(one_line(f"plumbline: could not check this repository ({exc})."))
    try:
        dirs = [project] + ([root] if root is not None and root != project else [])
        if not ponytail_enabled(dirs):
            parts.append(PONYTAIL_WARNING)
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
