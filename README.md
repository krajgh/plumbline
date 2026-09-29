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

### Scope ponytail to the agents that make the change (once)

ponytail injects its ruleset into every subagent through a `SubagentStart` hook (`hooks/ponytail-subagent.js`). It scopes that when the environment variable `PONYTAIL_SUBAGENT_MATCHER` holds a regular expression: only a subagent whose `agent_type` matches it (unanchored, case-insensitive) gets the ruleset; unset, an invalid expression or an unreported type means every subagent. plumbline wants ponytail on its planner, test-writer and builder (the agents that write), and off its verifier and reviewers. Set it once, in the `env` block of `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{"env": {"PONYTAIL_SUBAGENT_MATCHER": "^plumbline:(planner|test-writer|builder)$"}}
```

Every session start checks this, in the process environment and in the `env` block of the user, project and local settings files, and says what to change until the matcher reaches exactly those three agents. plumbline writes no settings itself. The matcher applies to every subagent of the session, plumbline's or not, so other agents no longer get the ruleset while it is set.

## Adopt a repository

Inside a git repository, run `/plumbline:init`. It writes `plumbline.toml` at the repository root and adds `.plumbline/` to `.gitignore`, tells you what it changed, and commits nothing: commit both files yourself. It never overwrites an existing `plumbline.toml`. `/plumbline:init --graft` records `enabled = true` for graft, which arrives in a later phase.

Without `plumbline.toml`, plumbline gives only the discipline note, plus a one-line hint inside git repositories, and gates nothing. Run records go in `.plumbline/runs/<run-id>/<stage-id>.json`, inside the checkout.

`plumbline.toml` also holds the commands the agents may run, each a command prefix (or a list of prefixes):

```toml
[commands]
test = "python3 -m pytest"
lint = "ruff check"
typecheck = "mypy ."
build = "make build"
```

The verifier needs `test`. The verifier's Bash runs only these commands, read-only git and search tools, and `plumbline.py check-diff`; the test-writer's runs the `test` command, read-only git and search tools.

## The pipeline file

`pipeline/default.toml` is plain TOML (top-level keys come before any table):

- `schema`, `name`, `inputs` (what exists before any stage runs), `generated` (globs whose lines are not counted), and `precedence` (when a change touches several file types, the first type listed decides its row).
- `[sizes]`: the changed-line limits for size S and size M. More than M is L.
- `[[type]]`: file types with glob `paths`, tried in order; a file takes the first that matches. The last type must match every path.
- `[[stage]]`, in the order they run. An agent stage has a `role`. A review stage has `kind = "review"`, a `target`, and `lenses`, and no role. Both have `reads` (a trailing `?` makes a read optional), a `record` type, and optionally a `gate`, an `on_fail` stage to return to (an earlier agent stage, or `main`) with `max_rounds`.
- `[matrix.<type>]`: for each type, the `stages` that run, in definition order (a row selects, it never reorders), optionally `lenses` for the review of the diff and a `note`. A row is flat, or split by size as `[matrix.<type>.S]`, `.M` and `.L`. A stage the row includes must find what it reads (an input, or an earlier stage of the row), for every role. A read marked `?` may be absent: `reduce` reads `verify?` and `review?`, so it reduces whatever the row produced, and a repository's row of just `intake` and `reduce` is valid.

- `[roles.<agent>]`: what each of the seven agents may do. `writes` lists write targets: `record` (the agent's own records under `.plumbline/runs/`), `tests` (the paths of the `tests` type) and `code` (everything else, except `.plumbline/`, `plumbline.toml` and the pipeline file). `commands` lists command classes: `test`, `lint`, `typecheck` and `build` (the repository's `[commands]`), `git-read` (diff, show, log, status, rev-parse, merge-base, ls-files, grep, blame), `search` (grep, rg, cat, head, tail, wc, `sed -n`, ls, and `find` without `-exec` or `-delete`), `plumbline-check` (`plumbline.py check-diff`) and `graft` (the graft wrapper, where graft is on). A role with no commands has no Bash. Every agent may also run `plumbline.py check-record` to check its record. An unknown target, class or role is a validation error.
- `[intent.<id>]`: why a change is made, next to its type and size (below).

Globs are anchored at the repository root. `**` matches zero or more directories, `*` and `?` stay within one path segment, and `LICENSE` matches only the root file.

### Intents

The row selects stages; the intent then removes stages (`skip`), or supplies the record a removed stage would have written (`supplies`), and may replace a stage's gate (`gates`) and the lenses of the review of the diff (`lenses`).

| Intent | Skips | Supplies | Also |
| --- | --- | --- | --- |
| `feature` (the default) | nothing | nothing | the planner writes the spec |
| `spec-supplied` | `plan` | the spec, from `--spec FILE` | |
| `fix` | `plan` | a one-criterion spec, from `--spec FILE` | the tests' gate is `reproduces_on_head`: they must fail on today's code, on an assertion |
| `refactor` | `plan`, `tests`, `test-review` | the shipped template spec (`pipeline/templates/refactor.json`): behaviour unchanged, every existing test passes | review lenses `correctness` and `boundaries` |
| `review-only` | `plan`, `tests`, `test-review`, `build` | nothing | a change that already exists: nothing is built |

After an intent is applied to a row, every required read must be produced by a stage left in the row or listed in `supplies`; an intent may name only known stages, gates and lenses. A supplied spec is validated against the `spec` schema and must pass `spec_complete`, like the record of the stage it stands for. The intake record carries the intent, and `pass` lists a supplied stage with 0 rounds.

The command line, `python3 scripts/plumbline.py`:

| Command | What it does |
| --- | --- |
| `validate-pipeline [FILE] [--project PATH]` | validates the default pipeline (or FILE), merged with the repository's `plumbline.toml` when `--project` is given |
| `classify [--project PATH] [--base REF] [--intent ID] [--row ROW] [--out FILE]` | writes the `change_class` record: the diff from the merge base to the working tree, plus untracked files, and the intent. `--row` declares the row (for example `code.M`) when nothing has changed yet, so that there is nothing to measure |
| `plan [--project PATH] [--base REF] [--run-id ID] [--intent ID [--spec FILE]] [--row ROW]` | prints, as JSON, the row with the intent applied and its ordered stages with resolved record paths. With `--intent` it starts the run: it writes the intake record and copies the supplied spec into the run as the plan record (a run that has begun is never restarted) |
| `check-record TYPE FILE` | validates a record against its schema |
| `render FILE [--type TYPE]` | prints a record as markdown |
| `init [--project PATH] [--graft]` | adopts a repository |
| `merge-review RUN STAGE [--round N]` | builds a review stage's `review_record` from its prosecutors', defenders' and detective's records, applying the survival rule |
| `gate RUN STAGE` | evaluates the stage's gate mechanically; exit 0 if it passes, 1 if not; the evaluation is entered in the ledger |
| `tokens RUN` | prints, as JSON, the output, fresh input and cache reads of the run's agents per model |
| `pass RUN` | writes the `pass_record` for HEAD, if the working tree is clean and every gate of the run passed |
| `override --reason TEXT [--run RUN]` | writes an override record for HEAD (a reason of at least 20 characters; `-` reads it from stdin); only the builder runs it, through `/plumbline:override` |
| `status [--run RUN]` | shows the latest run, its stages and gates, and whether HEAD is covered |
| `check-diff [--run RUN] [--base REF]` | runs the commit checks (symlinks, absolute home paths, key-shaped secrets) on the whole change, and prints them with `diff_sha256` as JSON; exit 1 when a check fails |

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
| `change_class` | intake | the changed files, their types, the size, the row and the intent |
| `spec` | planner | goal, acceptance criteria, interfaces, test plan, risks, a split proposal at size L |
| `tests_record` | test-writer | the tests, which criteria they cover, the stub check |
| `build_note` | builder | files changed, criteria addressed, assumptions |
| `verify_record` | verifier | commands run, test counts, failing criteria, mechanical checks, and `diff_sha256` |
| `review_record` | a review unit, by `merge-review` | findings, defenses, survivors, gaps, and `diff_sha256` |
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

**The reviewed change is the pushed change.** `diff_sha256` is the sha256 of what `git diff-tree --raw` lists between the tree of the run's merge base and the tree of the files as they are, untracked files included and `.plumbline/` left out: mode, blob id and path of each changed file, so a change hashes the same before and after it is committed. `check-diff` prints it (the verifier copies it into its record), and `merge-review` stamps it on the review record. `pass` refuses when the change of HEAD, from the merge base, hashes to something else than the latest verify record and the latest review of the diff: a change edited after its review fails, and the pipeline runs again from `verify`. `pass` also enters in the ledger the hash of every record its pass record lists, and of the pass record itself. The push gate does not trust the pass file alone: it reads the run again, requires that entry, requires that no listed record was altered since, and evaluates `all_gates_passed` again.

**Protected files.** `.plumbline/pass/` and every run's `ledger.jsonl` are written only by `plumbline.py` commands. An Edit, Write or NotebookEdit of them, and a Bash redirection, `tee`, `cp`, `mv`, `rm` and the like aimed at them, are denied to everyone, the main session included.

## Agents

Seven agents, each with a pinned model, a tool list and a `maxTurns` (a stage that needs more is split), and each ending its final message with `RECORD: <path>`:

| Agent | Model | Tools | Writes | Record |
| --- | --- | --- | --- | --- |
| `plumbline:planner` | Sonnet | Read, Grep, Glob, Bash, Write | its record | `spec` |
| `plumbline:test-writer` | Sonnet | Read, Grep, Glob, Bash, Edit, Write | tests and its record | `tests_record` |
| `plumbline:builder` | Sonnet | Read, Edit, Write, Grep, Glob (no Bash) | source and its record | `build_note` |
| `plumbline:verifier` | Haiku | Read, Grep, Glob, Bash, Write | its record | `verify_record` |
| `plumbline:prosecutor` | Sonnet | Read, Grep, Glob, Bash, Write | its record | `findings_record` |
| `plumbline:defender` | Haiku | Read, Grep, Glob, Bash, Write | its record | `defense_record` |
| `plumbline:detective` | Sonnet | Read, Grep, Glob, Bash, Write | its record | `gaps_record` |

The prompts state what to do. The planner, test-writer and builder name ponytail's ladder. The prosecutor carries the severity rubric and files a finding only with a quote and a concrete failure. The defender refutes only with a quote. The detective runs only once no blocker stands. An agent with Bash checks its record with `check-record` before it finishes. In this native build an agent that has Bash gets no Grep or Glob tools (`grep`, `rg` and `find` come through Bash).

## Skills

- `/plumbline:run` is the main session's recipe: infer and confirm the intent, start the run with `plan --intent`, launch each stage's agent (`run_in_background: false` when the next step needs its result), run a review round's prosecutors in the background together, then its defenders together, then the detective, evaluate the gates, loop to `on_fail` up to `max_rounds` (then stop and bring the findings and failing criteria to the builder), commit, `pass`, and report `status` and `tokens`. Records live in the main checkout, never in an agent's worktree.
- `/plumbline:override` (typed by the builder only: `disable-model-invocation: true`) records an override for HEAD. It runs `plumbline.py override` through skill shell injection with the builder's arguments as the reason (through a quoted here-document, so no character of the reason is read as shell), and the injection does not pass through PreToolUse. A Bash command that runs `plumbline.py override` is denied to every agent and to the main session, wherever the repository has adopted plumbline.
- `/plumbline:status` shows the latest run, its stages and gates, and whether HEAD is covered.
- `/plumbline:init` and `/plumbline:subagent-discipline`, as before.

## Hooks

`hooks/hooks.json` registers three hooks, each command ending in `|| true`. The PreToolUse and SubagentStop commands run a small `sh` filter first (`scripts/pre_tool_use.sh`, `scripts/subagent_stop.sh`): Claude Code runs a plugin's hook for every matching tool call in every session, and a plugin agent cannot carry a hook of its own, so most calls have nothing to do with plumbline. The filter reads the input once and starts Python only when it mentions `plumbline` (every plumbline agent's type, the `.plumbline/` paths, `plumbline.toml`, `plumbline.py`), `override`, or `git` or `gh` followed by a space, a tab or newline, a backslash or a quote (SubagentStop: `plumbline:`); everything else exits 0 at once with no output, in about a millisecond instead of about 30. All three hooks are silent, and allow everything, in a repository without `plumbline.toml`, and on any error.

- **SubagentStop**, for plumbline agents only (`plumbline:planner`, `test-writer`, `builder`, `verifier`, `prosecutor`, `defender`, `detective`): the agent's final reply must end with `RECORD: <path>`, a record under `.plumbline/runs/<run-id>/` that validates against that agent's record type. Until it does, the stop is blocked, at most 3 times; then the agent is let go and the ledger marks the record invalid. Every stop that is let go is entered in `ledger.jsonl` with the agent's id, type, stage, record and transcript path. Because `|| true` turns exit status 2 into 0, a block is delivered as `{"decision": "block", "reason": ...}` on stdout (the script also writes the reason to stderr and exits 2).
- **PreToolUse on Bash**: `git push` and `gh pr create` are denied unless HEAD has a valid pass or override record (a pass record is re-evaluated, see above). `git commit` is denied when what it would commit adds a symlink, an absolute home path (a `home` or `Users` directory at the root, then a user name and a slash), or a key-shaped secret (`sk-ant-` and 20 more characters). The command line is parsed, so `git push --dry-run` and `echo git push` are not pushes. A push in the same command line as a commit is denied, because the new commit cannot have a record yet. `plumbline.py override` is denied to everyone. A write into a protected file is denied to everyone. For a plumbline agent, every simple command of the line must belong to a command class of its role, and nothing may be redirected into a file except `/dev/null`; a denial names what the role may run.
- **PreToolUse on Edit, Write and NotebookEdit**: protected files are denied to everyone. A plumbline agent writes only what its role's write targets cover: the planner, verifier, prosecutor, defender and detective their own record only (`plan.json`, `verify.json`, or `<review stage>/round-<n>/<name>.json`); the test-writer the paths of the `tests` type and its record; the builder source files and its record, never a test path, `.plumbline/`, `plumbline.toml` or the pipeline file. A path that leads elsewhere through a symbolic link counts as where it leads.
- **PreToolUse on Agent**: launching a plumbline agent with `isolation: "worktree"` is denied, because a run's records live in the main checkout.
- **PreToolUse on Read, Grep and Glob, for `plumbline:builder` only**: the builder works blind to the tests. A path matching the pipeline's `tests` type, or listed in the newest run's tests record (that record itself included), is denied, and so is a Grep or Glob without an explicit path, or with a path that leads to a directory holding tests.

A deny uses `permissionDecision: "deny"`, which the model sees as `PreToolUse:<Tool> hook error: <reason>`.

The hooks are guard rails against drift and accident, not a sandbox. A command line is parsed, not run, so a path or command the shell computes (`$VAR`, `$(...)` output) cannot be followed, a symbolic link that hides `.plumbline/pass/` from the text of a main-session Bash command is not resolved (the write tools resolve links, and an agent's Bash never redirects into a file), and a command assembled from pieces (`pu""sh`) is not recognised. The `sh` filter errs towards Python, never away from it, for the words above.

## Status

Phase 2b of 5, agents and orchestration: on top of the enforcement core, the seven agents, `/plumbline:run`, `/plumbline:override` and `/plumbline:status`, the intent axis, role policies as data with write partitions and command allow-lists, protected files, the user-only override, the reviewed-change-is-the-pushed-change integrity checks, the ponytail matcher check, and the `sh` filter in front of the hooks. Repository rules as data arrive in phase 2c, graft and CI later. Not yet published. The tests run with `uv run --with pytest pytest -q`.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.
