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

2. Report what the command printed exactly as it is. If it refuses because `plumbline.toml` already exists, say so and stop: `init` leaves an existing file as it is, and so do you. The builder edits that file if it should change.

3. Remind the builder to commit the two files, `plumbline.toml` and `.gitignore` (only `plumbline.toml` if the repository already ignored `.plumbline/`). The builder decides when to commit, and plumbline commits nothing itself.

4. Tell the builder that `plumbline.toml` holds a commented `[commands]` table. The verify stage and the tests stage need `test = "..."` there, because `plumbline.py gate` runs the repository's test command itself; `lint`, `typecheck` and `build` join the verify run where the repository has them.
