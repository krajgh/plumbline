#!/usr/bin/env python3
"""SubagentStop hook for plumbline.

Reads the hook's JSON on stdin. It acts only for a plumbline agent (agent type
`plumbline:<name>`) inside a repository that has adopted plumbline. Such an agent
must end its report with a line `RECORD: <path>`, naming a JSON record that
validates against the record type of that agent (a planner writes a spec, a
prosecutor a findings_record, and so on) and that sits where that agent's records
belong: in the run in progress (the one .plumbline/runs/ACTIVE names, else the
newest run), at the path of a stage of the run's row that the agent's role serves
(`<stage>.json`), or, for the three review roles, at
`<review stage>/round-<n>/<name>.json`.

The report is the agent's final message. When that message has no RECORD line, the
hook looks for the line in the report the agent handed back through SubagentHandback:
the `message` of the last such call in the agent's transcript (`agent_transcript_path`).

Until the agent names a good record, the stop is blocked, at most 3 times, counted per
agent id. Then the agent is let go and the record is marked invalid in the ledger. Every
stop that is let go is entered in the run's ledger.jsonl: agent id, type, stage, record
and its sha256 at that moment, whether it is valid, and where the agent's transcript is
(`tokens` reads it). The sha256 is what lets the gates tell later that the record is
still the one the agent left. A stop that leaves what the agent's latest entry already
holds (the same record, hash and validity) adds no entry: an agent that the harness asks
again for its report stops again, and is still one agent, in one round. An agent of an
unknown `plumbline:` role is let go, and entered as invalid ("unknown plumbline role").

A block is delivered in every way Claude Code accepts, because the hook is
registered with `|| true`, which turns exit status 2 into 0: the reason goes to
stderr, to stdout as {"decision": "block", "reason": ...}, and the exit status is
2. Through `|| true` it is the JSON that blocks; run bare, it is the exit status.

Nothing else may hold an agent up: any error, any other agent, and any repository
without a plumbline.toml let the stop go, silently, with exit status 0. A problem the
agent cannot mend (the configuration is invalid, the run has no usable intake record)
does not hold it up either: the stop is let go and the record entered as invalid.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_BLOCKS = 3
MAX_LISTED = 20
RECORD_LINE = re.compile(r"^RECORD:\s*(\S.*?)\s*$")
CODE_FENCE = re.compile(r"^(`{3,}|~{3,})[\w-]*$")
HANDBACK_TOOL = "SubagentHandback"  # the tool through which the harness has an agent deliver its report
MAX_TRANSCRIPT_BYTES = 2 << 20  # how much of the end of an agent's transcript is read for that report


def record_line(text) -> str | None:
    """The path on the last non-empty line, when that line is `RECORD: <path>`. Markdown emphasis around the label
    and code quotes around the path are tolerated, and so is a closing code fence after the line (the prompts show
    the line inside a fenced block, and an agent may copy the fence too)."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    while lines and CODE_FENCE.match(lines[-1]):
        lines.pop()
    if not lines:
        return None
    match = RECORD_LINE.match(lines[-1].replace("*", ""))
    if not match:
        return None
    return match.group(1).strip("`'\"<> ") or None


def transcript_tail(named) -> list[str]:
    """The lines of the last MAX_TRANSCRIPT_BYTES bytes of the transcript file `named`, oldest first. What is no readable regular
    file (a missing path, a directory, a pipe, a value that is no path) gives no lines."""
    try:
        path = Path(named)
        if not path.is_file():
            return []
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - MAX_TRANSCRIPT_BYTES)
            handle.seek(max(0, start - 1))  # one byte early: that byte says whether a line begins exactly at `start`
            data = handle.read(MAX_TRANSCRIPT_BYTES + 1)
    except (OSError, TypeError, ValueError):
        return []
    lines = data.decode("utf-8", "replace").split("\n")
    return lines[1:] if start else lines  # a cut may have torn the first line, which is left out


def content_blocks(record) -> list:
    """The content blocks of a transcript record: those of its message, or, without one, of the record itself."""
    if not isinstance(record, dict):
        return []
    message = record.get("message")
    content = (message if isinstance(message, dict) else record).get("content")
    return content if isinstance(content, list) else []


def handback_message(named) -> str | None:
    """The `message` of the last SubagentHandback call in the transcript `named`, read from its end; None when there is no
    such call or it carries no text."""
    for line in reversed(transcript_tail(named)):
        if HANDBACK_TOOL not in line:  # tool results and prose fill most lines: only a line that names the tool is parsed
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        for block in reversed(content_blocks(record)):
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == HANDBACK_TOOL:
                given = block.get("input")
                message = given.get("message") if isinstance(given, dict) else None
                return message if isinstance(message, str) else None
    return None


def handback_record_line(named) -> str | None:
    """The path on the last line of the report that the agent handed back through SubagentHandback, read from its transcript.
    The transcript is a fallback, so one that is missing, torn or unlike what is expected names nothing and raises nothing."""
    try:
        return record_line(handback_message(named))
    except Exception:
        return None


def placement_problems(pl, root: Path, run_id: str | None, parts: tuple[str, ...], role: str) -> tuple[list[str], list[str], str | None]:
    """Where in the run the record sits, as (problems the agent can mend, problems it cannot, the stage the path names).
    `parts` is the path below .plumbline/runs/. The run is the one in progress, and the path must be a stage's record of
    that run's row that this agent's role serves."""
    if run_id is None:
        return ["no run is in progress: `plumbline.py plan --intent ID` starts one"], [], None
    if parts[0] != run_id:
        return [f"the record must be in the run in progress, '{run_id}' (you named run '{parts[0]}'); write it under {pl.RUNS_DIR}/{run_id}/"], [], None
    project = pl.load_project(root)
    if project.pipeline is None or project.errors:
        return [], ["the pipeline configuration is invalid, so the place of the record in the run was not checked"], None
    try:
        run = pl.load_run(project, run_id)
    except pl.PlumblineError as exc:
        return [], [f"the place of the record in the run was not checked: {exc}"], None
    review_role = role in ("prosecutor", "defender", "detective")
    stage_id = parts[1][: -len(".json")] if len(parts) == 2 and parts[1].endswith(".json") else parts[1] if len(parts) > 2 else None
    mine = [s for s in run.stages if (s.get("kind", "agent") == "review") == review_role and (review_role or s.get("role") == role)]
    if stage_id not in [s["id"] for s in mine]:
        where = ", ".join(f"{pl.RUNS_DIR}/{run_id}/{s['id']}{'/round-<n>/<name>' if review_role else ''}.json" for s in mine)
        return [f"a {role} writes its record at {where or 'a stage of the run that its role serves (this run has none)'}, not at {pl.RUNS_DIR}/{'/'.join(parts)}"], [], None
    if review_role:
        if len(parts) != 4 or not re.fullmatch(r"round-[1-9][0-9]*", parts[2]) or not parts[3].endswith(".json"):
            return [f"a {role}'s record belongs at {pl.RUNS_DIR}/{run_id}/{stage_id}/round-<n>/<name>.json, not at {pl.RUNS_DIR}/{'/'.join(parts)}"], [], stage_id
    elif len(parts) != 2:
        return [f"a {role}'s record belongs at {pl.RUNS_DIR}/{run_id}/{stage_id}.json, not at {pl.RUNS_DIR}/{'/'.join(parts)}"], [], stage_id
    return [], [], stage_id


def locate(pl, root: Path, cwd: Path, named: str, record_type: str, role: str) -> dict:
    """Check the record an agent named. The result holds its problems (those the agent can mend, and those it cannot), the stage
    the path names, the path relative to the root, its sha256, and a state: "unusable" for a path that is no record of a run,
    "invalid" for a record that does not validate, "misplaced" for a valid record in the wrong place, "ok"."""
    path = Path(named)
    candidates = [path] if path.is_absolute() else [cwd / path, root / path]
    found = next((c for c in candidates if c.is_file()), candidates[0])
    result = {"problems": [], "unmendable": [], "stage": None, "where": pl.rel_path(root, found), "sha256": None, "state": "unusable"}
    runs = (root / pl.RUNS_DIR).resolve()
    try:
        parts = found.resolve().relative_to(runs).parts
    except ValueError:
        result["problems"] = [f"the record must be inside {pl.RUNS_DIR}/<run-id>/ (you named {named})"]
        return result
    if len(parts) < 2 or not pl.RUN_ID_PATTERN.fullmatch(parts[0]) or not parts[-1].endswith(".json"):
        result["problems"] = [f"the record must be a .json file inside {pl.RUNS_DIR}/<run-id>/ (you named {named})"]
        return result
    result["sha256"] = pl.file_sha256(found)
    placed, result["unmendable"], result["stage"] = placement_problems(pl, root, pl.active_run_id(root), parts, role)
    data, problem = pl.load_json_file(found)
    record_problems = [f"{result['where']}: {problem}"] if problem else [f"{result['where']}: {error}" for error in pl.check_record(record_type, data)]
    result["problems"] = record_problems + placed
    result["state"] = "invalid" if record_problems else "misplaced" if placed else "ok"
    return result


def counter_file(root: Path, agent_id) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id if isinstance(agent_id, str) else "") or "unknown"
    return root / ".plumbline" / "blocks" / name


def read_count(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def block_message(record_type: str, named, problems: list[str], attempt: int, state: str) -> str:
    many = f"{len(problems)} problem{'' if len(problems) == 1 else 's'}"
    if named is None:
        head = (
            "plumbline: your report must end with a line of the form `RECORD: <path>`, "
            f"naming the {record_type} record you wrote under .plumbline/runs/<run-id>/. "
            "Put that line last in the whole report: in the message of your SubagentHandback call when the harness asks for "
            "your report through it, and in your final message when that call is refused."
        )
    elif state == "misplaced":
        head = (
            f"plumbline: the record you named is a valid {record_type}, but it is not where this agent's record belongs ({many}). "
            "Write it there, then end your report with the line `RECORD: <path>` again."
        )
    elif state == "unusable":
        head = f"plumbline: the path you named is no record of a run ({many}). Write your record where it belongs, then end your report with the line `RECORD: <path>` again."
    else:
        head = f"plumbline: the record you named is not a valid {record_type} ({many}). Fix the file, then end your report with the line `RECORD: <path>` again."
    lines = [head] + [f"- {p}" for p in problems[:MAX_LISTED]]
    if len(problems) > MAX_LISTED:
        lines.append(f"- and {len(problems) - MAX_LISTED} more")
    lines.append(f"(attempt {attempt} of {MAX_BLOCKS}: after that you are let go and the record is marked invalid)")
    return "\n".join(lines)


def ledger_entry(data: dict, agent_type: str, record_type, valid: bool, blocks: int, stage=None, where=None, sha=None, errors=None) -> dict:
    entry = {
        "kind": "agent",
        "agent_id": data.get("agent_id"),
        "agent_type": agent_type,
        "stage": stage,
        "record": where,
        "record_type": record_type,
        "record_sha256": sha,
        "valid": valid,
        "blocks": blocks,
        "session_id": data.get("session_id"),
        "transcript": data.get("agent_transcript_path"),
        "session_transcript": data.get("transcript_path"),
    }
    if errors:
        entry["errors"] = errors[:MAX_LISTED]
    return entry


def enter(pl, root: Path, run_id: str, entry: dict) -> None:
    """Append a stop to the run's ledger, unless the agent's latest entry holds what this stop would add: the same record with
    the same hash and validity. An agent that stops again with the record it left (the harness asks it again for a report it
    has not delivered) stays one entry, and so one round; one that changes its record, or whether it is valid, is entered
    again. A stop without an agent id cannot be matched to an earlier one, and is always appended."""
    agent_id = entry.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        last = pl.latest_entry(pl.read_ledger(root, run_id), "agent", agent_id=agent_id)
        if last is not None and all(last.get(key) == entry.get(key) for key in ("record", "record_sha256", "valid")):
            return
    pl.append_ledger(root, run_id, entry)


def decide(data) -> str | None:
    """The reason to block this stop, or None to let the agent go. Writes the
    ledger entry and keeps the count of blocks, as described above."""
    if not isinstance(data, dict):
        return None
    agent_type = data.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.startswith("plumbline:"):
        return None
    import plumbline as pl

    role = agent_type[len(pl.AGENT_PREFIX):]
    record_type = pl.AGENT_RECORDS.get(role)
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    root = pl.git_toplevel(Path(cwd), timeout=5)
    if root is None or not (root / pl.CONFIG_FILE).is_file():
        return None  # not adopted here: gate nothing
    if record_type is None:  # a plumbline agent this plugin does not know: let it go, and say so in the run's ledger
        active = pl.active_run_id(root)
        if active is not None:
            enter(pl, root, active, ledger_entry(data, agent_type, None, False, 0, errors=["unknown plumbline role"]))
        return None

    counter = counter_file(root, data.get("agent_id"))
    blocks = read_count(counter)  # counted per agent id: an agent's stops add up, whatever stop_hook_active says
    named = record_line(data.get("last_assistant_message"))
    if named is None:  # the report may have gone through SubagentHandback: its message is the second place to look
        named = handback_record_line(data.get("agent_transcript_path"))
    if named is None:
        found = {"problems": ["no RECORD line at the end of the final reply"], "unmendable": [], "stage": None, "where": None, "sha256": None, "state": "unusable"}
    else:
        found = locate(pl, root, Path(cwd), named, record_type, role)
    problems = found["problems"]

    if problems and blocks < MAX_BLOCKS:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(blocks + 1), encoding="utf-8")
        return block_message(record_type, named, problems, blocks + 1, found["state"])

    counter.unlink(missing_ok=True)
    errors = problems + found["unmendable"]
    run_id = pl.active_run_id(root)  # the stop belongs to the run in progress, whatever the agent named
    if run_id is not None:
        enter(
            pl, root, run_id,
            ledger_entry(data, agent_type, record_type, not errors, blocks, found["stage"], found["where"], found["sha256"], errors),
        )
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
