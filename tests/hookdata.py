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
