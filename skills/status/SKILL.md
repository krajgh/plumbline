---
name: status
description: Show where plumbline's latest run stands in this repository, with its stages and gates, and whether HEAD is covered by a pass or an override. Use when asked how a run is going or whether HEAD can be pushed.
allowed-tools: Bash(python3 *)
---

# plumbline status

The command below ran when this skill loaded (skill shell injection), and its output follows.

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" status
```

Report it as it is: the run, each stage with its state and gate, and whether HEAD is covered. Where HEAD is covered by a pass record, the line `open: 3 findings, 4 gaps` counts what the run left open, and `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" open` lists the findings and gaps. A stage that failed shows why beneath it. When the run has stages that are not `pass`, `supplied` or `recorded`, `/plumbline:run` continues it from the first of them.

For another run, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" status --run <run id>`. For what a run cost, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" tokens <run id>`: it prints the output, fresh input and cache reads of the run's agents, per model, and, apart, of the orchestrator's legs (`orchestration`).
