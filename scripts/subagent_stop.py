#!/usr/bin/env python3
"""SubagentStop hook for plumbline.

Reads the hook's JSON on stdin. It acts only for a plumbline agent (agent type
`plumbline:<name>`) inside a repository that has adopted plumbline. Such an agent
must end its final reply with a line `RECORD: <path>`, naming a JSON record under
.plumbline/runs/<run-id>/ that validates against the record type of that agent
(a planner writes a spec, a prosecutor a findings_record, and so on).

Until it does, the stop is blocked, at most 3 times. Then the agent is let go and
the record is marked invalid in the ledger. Every stop that is let go is entered
in the run's ledger.jsonl: agent id, type, stage, record, whether it is valid, and
where the agent's transcript is (`tokens` reads it).

A block is delivered in every way Claude Code accepts, because the hook is
registered with `|| true`, which turns exit status 2 into 0: the reason goes to
stderr, to stdout as {"decision": "block", "reason": ...}, and the exit status is
2. Through `|| true` it is the JSON that blocks; run bare, it is the exit status.

Nothing else may hold an agent up: any error, any other agent, and any repository
without a plumbline.toml let the stop go, silently, with exit status 0.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_BLOCKS = 3
MAX_LISTED = 20
RECORD_LINE = re.compile(r"^RECORD:\s*(\S.*?)\s*$")


def record_line(text) -> str | None:
    """The path on the last non-empty line, when that line is `RECORD: <path>`.
    Markdown emphasis around the label and code quotes around the path are tolerated."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return None
    match = RECORD_LINE.match(lines[-1].replace("*", ""))
    if not match:
        return None
    return match.group(1).strip("`'\"<> ") or None


def locate(pl, root: Path, cwd: Path, named: str, record_type: str):
    """Check the record an agent named: (problems, run id, stage id, record path
    relative to the root). The run and the stage come from the path, which must be
    .plumbline/runs/<run>/<stage>.json or .plumbline/runs/<run>/<stage>/.../<name>.json."""
    path = Path(named)
    candidates = [path] if path.is_absolute() else [cwd / path, root / path]
    found = next((c for c in candidates if c.is_file()), candidates[0])
    runs = (root / pl.RUNS_DIR).resolve()
    try:
        parts = found.resolve().relative_to(runs).parts
    except ValueError:
        return [f"the record must be inside {pl.RUNS_DIR}/<run-id>/ (you named {named})"], None, None, None
    if len(parts) < 2 or not pl.RUN_ID_PATTERN.fullmatch(parts[0]) or not parts[-1].endswith(".json"):
        return [f"the record must be a .json file inside {pl.RUNS_DIR}/<run-id>/ (you named {named})"], None, None, None
    run_id = parts[0]
    stage = parts[1][: -len(".json")] if len(parts) == 2 else parts[1]
    where = pl.rel_path(root, found)
    data, problem = pl.load_json_file(found)
    if problem:
        return [f"{where}: {problem}"], run_id, stage, where
    errors = pl.check_record(record_type, data)
    return [f"{where}: {error}" for error in errors], run_id, stage, where


def counter_file(root: Path, agent_id) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id if isinstance(agent_id, str) else "") or "unknown"
    return root / ".plumbline" / "blocks" / name


def read_count(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def block_message(record_type: str, named, problems: list[str], attempt: int) -> str:
    if named is None:
        head = (
            "plumbline: your final reply must end with a line of the form `RECORD: <path>`, "
            f"naming the {record_type} record you wrote under .plumbline/runs/<run-id>/. "
            "That line must be the last line of your final message itself, not only of a hand-back report."
        )
    else:
        head = (
            f"plumbline: the record you named is not a valid {record_type} ({len(problems)} problem"
            f"{'' if len(problems) == 1 else 's'}). Fix the file, then end your final reply with the line `RECORD: <path>` again."
        )
    lines = [head] + [f"- {p}" for p in problems[:MAX_LISTED]]
    if len(problems) > MAX_LISTED:
        lines.append(f"- and {len(problems) - MAX_LISTED} more")
    lines.append(f"(attempt {attempt} of {MAX_BLOCKS}: after that you are let go and the record is marked invalid)")
    return "\n".join(lines)


def decide(data) -> str | None:
    """The reason to block this stop, or None to let the agent go. Writes the
    ledger entry and keeps the count of blocks, as described above."""
    if not isinstance(data, dict):
        return None
    agent_type = data.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.startswith("plumbline:"):
        return None
    import plumbline as pl

    record_type = pl.AGENT_RECORDS.get(agent_type[len(pl.AGENT_PREFIX):])
    cwd = data.get("cwd")
    if record_type is None or not isinstance(cwd, str) or not cwd:
        return None
    root = pl.git_toplevel(Path(cwd), timeout=5)
    if root is None or not (root / pl.CONFIG_FILE).is_file():
        return None  # not adopted here: gate nothing

    counter = counter_file(root, data.get("agent_id"))
    blocks = 0 if data.get("stop_hook_active") is False else read_count(counter)  # a fresh stop starts a fresh count
    named = record_line(data.get("last_assistant_message"))
    if named is None:
        problems, run_id, stage, where = ["no RECORD line at the end of the final reply"], None, None, None
    else:
        problems, run_id, stage, where = locate(pl, root, Path(cwd), named, record_type)

    if problems and blocks < MAX_BLOCKS:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(blocks + 1), encoding="utf-8")
        return block_message(record_type, named, problems, blocks + 1)

    counter.unlink(missing_ok=True)
    if run_id is not None:
        entry = {
            "kind": "agent",
            "agent_id": data.get("agent_id"),
            "agent_type": agent_type,
            "stage": stage,
            "record": where,
            "record_type": record_type,
            "valid": not problems,
            "blocks": blocks,
            "session_id": data.get("session_id"),
            "transcript": data.get("agent_transcript_path"),
            "session_transcript": data.get("transcript_path"),
        }
        if problems:
            entry["errors"] = problems[:MAX_LISTED]
        pl.append_ledger(root, run_id, entry)
    return None


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except BaseException:  # a hook must never crash, and must not hold anything up
        return 0
    if reason is None:
        return 0
    sys.stderr.write(reason + "\n")
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 0
    sys.exit(code)
