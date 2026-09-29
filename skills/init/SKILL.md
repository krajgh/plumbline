---
name: init
description: Adopt plumbline in the current git repository by writing plumbline.toml and ignoring .plumbline/ in .gitignore. Adoption is the builder's choice, so only the builder invokes it.
disable-model-invocation: true
allowed-tools: Bash(python3 *)
---

# Adopt plumbline in this repository

Adoption is the builder's decision. This skill runs only when the builder types `/plumbline:init`, and it changes two files in the repository and nothing else.

1. From the repository root, run this command. `init` finds the top level of the repository itself, so a subdirectory also works. If the builder gave arguments (the only one is `--graft`), pass them along unchanged and add none of your own.

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" init
   ```

2. Report what the command printed exactly as it is. If it refuses because `plumbline.toml` already exists, say so and stop: it never overwrites, and you must not delete or edit the existing file to get around it.

3. Remind the builder to commit the two files, `plumbline.toml` and `.gitignore` (only `plumbline.toml` if the repository already ignored `.plumbline/`). Do not commit them yourself: the builder decides when, and plumbline commits nothing.
