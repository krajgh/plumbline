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
- `[matrix.<type>]`: for each type, the `stages` that run, in definition order (a row selects, it never reorders), optionally `lenses` for the review of the diff and a `note`. A row is flat, or split by size as `[matrix.<type>.S]`, `.M` and `.L`. A stage the row includes must find what it reads (an input, or an earlier stage of the row), for every role. A read marked `?` may be absent: `reduce` reads `verify?` and `review?`, so it reduces whatever the row produced, and a repository's row of just `intake` and `reduce` is valid.

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
| `merge-review RUN STAGE [--round N]` | builds a review stage's `review_record` from its prosecutors', defenders' and detective's records, applying the survival rule |
| `gate RUN STAGE` | evaluates the stage's gate mechanically; exit 0 if it passes, 1 if not; the evaluation is entered in the ledger |
| `tokens RUN` | prints, as JSON, the output, fresh input and cache reads of the run's agents per model |
| `pass RUN` | writes the `pass_record` for HEAD, if the working tree is clean and every gate of the run passed |
| `override --reason TEXT [--run RUN]` | writes an override record for HEAD (a reason of at least 20 characters); only when the builder asks for it |
| `status [--run RUN]` | shows the latest run, its stages and gates, and whether HEAD is covered |

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
| `review_record` | a review unit, by `merge-review` | findings, defenses, survivors, gaps |
| `findings_record` | each prosecutor | one lens and its findings |
| `defense_record` | each defender | one defender's verdict on each finding |
| `gaps_record` | the detective | what is missing, once no blocker stands |
| `pass_record` | reduce | stage results, tokens, the verdict |
| `override_record` | the builder | a push without a passing run, with a reason |

## Runs, gates and the pass record

A run is the directory `.plumbline/runs/<run-id>/` in the checkout:

```
.plumbline/runs/<run-id>/<stage-id>.json                    the stage's record
.plumbline/runs/<run-id>/<stage-id>/round-<n>/<name>.json   a review unit's per-agent records
.plumbline/runs/<run-id>/ledger.jsonl                       only ever appended to
.plumbline/pass/<HEAD>.json                                 the pass record that lets HEAD be pushed
.plumbline/pass/<HEAD>.override.json                        or an override record
```

**Gates** are mechanical. `gate RUN STAGE` first requires the stage's record to exist and validate, then checks it: `spec_complete` (at least one acceptance criterion, each with a test plan entry), `acs_covered` (each criterion appears in some test's `ac_ids`), `tests_fail_on_stub` (the stub check ran and every test failed on an assertion), `verify_green` (`green`), `no_surviving_blockers` (`blockers_surviving` is 0), and `all_gates_passed` (every other stage of the run's row has a valid record and passed its gate at its last evaluation in the ledger, with its record unchanged since).

**A review unit** writes one record per agent under `<stage-id>/round-<n>/`, and `merge-review` builds the stage's record. A finding survives when at least `survive_if_unrefuted_by` of the stage's `defenders` did not refute it (a majority when the stage sets none). A defender refutes only with a verdict of `refuted` and a quote of the code; silence, a concession, or a refutation without a quote does not count. A stage without defenders lets every finding survive. Duplicate finding ids, a missing lens and a defense of an unknown finding are errors, never silently repaired.

**`pass RUN`** needs a clean working tree apart from `.plumbline/`, so commit first. It evaluates every gate afresh, then writes the `pass_record` (with the tokens of the run's agents) to the run and to `.plumbline/pass/<HEAD>.json`. **`override --reason`** writes `.plumbline/pass/<HEAD>.override.json` instead, and never overwrites one.

## Hooks

`hooks/hooks.json` registers three hooks, each command ending in `|| true`. All three are silent, and allow everything, in a repository without `plumbline.toml`, and on any error.

- **SubagentStop**, for plumbline agents only (`plumbline:planner`, `test-writer`, `builder`, `verifier`, `prosecutor`, `defender`, `detective`): the agent's final reply must end with `RECORD: <path>`, a record under `.plumbline/runs/<run-id>/` that validates against that agent's record type. Until it does, the stop is blocked, at most 3 times; then the agent is let go and the ledger marks the record invalid. Every stop that is let go is entered in `ledger.jsonl` with the agent's id, type, stage, record and transcript path. Because `|| true` turns exit status 2 into 0, a block is delivered as `{"decision": "block", "reason": ...}` on stdout (the script also writes the reason to stderr and exits 2).
- **PreToolUse on Bash**: `git push` and `gh pr create` are denied unless HEAD has a valid pass or override record. `git commit` is denied when what it would commit adds a symlink, an absolute home path (a `home` or `Users` directory at the root, then a user name and a slash), or a key-shaped secret (`sk-ant-` and 20 more characters). The command line is parsed, so `git push --dry-run` and `echo git push` are not pushes. A push in the same command line as a commit is denied, because the new commit cannot have a record yet.
- **PreToolUse on Read, Grep and Glob, for `plumbline:builder` only**: the builder works blind to the tests. A path matching the pipeline's `tests` type, or listed in the newest run's tests record (that record itself included), is denied, and so is a Grep or Glob without an explicit path, or with a path that leads to a directory holding tests.

A deny uses `permissionDecision: "deny"`, which the model sees as `PreToolUse:<Tool> hook error: <reason>`.

## Status

Phase 2a of 5, the enforcement core: on top of the foundation, the gates, the review-unit records and `merge-review`, `pass`, `override`, `status` and `tokens`, and the hooks that validate agents' records, gate pushes, check commits and blind the builder. The agents and the `/plumbline:run` recipe that drives a pipeline arrive in phase 2b, graft and CI later. Not yet published. The tests run with `uv run --with pytest pytest -q`.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.
