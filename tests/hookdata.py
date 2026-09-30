"""Hook payloads shaped as Claude Code 2.1.281 sends them. The field names and
their presence are those of the probe's real samples: SubagentStop carries
stop_hook_active, agent_id, agent_type, agent_transcript_path and
last_assistant_message; tool events inside a subagent also carry agent_id and
agent_type, and those in the main thread carry neither."""
import json

SESSION = "d8461d9f-a3ae-42bf-a258-8ca265dacec8"
PROMPT = "7df625bd-9347-4cd4-8cc3-ef737b214fc5"
AGENT = "ae0c08440bb20aca1"


def _common(cwd, event, **fields) -> dict:
    return {
        "session_id": SESSION,
        "transcript_path": f"{cwd}/.claude/projects/slug/{SESSION}.jsonl",
        "cwd": str(cwd),
        "prompt_id": PROMPT,
        "permission_mode": "default",
        "hook_event_name": event,
        **fields,
    }


def stop_payload(cwd, agent_type="plumbline:planner", message="", agent_id=AGENT, active=False, **fields) -> dict:
    """A SubagentStop payload; `message` is the agent's last message."""
    return _common(
        cwd,
        "SubagentStop",
        stop_hook_active=active,
        agent_id=agent_id,
        agent_type=agent_type,
        agent_transcript_path=f"{cwd}/.claude/projects/slug/{SESSION}/subagents/agent-{agent_id}.jsonl",
        last_assistant_message=message,
        background_tasks=[],
        session_crons=[],
        **fields,
    )


def tool_payload(cwd, tool_name, tool_input, agent_type=None, agent_id=AGENT, **fields) -> dict:
    """A PreToolUse payload; a tool used inside a subagent carries agent_id and agent_type."""
    payload = _common(cwd, "PreToolUse", tool_name=tool_name, tool_input=tool_input, tool_use_id="toolu_01ABC", **fields)
    if agent_type is not None:
        payload.update(agent_id=agent_id, agent_type=agent_type)
    return payload


def bash_payload(cwd, command, **fields) -> dict:
    return tool_payload(cwd, "Bash", {"command": command, "description": "run it", "timeout": 120000, "run_in_background": False}, **fields)


def denial(result):
    """The permissionDecisionReason of a hook run that denied, after checking the whole shape; None when it allowed."""
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return None
    payload = json.loads(result.stdout)
    assert list(payload) == ["hookSpecificOutput"]
    output = payload["hookSpecificOutput"]
    assert list(output) == ["hookEventName", "permissionDecision", "permissionDecisionReason"]
    assert output["hookEventName"] == "PreToolUse" and output["permissionDecision"] == "deny"
    assert isinstance(output["permissionDecisionReason"], str) and output["permissionDecisionReason"]
    return output["permissionDecisionReason"]


# --- helpers for the hook-holds tests (plain functions; the fixtures live in the test files)


def start_run(repo, run_id="r1", activate=True):
    """A run has begun in the adopted `repo`, as `plumbline.py plan --intent feature --row code.S` begins it: the intake record measured
    from the repository's own merge base with main, the intake entry in the ledger, and .plumbline/runs/ACTIVE naming the run. With
    `activate=False` the ACTIVE file is left as it was (or absent), as in a repository where another run, or the newest one, is in progress."""
    import contextlib
    import io

    import plumbline as pl

    active = repo / ".plumbline" / "runs" / "ACTIVE"
    before = active.read_bytes() if active.is_file() else None
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pl.main(["plan", "--intent", "feature", "--row", "code.S", "--base", "main", "--run-id", run_id, "--project", str(repo)])
    assert code == 0, f"plan --intent failed: {out.getvalue()}{err.getvalue()}"
    if not activate:
        if before is None:
            active.unlink(missing_ok=True)
        else:
            active.write_bytes(before)
    return run_id


def activate_run(repo, run_id):
    """What `plumbline.py plan --intent` writes: the run id and a newline."""
    path = repo / ".plumbline" / "runs" / "ACTIVE"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run_id + "\n", encoding="utf-8")


def add_origin(repo, tmp_path):
    """A local bare repository as `origin` of `repo`, holding `main`, for pushes that really happen. Returns its path."""
    import subprocess

    from helpers import git

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return origin


def remote_ref(origin, ref):
    """The commit a ref of the bare repository `origin` points to, or None."""
    import subprocess

    result = subprocess.run(["git", "-C", str(origin), "rev-parse", "--verify", "--quiet", ref], capture_output=True, text=True)
    return result.stdout.strip() or None
