# plumbline

A Claude Code plugin: a development harness in four parts.

1. **The subagent discipline.** Work goes through subagents sized to the job. The main session keeps the brief, the review and the decisions, and alone writes the files the whole project shares. It ships as the `plumbline:subagent-discipline` skill and a short note at every session start.
2. **A typed, adversarial pipeline, defined as data.** Plan, tests, a test-review loop, build, verify, a review loop (prosecutors, then defenders, then a detective), and a reduce in the main session. Every stage writes a typed JSON record. The definition is `pipeline/default.toml`, and a repository can override parts of it.
3. **ponytail as a dependency**, applied to the making side: the planner, the test-writer and the builder.
4. **graft as an optional per-repository switch**, off by default.

## Install

plumbline depends on the ponytail plugin, which lives in its own marketplace. Add that marketplace first, then plumbline's, then install:

```
claude plugin marketplace add DietrichGebert/ponytail
claude plugin marketplace add krajgh/plumbline
claude plugin install plumbline@plumbline
```

plumbline's marketplace allows the cross-marketplace dependency, so installing plumbline brings ponytail with it. If ponytail is not enabled in any settings file, the session-start note says so.

## Adopt a repository

Inside a git repository, run `/plumbline:init`. It writes `plumbline.toml` at the repository root and adds `.plumbline/` to `.gitignore`, tells you what it changed, and commits nothing: commit both files yourself. It never overwrites an existing `plumbline.toml`. `/plumbline:init --graft` records `enabled = true` for graft, which arrives in a later phase.

Without `plumbline.toml`, plumbline gives only the discipline note, plus a one-line hint inside git repositories, and gates nothing. Run records go in `.plumbline/runs/<run-id>/<stage-id>.json`, inside the checkout.

## The pipeline file

`pipeline/default.toml` is plain TOML (top-level keys come before any table):

- `schema`, `name`, `inputs` (what exists before any stage runs), `generated` (globs whose lines are not counted), and `precedence` (when a change touches several file types, the first type listed decides its row).
- `[sizes]`: the changed-line limits for size S and size M. More than M is L.
- `[[type]]`: file types with glob `paths`, tried in order; a file takes the first that matches. The last type must match every path.
- `[[stage]]`, in the order they run. An agent stage has a `role`. A review stage has `kind = "review"`, a `target`, and `lenses`, and no role. Both have `reads` (a trailing `?` makes a read optional), a `record` type, and optionally a `gate`, an `on_fail` stage to return to (an earlier agent stage, or `main`) with `max_rounds`.
- `[matrix.<type>]`: for each type, the `stages` that run, in definition order (a row selects, it never reorders), optionally `lenses` for the review of the diff and a `note`. A row is flat, or split by size as `[matrix.<type>.S]`, `.M` and `.L`. A stage the row includes must find what it reads (an input, or an earlier stage of the row); the one exception is a stage run by the main session, such as `reduce`, which reduces whatever the row produced, so a row without a stage it reads draws a note instead.

Globs are anchored at the repository root. `**` matches zero or more directories, `*` and `?` stay within one path segment, and `LICENSE` matches only the root file.

The command line, `python3 scripts/plumbline.py`:

| Command | What it does |
| --- | --- |
| `validate-pipeline [FILE] [--project PATH]` | validates the default pipeline (or FILE), merged with the repository's `plumbline.toml` when `--project` is given |
| `classify [--project PATH] [--base REF] [--out FILE]` | writes the `change_class` record: the diff from the merge base to the working tree, plus untracked files |
| `plan [--project PATH] [--base REF] [--run-id ID]` | prints, as JSON, the row and its ordered stages with resolved record paths |
| `check-record TYPE FILE` | validates a record against its schema |
| `render FILE [--type TYPE]` | prints a record as markdown |
| `init [--project PATH] [--graft]` | adopts a repository |

### Repository overrides

A repository's `plumbline.toml` can replace `precedence`, add `[[type]]` entries (tried before the pipeline's own), and add or replace `[matrix.<type>]` rows. For example, a repository with its own model evals can send changes under `voice/` through those evals instead of the pipeline:

```toml
schema = 1
pipeline = "default"        # a pipeline shipped in the plugin's pipeline/, or a repo-relative path

precedence = ["voice", "code", "config", "tests", "docs"]

[graft]
enabled = false

[[type]]
id = "voice"
paths = ["voice/**"]

[matrix.voice]
stages = ["intake", "reduce"]
note = "Voice and prompt changes go through this repository's own model evals."
```

Keep `precedence` above the first table, and make it name every type, the new one included. `plumbline.py validate-pipeline --project .` reports any problem in the merged result.

## Records

Every stage writes one record. The schemas in `schemas/` use a small JSON Schema subset, and `check-record` reports every error with its JSON path.

| Record | Written by | Holds |
| --- | --- | --- |
| `change_class` | intake | the changed files, their types, the size, and the row |
| `spec` | planner | goal, acceptance criteria, interfaces, test plan, risks, a split proposal at size L |
| `tests_record` | test-writer | the tests, which criteria they cover, the stub check |
| `build_note` | builder | files changed, criteria addressed, assumptions |
| `verify_record` | verifier | commands run, test counts, failing criteria, mechanical checks |
| `review_record` | a review unit | findings, defenses, survivors, gaps |
| `pass_record` | reduce | stage results, tokens, the verdict |
| `override_record` | the builder | a push without a passing run, with a reason |

## Status

Phase 1 of 5, the foundation: the repository, the pipeline definition and its validator, the record schemas, the command line, session start, and `/plumbline:init`. Agents, gates, graft and CI arrive in later phases; nothing here yet runs a pipeline or blocks a push. Not yet published. The tests run with `uv run --with pytest pytest -q`.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.
