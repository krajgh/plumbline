# plumbline

A Claude Code plugin: a development harness in four parts.

1. **The subagent discipline.** Work goes through subagents sized to the job. The main session keeps the brief, the review and the decisions, and alone writes the files the whole project shares. It ships as the `plumbline:subagent-discipline` skill and a short note at every session start.
2. **A typed, adversarial pipeline, defined as data.** Plan, tests, a test-review loop, build, verify, a review loop (prosecutors, then defenders, then a detective), and a reduce in the main session. Every stage writes a typed JSON record. The definition is `pipeline/default.toml`, and a repository can override parts of it.
3. **ponytail as a dependency**, applied to the making side: the planner, the test-writer and the builder.
4. **graft as an optional per-repository switch**, off by default.

## Install

plumbline needs Claude Code, `git`, and `python3` 3.11 or newer on `PATH`: the command line and the hooks read TOML with the standard library's `tomllib`. `node` is optional; session start uses it, when it is there, to check a regular expression the way ponytail's hook reads it (next section). Where a hook finds no `python3` of that version, an adopted repository gets the denial "plumbline needs python3 on PATH" for each call the hook would have looked at, until one is installed. Other repositories see nothing.

plumbline depends on the ponytail plugin, which lives in its own marketplace. Add that marketplace first, then plumbline's, then install:

```
claude plugin marketplace add DietrichGebert/ponytail
claude plugin marketplace add krajgh/plumbline
claude plugin install plumbline@plumbline
```

plumbline's marketplace allows the cross-marketplace dependency, so installing plumbline brings ponytail with it. If ponytail is not enabled in any settings file, the session-start note says so.

### Scope ponytail to the agents that make the change (once)

ponytail injects its ruleset into every subagent through a `SubagentStart` hook (`hooks/ponytail-subagent.js`). It scopes that when the environment variable `PONYTAIL_SUBAGENT_MATCHER` holds a regular expression: only a subagent whose `agent_type` matches it (unanchored, case-insensitive) gets the ruleset; unset, an invalid expression or an unreported type means every subagent. plumbline wants ponytail on its planner, test-writer and builder (the agents that write), and off its verifier and reviewers. Set the variable once, in the `env` block of `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{"env": {"PONYTAIL_SUBAGENT_MATCHER": "^(?!plumbline:)|^plumbline:(planner|test-writer|builder)$"}}
```

The first alternative keeps ponytail on for every subagent that is not a plumbline agent. The second picks plumbline's three making agents. The verifier, prosecutor, defender and detective match neither, so ponytail stays off them. The shorter `^plumbline:(planner|test-writer|builder)$` passes the check too, and switches ponytail off for every other subagent of the session.

Where ponytail is enabled and the repository has adopted plumbline, every session start checks the variable, in the process environment and in the `env` block of the user, project and local settings files, and says what to change until the matcher reaches exactly those three of plumbline's agents. The check runs the value through `node` when it is installed, because ponytail's hook is JavaScript (`(?<name>...)` is a group there, and `(?P<name>...)` is an error); without `node` it uses Python's `re`. plumbline writes no settings itself.

## Adopt a repository

Inside a git repository, run `/plumbline:init`. It writes `plumbline.toml` at the repository root and adds `.plumbline/` to `.gitignore`, tells you what it changed, and commits nothing: commit both files yourself. It never overwrites an existing `plumbline.toml`. `/plumbline:init --graft` records `enabled = true` for graft, which arrives in a later phase.

Without `plumbline.toml`, plumbline gives a session the discipline note, a one-line hint inside git repositories, and, where ponytail is not enabled, the line that says how to enable it (that line shows in any directory). The PreToolUse and SubagentStop hooks stay silent and gate nothing. Run records go in `.plumbline/runs/<run-id>/<stage-id>.json`, inside the checkout.

### The `[commands]` table

`plumbline.toml` holds the commands the agents may run and that `plumbline.py gate` runs itself, each a command prefix (or a list of prefixes):

```toml
[commands]
test = "python3 -m pytest"
lint = "ruff check"
typecheck = "mypy ."
build = "make build"
timeout = 900
```

- The verifier's Bash runs only these commands, read-only git and search tools, and `plumbline.py check-diff`; the test-writer's runs the `test` command, read-only git and search tools.
- `gate` needs `test`. For a verify stage it runs `test` and then each of `lint`, `typecheck` and `build` that the table sets; for a tests stage it runs `test`. With no `test` the two gates fail and say to add one. Where a command has a list of prefixes, `gate` runs the first.
- Each command runs from the repository root under `sh -c`, in a session of its own, with no input. Its output is discarded: plumbline keeps that it ran, how it ended and how long it took, so no assertion text reaches the main session.
- `timeout` is an integer of at least 1, in seconds, and limits each command (900 by default). Past it the command and everything it started are killed, and the gate fails and says so. `gate` ends the commands too when it receives SIGTERM, SIGINT or SIGHUP.
- A pytest command (`pytest`, `python -m pytest`, `uv run ... pytest`) also gets `--junitxml=.plumbline/runs/<run-id>/junit-<stage>.xml`, and `gate` keeps its counts of tests, failures, errors and skipped tests. Only the `test` command gets this.
- Around each run `gate` records a hash of the files a test run must leave alone: `.git/config` and `.git/hooks/`, `.gitattributes`, `.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md`, `plumbline.toml`, and the repository's own pipeline file. A run that changed one fails the gate.
- Set `test` and commit `plumbline.toml` before you start a run, because `pass` needs a clean tree and the agents leave `plumbline.toml` to you. At `pass`, the latest measured run must have run every command the table declares then.

## The pipeline file

`pipeline/default.toml` is plain TOML (top-level keys come before any table):

- `schema`, `name`, `inputs` (what exists before any stage runs), `generated` (globs whose lines are not counted), and `precedence` (when a change touches several file types, the first type listed decides its row).
- `[sizes]`: the changed-line limits for size S and size M. More than M is L.
- `[[type]]`: file types with glob `paths`, tried in order; a file takes the first that matches. The last type must match every path.
- `[[stage]]`, in the order they run. An agent stage has a `role`. A review stage has `kind = "review"`, a `target`, and `lenses`, and no role. Both have `reads` (a trailing `?` makes a read optional), a `record` type, and optionally a `gate`, an `on_fail` stage to return to with `max_rounds`. `on_fail` names an earlier agent stage, `main`, or, for an agent stage, itself: then the planner or the test-writer runs again with the gate's problems.
- `[matrix.<type>]`: for each type, the `stages` that run, in definition order (a row selects, it never reorders), optionally `lenses` for the review of the diff and a `note`. A row is flat, or split by size as `[matrix.<type>.S]`, `.M` and `.L`. A stage the row includes must find what it reads (an input, or an earlier stage of the row), for every role. A read marked `?` may be absent: `reduce` reads `verify?` and `review?`, so it reduces whatever the row produced, and a repository's row of just `intake` and `reduce` is valid.
- `[roles.<agent>]`: what each of the seven agents may do. `writes` lists write targets: `record` (the agent's own records under `.plumbline/runs/`), `tests` (the paths of the `tests` type) and `code` (everything else, except `.plumbline/`, `plumbline.toml` and the pipeline file). `commands` lists command classes: `test`, `lint`, `typecheck` and `build` (the repository's `[commands]`), `git-read` (diff, show, log, status, rev-parse, merge-base, ls-files, grep, blame), `search` (grep, rg, cat, head, tail, wc, `sed -n`, ls, and `find` without `-exec` or `-delete`), `plumbline-check` (`plumbline.py check-diff`) and `graft` (the graft wrapper, where graft is on). A role with no commands has no Bash. Every agent may also run `plumbline.py check-record` to check its record. An unknown target, class or role is a validation error.
- `[intent.<id>]`: why a change is made, next to its type and size (below).

In the default pipeline `plan` has 2 rounds, `tests` 3, `test-review` 2, `verify` 3 and `review` 3. The gate of `plan` is `spec_complete`, the gate of `tests` is `tests_fail_on_stub`, and both run their own agent again on a failure.

Globs are anchored at the repository root. `**` matches zero or more directories, `*` and `?` stay within one path segment, and `LICENSE` matches only the root file.

### Intents

The row selects stages; the intent then removes stages (`skip`), or supplies the record a removed stage would have written (`supplies`), and may replace a stage's gate (`gates`) and add lenses to the review of the diff (`lenses`, joined to the row's own).

| Intent | Skips | Supplies | Also |
| --- | --- | --- | --- |
| `feature` (the default) | nothing | nothing | the planner writes the spec |
| `spec-supplied` | `plan` | the spec, from `--spec FILE` | |
| `fix` | `plan` | a one-criterion spec, from `--spec FILE` | the tests' gate is `reproduces_on_head`: they must fail on today's code, on an assertion |
| `refactor` | `plan`, `tests`, `test-review` | the shipped template spec (`pipeline/templates/refactor.json`): behaviour unchanged, every existing test passes | review lenses `correctness` and `boundaries`, next to the row's own; the change touches no file of the `tests` type |
| `review-only` | `plan`, `tests`, `test-review`, `build` | nothing | a change that already exists: nothing is built |

After an intent is applied to a row, every required read must be produced by a stage left in the row or listed in `supplies`; an intent may name only known stages, gates and lenses. A supplied spec is validated against the `spec` schema and must pass `spec_complete`, like the record of the stage it stands for. The intake record carries the intent, and `pass` lists a supplied stage with 0 rounds.

### The command line

`python3 scripts/plumbline.py` takes these commands. Options are spelled out: the command line takes no abbreviation of an option, so `--rea` is an error and never `--reason`.

| Command | What it does |
| --- | --- |
| `validate-pipeline [FILE] [--project PATH]` | validates the default pipeline (or FILE), merged with the repository's `plumbline.toml` when `--project` is given |
| `classify [--project PATH] [--base REF] [--intent ID] [--row ROW] [--out FILE]` | writes the `change_class` record: the diff from the merge base to the working tree, plus untracked files, and the intent. `--row` declares the row (for example `code.M`) when nothing has changed yet, so that there is nothing to measure |
| `plan [--project PATH] [--base REF] [--run-id ID] [--intent ID [--spec FILE]] [--row ROW]` | prints, as JSON, the row with the intent applied and its ordered stages with resolved record paths. With `--intent` it starts the run, in a repository that has adopted plumbline: it writes the intake record (its hash goes into the ledger first), copies the supplied spec into the run as the plan record, and names the run in `.plumbline/runs/ACTIVE`. A run that has begun is never restarted |
| `check-record TYPE FILE` | validates a record against its schema |
| `render FILE [--type TYPE]` | prints a record as markdown |
| `init [--project PATH] [--graft]` | adopts a repository |
| `merge-review RUN STAGE [--round N]` | builds a review stage's `review_record` from its prosecutors', defenders' and detective's records, applying the survival rule. It refuses a record made against another change or left by no agent of the right role, counts a refutation only when its quote is in the change or the finding's file, and enters the merge in the ledger. With no `--round` it merges the highest round; a round past the stage's `max_rounds` is refused |
| `gate RUN STAGE` | evaluates the stage's gate; exit 0 if it passes, 1 if not, 3 when the stage has used its rounds. The gates of the verify and tests stages run the repository's `[commands]` first. It prints "round k of N", and when a review fails with blockers standing and rounds left it opens the next round's directory. The evaluation is entered in the ledger |
| `tokens RUN` | prints, as JSON, the output, fresh input and cache reads of the run's agents per model |
| `pass RUN` | writes the `pass_record` for HEAD, if the working tree is clean, every gate of the run passed, and the change, measured again from the run's merge base, selects no stage the run lacks |
| `override --reason TEXT [--run RUN]` | writes an override record for HEAD (a reason of at least 20 characters; `-` reads it from stdin); only the builder runs it, through `/plumbline:override`. `--run` defaults to the run in progress |
| `status [--run RUN]` | shows a run, its stages and gates, and whether HEAD is covered; the run is the one `.plumbline/runs/ACTIVE` names unless `--run` says another |
| `check-diff [--run RUN] [--base REF]` | runs the commit checks (symlinks, absolute home paths, key-shaped secrets) on the whole change, and prints them with `diff_sha256` as JSON; with `--run` it also prints the row the change measures as (`row`) and measures from where the run began. Exit 1 when a check fails |

Every command except `check-record` and `render` takes `--project PATH`: the repository to work in, by default the one that holds the current directory. Commands that measure a change read it on a copy of the index, so they leave the repository's own index as it was, and every git command they run passes `--no-optional-locks`.

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
| `change_class` | intake | the changed files, their types, the size, the row, the intent, and the merge base every later check measures from |
| `spec` | planner | goal, acceptance criteria, interfaces, test plan, risks, a split proposal at size L |
| `tests_record` | test-writer | the tests (each named as the name appears in its file), which criteria they cover, the stub check |
| `build_note` | builder | files changed, criteria addressed, assumptions |
| `verify_record` | verifier | commands run (each summary one short line, at most 80 characters, without the characters of source code), test counts, failing criteria, mechanical checks, and `diff_sha256` |
| `review_record` | a review unit, by `merge-review` | findings (each marked `evidence_unverified` or not), defenses, survivors, `routes` (who each surviving finding goes to), gaps, and `diff_sha256` |
| `findings_record` | each prosecutor | one lens and its findings, and `diff_sha256` |
| `defense_record` | each defender | one defender's verdict on each finding, and `diff_sha256` |
| `gaps_record` | the detective | what is missing, once no blocker stands, and `diff_sha256` |
| `pass_record` | reduce | stage results, tokens, the verdict |
| `override_record` | the builder | a push without a passing run, with a reason |

## Runs, gates and the pass record

A run is the directory `.plumbline/runs/<run-id>/` in the checkout:

```
.plumbline/runs/ACTIVE                                      the id of the run in progress, and a newline
.plumbline/runs/<run-id>/<stage-id>.json                    the stage's record
.plumbline/runs/<run-id>/<stage-id>/round-<n>/<name>.json   a review unit's per-agent records
.plumbline/runs/<run-id>/junit-<stage>.xml                  what a measured pytest run reported
.plumbline/runs/<run-id>/ledger.jsonl                       only ever appended to
.plumbline/pass/<HEAD>.json                                 the pass record that lets HEAD be pushed
.plumbline/pass/<HEAD>.override.json                        or an override record
```

**The run in progress.** `plan --intent` writes `.plumbline/runs/ACTIVE`: the run's id and a newline. `status`, `override` and the hooks read it. Agents write their records in that run, the builder reads that run's intake and plan, and a stop is credited to it. Where the file is missing, or names a run that is not there, the newest run stands in. Only `plan --intent` writes it (see Protected files). The run ids `ACTIVE` and `active` are refused, because the file sits beside the run directories.

**The ledger** is `ledger.jsonl`, one JSON object per line, each with a UTC time `at` and a `kind`:

| Kind | Written by | Holds |
| --- | --- | --- |
| `intake` | `plan --intent` | `stage`, `record`, `record_sha256` (entered before the file exists), `intent`, `row`, `merge_base` |
| `supplied` | `plan --intent` | `stage`, `record`, `source` (where the supplied record came from), `record_sha256` |
| `agent` | the SubagentStop hook | `agent_id`, `agent_type`, `stage`, `record`, `record_type`, `record_sha256`, `valid`, `blocks` (how often the stop was held back), `transcript` and `session_transcript` (the paths the tokens command reads). A stop that leaves the `record`, `record_sha256` and `valid` that the agent's latest entry already holds is not entered again |
| `merge` | `merge-review` | `stage`, `round`, `record`, `record_sha256`, and the `parts` it was built from, each with its path and sha256 |
| `run` | `gate`, for a verify or tests stage | `stage`, `gate`, the `commands` that ran (`name`, `cmd`, `exit_code`, `seconds`, and `timed_out` or `junit` where they apply), `diff_sha256` of the change they ran on, `guarded_before` and `guarded_after`, `timeout` |
| `gate` | `gate`, `pass`, and `plan --intent` for a supplied record | `stage`, `gate`, `passed`, `record_sha256`, and the first ten `problems` |
| `pass` | `pass` | `commit`, `pass_record`, `pass_sha256`, and `records`: the sha256 of every record the pass record lists |

**Provenance.** A stage's record counts only when the ledger traces it. An agent stage needs an `agent` entry from the matching `plumbline:<role>`, valid, holding the record's current sha256. A review needs the `merge` entry that `merge-review` wrote, whose `parts` each carry their own `agent` entry from a prosecutor, defender or detective. A record an intent supplies needs the `supplied` entry, and the intake record needs its `intake` entry: a run whose intake record was edited since is refused. A record written by hand, edited after its agent stopped, left invalid, or written by an agent of another role has no such entry, and its gate fails. `pass` and the push gate apply the same test.

**Gates** are mechanical. `gate RUN STAGE` requires the stage's record to exist, validate and be traced, then checks it. A gate marked *measured* also runs the repository's commands (see the `[commands]` table) and reads what happened, so that it rests on what happened and not on what an agent typed.

- `spec_complete`: at least one acceptance criterion, each defined once and each with a test plan entry, and no test plan entry for a criterion the spec lacks. A `fix` spec has exactly one criterion.
- `acs_covered`: opens the test files. Each test's file exists inside the repository, is of the `tests` type and holds the test's name; each criterion a test claims is the spec's; each criterion of the spec is covered by such a test.
- `tests_fail_on_stub` *(measured)*: `acs_covered`, at least one test, a stub check that ran and says every test failed on an assertion, and the test command, run by `gate`, must fail. A pytest run fails with exit status 1 (2 is a collection or usage error, and 5 collected no tests: neither is a failing test); any other command fails with a status other than 0, 126 and 127. Once the build exists, a test-writer that revises tests which passed this gate before is held to coverage and to at least one test; the run is recorded, and the tests are no longer expected to fail.
- `reproduces_on_head` *(measured)*: the tests gate of a `fix`: the same, against today's code.
- `verify_green` *(measured)*: the record's `green` must be what its commands, checks and counts add up to, and every command `gate` ran must exit 0 on the change the record covers. No guarded file may change during the run. For a pytest run the count of tests must not fall below the tests stage's run, and no more of them may be skipped. A refactor's change touches no file of the `tests` type.
- `no_surviving_blockers`: the number of standing blockers is recomputed from the findings and the survivors, and a review of the diff must cover the change as it is.
- `all_gates_passed`: every other stage of the run's row has a valid, traced record and passed its gate at its last evaluation in the ledger, with its record unchanged since.

**Rounds.** A stage with `max_rounds` runs at most that many rounds. `gate` prints `round k of N` and exits 3 when the stage fails on its last round, or is run past it (a good record included); then the run stops and brings the findings and failing criteria to the builder. An agent stage's round is the number of its attempts since its gate last passed. An attempt is an agent's work up to the stage's next gate: an agent that stops again before the gate for the same report is still one attempt, the same agent resumed after a failed gate makes the next, and a second agent before the gate, or a stop that names no agent, is an attempt of its own. A review's round is its record's round. The `rounds` of a stage in the pass record count its attempts over the whole run.

**A review unit** writes one record per agent under `<stage-id>/round-<n>/`, and `merge-review` builds the stage's record. The files are `prosecutor-<lens>.json` (one per lens), `defender-<n>.json` and `detective.json`. A finding survives when at least `survive_if_unrefuted_by` of the stage's `defenders` did not refute it (a majority when the stage sets none). A defender refutes only with a verdict of `refuted` and a quote of at least 6 characters, whitespace aside, that occurs in the change's diff (added, removed and context lines) or in the current content of the file the finding names; silence, a concession, or a refutation without such a quote does not count. A finding whose own evidence is in neither place stays and is marked `evidence_unverified`. A surviving finding is routed: the tests lens and findings about a test file go to the test-writer, the rest to the builder, whose brief carries the text of its findings and no test, quote or review file. A stage without defenders lets every finding survive. Duplicate finding ids, a missing lens, a defense of an unknown finding and a record made against another change are errors, never silently repaired.

**Review rounds beyond the first.** Round 1's directory appears when its first agent writes. When a review's gate fails with blockers standing and the stage has rounds left, `gate` creates the next round's directory itself (`round-2/` after round 1) and says so; the main session has no step of its own. The review agents write only in the highest round directory their stage has, and `merge-review` merges the highest round when it gets no `--round`, so the hook, `merge-review` and `gate` count one round. A gate that fails for the ledger's reason (a record that is missing, changed after `merge-review` wrote it, or not traced to its agents) opens nothing: that round is merged again.

**`pass RUN`** needs a clean working tree apart from `.plumbline/`, so commit first. It evaluates every gate afresh and measures the change again, from the run's merge base: the run's row is an estimate, and `pass` refuses a run whose row lacks a stage the measured row selects, a row that ends before reduce, and a change of size L (nothing is built at that size). Its notes say the declared and the measured row. Then it writes the `pass_record` (with the tokens of the run's agents) to the run and to `.plumbline/pass/<HEAD>.json`. **`override --reason`** writes `.plumbline/pass/<HEAD>.override.json` instead, and never overwrites one.

**The reviewed change is the pushed change.** `diff_sha256` is the sha256 of what `git diff-tree --raw` lists between the tree of the run's merge base and the tree of the files as they are, untracked files included and `.plumbline/` left out: mode, blob id and path of each changed file, so a change hashes the same before and after it is committed. `check-diff` prints it, and the verifier, each prosecutor, each defender and the detective copy it into their records. `merge-review` refuses a record made against another change and stamps the hash on the review record. `pass` refuses when the change of HEAD, from the merge base, hashes to something else than the latest verify record and the latest review of the diff: a change edited after its review fails, and the pipeline runs again from `verify`. `pass` also enters in the ledger the hash of every record its pass record lists, and of the pass record itself. The push gate does not trust the pass file alone: it reads the run again, requires that entry, requires that no listed record was altered since, and evaluates `all_gates_passed` again.

**Protected files.** `.plumbline/pass/`, every run's `ledger.jsonl` and `.plumbline/runs/ACTIVE` are written only by `plumbline.py` commands. An Edit, Write or NotebookEdit of them, and a Bash redirection, `tee`, `cp`, `mv`, `rm` and the like aimed at them, are denied to everyone, the main session included.

**Resuming a run.** A run outlives a compaction or a restart. `/plumbline:status` shows the run `.plumbline/runs/ACTIVE` names, each stage with its state. `/plumbline:run` continues it from the first stage that is not `pass`, `supplied` or `recorded`, under the same run id. It starts a new run only when the builder asks for a new change, or when the run is stuck: its row is too small for the change, or its stages have used their rounds.

## Agents

Seven agents, each with a pinned model, a tool list and a `maxTurns` (a stage that needs more is split), and each ending its report, its final message or the message of its SubagentHandback call, with `RECORD: <path>`:

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

- `/plumbline:run` is the main session's recipe: continue the run in progress, or infer and confirm the intent and start a run with `plan --intent`; launch each stage's agent (`run_in_background: false` when the next step needs its result); run the two measured gates with the Bash tool's largest timeout, or in the background; run a review round's prosecutors in the background together, then its defenders together, then the detective; evaluate the gates; loop to `on_fail` up to `max_rounds` (then stop and bring the findings and failing criteria to the builder); commit; `pass`; and report `status` and `tokens`. Records live in the main checkout, never in an agent's worktree.
- `/plumbline:override` (typed by the builder only: `disable-model-invocation: true`) records an override for HEAD. It runs `plumbline.py override` through skill shell injection with the builder's arguments as the reason (through a quoted here-document, so no character of the reason is read as shell), and the injection does not pass through PreToolUse. A Bash command that runs `plumbline.py override` is denied to every agent and to the main session, wherever the repository has adopted plumbline.
- `/plumbline:status` shows a run (the one in progress), its stages and gates, and whether HEAD is covered.
- `/plumbline:init` and `/plumbline:subagent-discipline`, as before.

## Hooks

`hooks/hooks.json` registers three hooks, each command ending in `|| true`. The PreToolUse and SubagentStop commands run a small `sh` filter first (`scripts/pre_tool_use.sh`, `scripts/subagent_stop.sh`): Claude Code runs a plugin's hook for every matching tool call in every session, and a plugin agent cannot carry a hook of its own, so most calls have nothing to do with plumbline. The filter reads the input once and starts Python only when it mentions `plumbline` (every plumbline agent's type, the `.plumbline/` paths, `plumbline.toml`, `plumbline.py`), `override`, or `git` or `gh` followed by a space, a tab or newline, a backslash, a quote, a dash, a dot or a redirection (SubagentStop: `plumbline:`). Everything else exits 0 at once with no output, in about a millisecond instead of about 60.

The PreToolUse and SubagentStop hooks are silent, and allow everything, in a repository without `plumbline.toml`. Where a rule meets input it cannot handle, it says nothing and the others still run, with one exception: the push gate holds back a push it cannot check. SessionStart prints its note in every directory.

### SessionStart

It prints the discipline note; inside a git repository one line saying whether plumbline is adopted (with the pipeline's name and graft on or off), not adopted, or that `plumbline.toml` is invalid; the ponytail line where ponytail is not enabled; and, where it is enabled and the repository has adopted plumbline, a line unless `PONYTAIL_SUBAGENT_MATCHER` reaches exactly the three making agents.

### SubagentStop

It acts for plumbline agents only. The agent's report must end with `RECORD: <path>`. The report is its final message; when that message has no such line, the hook reads the `message` of the agent's last SubagentHandback call (the tool through which the harness has an agent deliver its report) from the end of its transcript, at most 2 MiB of it. The line names a record that validates against that agent's record type and sits where that agent's records belong: in the run in progress, at `<stage>.json` for a stage of the run's row that its role serves, or, for the three review roles, at `<review stage>/round-<n>/<name>.json`. Until it does, the stop is blocked, at most 3 times per agent id; then the agent is let go and the ledger marks the record invalid. A problem the agent cannot mend (an invalid `plumbline.toml`, a run with no usable intake record) does not hold it up: it is let go and entered as invalid. Every stop that is let go is entered in `ledger.jsonl` as an `agent` entry, with the record's sha256, credited to the run in progress, unless the agent's latest entry holds the same record, sha256 and validity already: an agent that the harness asks again for its report stops again and stays one entry. A later stop that changes the record, or whether it is valid, is entered. An agent of an unknown `plumbline:` role is let go and entered as invalid. Because `|| true` turns exit status 2 into 0, a block is delivered as `{"decision": "block", "reason": ...}` on stdout (the script also writes the reason to stderr and exits 2).

### PreToolUse

It is registered for `Bash|PowerShell|Monitor|Read|Grep|Glob|Edit|Write|NotebookEdit|Agent`. A Monitor runs a shell command, so it is read as Bash.

**On Bash, PowerShell and Monitor, for everyone:**

- `git push` and `gh pr create` (also `gh pr new`) are denied unless the commit the command publishes has a valid pass or override record. A pass record is re-evaluated, see above.
- Every directory the command line may run in is checked: the working directory and each `cd`, `pushd`, `env -C` and `git -C` target, up to 64. A working directory inside `.git` is checked like any other. A repository git will not read (ownership, `safe.directory`) holds the push back with a message that says why.
- Every ref a push names is checked, not only HEAD: `git push origin other` needs `other`'s commit covered, and `src:dst` needs `src`. Deleting a remote ref (`:dst`, `--delete`) publishes nothing and passes. `--all`, `--mirror`, `--tags`, a wildcard refspec, a bare `:` and more than 100 refs are denied with "push one reviewed branch at a time". A push that git's own settings decide (`push.default=matching`, a mirror remote, `remote.<name>.push`, `-c` and `GIT_CONFIG_*` on the command line) is read as git would read it.
- A push in the same command line as a command that moves HEAD or a ref (a commit, `merge`, `rebase`, `reset`, `checkout`, `branch`, `tag`, `update-ref` and the like) is denied when what it publishes cannot be worked out from the line: commit first, record the pass, and push in another command. `git switch -c x && git push origin x` passes, because the new branch points where HEAD does.
- Aliases are expanded: `-c alias.p=push`, an alias from git config (a shell alias with `"$@"` included), `git-push`, `git.exe`. Wrappers are read through: `sudo`, `env`, `timeout`, `nice`, `stdbuf`, `flock`, `coproc`, `trap`, `bash -c`, `eval`, `cmd /c`, `powershell -Command`, `iex`, three shells deep. `--dry-run`, `-n` and `--help` are not pushes.
- `git commit` is denied when what it would commit adds a symlink, an absolute home path (a `home` or `Users` directory at the root, then a user name and a slash), or a key-shaped secret (`sk-ant-` and 20 more characters). The check reads the change on a copy of the index, so it leaves the repository's own index as it was.
- `plumbline.py override` is denied to everyone, and so is any abbreviation of its options that argparse would take (`--rea`, `--re`, `--proj`).
- A write into a protected file is denied to everyone (see Protected files).

**On Bash, for plumbline agents:** every simple command of the line must belong to a command class of its role, and nothing may be redirected into a file except `/dev/null`. A denial names what the role may run. The read-only classes run as they are:

- `git-read` takes the subcommands listed under `[roles.<agent>]`, and before the subcommand only `-C <dir>`, `--no-pager`, `-P`, `--no-optional-locks`, `--literal-pathspecs` and `--no-replace-objects`. An option that runs a program or writes a file is refused in any spelling git accepts: `--output`, `-O` and `--open-files-in-pager`, `--ext-diff`, `--textconv`, `--exec`, `--upload-pack`, `--receive-pack`, and every abbreviation of them (`--ope`, `--outp`).
- `search` refuses `find` with `-exec`, `-execdir`, `-ok`, `-okdir`, `-delete`, `-fprint` or `-fls`, `rg` with `--pre` or `--hostname-bin`, and `sed` unless it is `sed -n` with print commands only.
- Neither takes a `VAR=value` before it: `GIT_EXTERNAL_DIFF` and `GIT_CONFIG_*` can make git run a program.

**On Edit, Write and NotebookEdit:** the protected files are denied to everyone. A plumbline agent writes only what its role's write targets cover.

- Its own record goes in the run in progress (`.plumbline/runs/ACTIVE`; with none, in the newest run; with no run at all, nowhere). The planner writes `plan.json`, the verifier `verify.json`. A prosecutor writes `prosecutor-<lens>.json`, a defender `defender-<n>.json` and the detective `detective.json`, each in the current round of a review stage: the highest `round-<n>` directory the stage has, or 1. A review role that writes another's file, or another round's, is denied.
- The test-writer writes the paths of the `tests` type and its record. The builder writes source files and its record.
- No agent writes the lane files, which change through the main session (and CI, and the agent settings): `.git/` (a linked worktree's git directory and the main repository's included), `.claude/`, `.github/`, `.husky/`, `.mcp.json`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `.gitattributes`, `.worktreeinclude` and `.pre-commit-config.yaml`. They are matched at any depth and in any case.
- An agent that writes code but not tests (the builder, by default) also leaves the test harness to the main session: `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg` and `noxfile.py` wherever they lie, and the existing files that `[commands]` names (`sh run_tests.sh` names `run_tests.sh`; `pytest --config=pytest.ini` names `pytest.ini`). It writes nothing under `.plumbline/` beyond its record, and neither `plumbline.toml` nor the pipeline file.
- A path that leads elsewhere through a symbolic link counts as where it leads.

**On Agent:** a plumbline agent is launched in the main checkout, where the run's records live: any `isolation` value is denied. Its `model` is absent, or the one its definition pins (Sonnet or Haiku, in any case): another model is denied. Another agent, `general-purpose` or none, whose prompt or description names a `.plumbline/` path (with `/` or `\`, in any case) is denied: stage work goes to the `plumbline:*` agents.

**On Read, Grep and Glob, for `plumbline:builder` only:** the builder works blind to the tests. It cannot read:

- a path of the pipeline's `tests` type, any `conftest.py`, or a file listed in the tests record of any run (those records included);
- under `.plumbline/`, anything but the active run's `intake.json` and `plan.json` and its own `build.json`: not the other records, the review files, the ledger, `junit-<stage>.xml`, the pass records or `ACTIVE`;
- what a test run leaves behind: `.pytest_cache`, `junit*.xml`, `.coverage*`, `htmlcov`, `.tox`, `coverage.xml`, `lcov.info`, `.nyc_output`, `.hypothesis`, `test-results`, `test-reports`, `pytest-report*.xml` and `pytest-report*.html`;
- the agent transcripts, under `~/.claude`, `$CLAUDE_CONFIG_DIR`, the directory the session's transcript is in, or any `.claude/projects` path. The plugin's own `schemas/` folder stays readable, because the builder is told to read it.

A Grep or Glob without an explicit path is denied, and so is one whose path leads to a directory that holds any of these, an ancestor of the repository included. The main session briefs the builder with the text of the failures, since the builder cannot read the verify or review records.

An agent whose working directory drifts out of the repository is held all the same: its repository is found from the paths its call names, and from `CLAUDE_PROJECT_DIR` (the push gate leaves that one out), as well as from its working directory.

A deny uses `permissionDecision: "deny"`, which the model sees as `PreToolUse:<Tool> hook error: <reason>`.

## Limits

The hooks are guard rails. They stop an agent that overreaches when a rule blocks it, and an agent that follows text it found in a repository. They are not a sandbox. A session that means to get past them can, and what stops that is CI: phase 4 adds a server-side check that a pass record exists for what is pushed. What is left open today:

**Pushes and pull requests**
- **Pushes assembled indirectly.** The hook parses text. It reads `sh -c`, `eval`, `$( )`, backticks, heredocs and `env -S` when the text is there, up to three shells deep. It does not read what a shell computes at run time, or what a program does. A push through a pipe into a shell (`echo 'git push' | sh`), a heredoc or here-string into a shell, a variable or command substitution that supplies the command name (`$G push`), `xargs git push`, `find -exec git push`, a script file, `python3` with `subprocess`, `git rebase -x`, `git bisect run`, `git submodule foreach`, or an `eval` of a variable is not seen. Brace expansion of the command word and shell nesting deeper than three levels are not read either.
- **`g""it push`.** A name spelled with an empty pair of quotes in the middle never passes the `sh` filter, so Python does not start. `git pu""sh` is recognised and denied.
- **Pushes of a published commit.** The gate never asks the remote, so a commit that is already published still needs a pass or override record before it is pushed again.
- **Tools the hook does not cover.** A push or pull request made through an MCP GitHub tool, a `gh` alias (`gh alias set ship 'pr create --fill'`, then `gh ship`), or git's autocorrect is not seen. The matcher names the tools it covers; the older tool name `Task` is not among them, and a tool added to Claude Code later is not covered until the matcher lists it.
- **Pre-adoption branches.** A branch or commit from before `plumbline.toml` existed has no adoption to find, so its pushes are not gated.
- **`plumbline.toml` moved aside.** A repository without `plumbline.toml` is not adopted, as far as the hooks can tell. `mv plumbline.toml plumbline.toml.off` in a call of its own leaves the pushes that follow ungated until the file is back, and nothing records that it was away.

**Files**
- **Symlink aliases to `.plumbline/`.** A link named anything, pointing at `.plumbline/pass/` or at a ledger, is followed by the write tools but not by the text of a main-session Bash command. A call that never names plumbline does not start Python at all.
- **Writers the hook does not model.** Redirections, `tee`, `rm`, `unlink`, `shred`, `truncate`, `touch`, `mv`, `dd of=`, `sed -i` and `perl -i` are read, and so are `cp`, `install`, `ln`, `rsync`, `tar`, `unzip`, `patch`, `curl` and `wget` (a copy of a glob or of a directory's contents counts as landing in the destination). Any other program that writes a file is not.
- **File systems and shells.** A case-insensitive file system that lets `Tests/` name `tests/`, and PowerShell's backtick quoting, are not handled.
- **The test harness beyond the named files.** The builder can still edit `pyproject.toml`, the `Makefile` and `package.json`, which also say how tests run. Only a measured run can catch what such an edit does, and only for pytest: the verify gate compares the number of tests that ran, and how many were skipped, with the tests stage's run. The guarded-file hashes do not cover those three files.

**Measured runs**
- **Commands run with the session's privileges, and their output is discarded.** `gate` runs the repository's declared commands as `sh -c` with the rights of the main session. That is not a sandbox: the test command itself runs arbitrary code. A run that changes a guarded file fails the gate; a run that persists anything else is not seen.
- **The junit inventory exists only for pytest.** For another runner nothing compares how many tests ran, so a declared script rewritten to `exit 0` is not caught by a measured run. The builder's write denial (the test harness, and the files `[commands]` names) is what stands in the way.

**Agents' Bash**
- **Honest commands can be refused.** An agent's Bash is judged command by command against its role's classes. `test`, `[` and `sort` are not among them, and neither are a `case` with several clauses or a `sed -n` script with several commands. An agent that needs one of them asks the main session to run it.
- **`allowed-tools` is wider than the plugin's script.** While `/plumbline:run`, `/plumbline:status` or `/plumbline:init` is active, Claude Code pre-approves any `python3` command (`Bash(python3 *)`), not only `plumbline.py`. The install path differs per user, so a narrower pattern could silently drop the pre-approval. The hooks apply either way.

## Status

Version 0.4.1. Phase 2, agents and enforcement, is done: the seven agents, `/plumbline:run`, `/plumbline:override` and `/plumbline:status`, the intent axis, role policies as data with write partitions and command allow-lists, protected files, the user-only override, the provenance and measured gates, the reviewed-change-is-the-pushed-change checks, the hooks that hold, the ponytail matcher check, and the `sh` filter in front of the hooks. Version 0.4.1 carries what the first real session found: a stage's round counts agents, not stops, and a report handed back through SubagentHandback names its record. The next phase is phase 3, light testing in a real session: one small change through the whole pipeline on a scratch repository, with tokens measured per stage. Repository rules as data (phase 2c) follow it, graft and the CI templates are phase 4, and the release is phase 5. Not yet published. The tests run with `uv run --with pytest pytest -q`.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.
