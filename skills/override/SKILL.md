---
name: override
description: Record an override for HEAD, so that it can be pushed without a passing pipeline run. Typed by the builder with a reason of at least 20 characters; only the builder invokes it.
disable-model-invocation: true
argument-hint: why the pipeline is skipped (at least 20 characters)
allowed-tools: Bash(python3 *)
---

# Override the pipeline for HEAD

The builder typed this command to let HEAD be pushed without a passing run. It ran before this text reached you: the command below is skill shell injection, so Claude Code ran it and put its output here, and no tool call of yours was involved. The builder's reason went in through a quoted here-document, so no character of it was read as shell syntax.

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" override --reason - <<'PLUMBLINE_OVERRIDE_REASON'
$ARGUMENTS
PLUMBLINE_OVERRIDE_REASON
```

The builder's reason: $ARGUMENTS

Tell the builder what the command reported: the commit it covers, where the record is, and which stages of the latest run did not pass. Then stop. The push is the builder's next step, and the override record is in `.plumbline/pass/`, which only `plumbline.py` writes.
