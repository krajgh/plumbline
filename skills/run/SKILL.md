---
name: run
description: Run a change through plumbline's pipeline in an adopted repository - classify it, plan, write tests, build, verify, review with prosecutors and defenders, then commit and record a pass so it can be pushed. Use when a change has to be made, or checked, before it is pushed.
argument-hint: what to change, or the change to review
allowed-tools: Bash(python3 *) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git add *) Bash(git commit *)
---

# Run the pipeline

You are the reduce. Stage agents do the work and write typed records; you launch them, evaluate the gates, decide what happens next, and alone commit and record the pass. `plumbline.py` writes every other run file: the intake record, the review records that `merge-review` builds, the ledger and the pass record. A record counts only when the ledger traces it: to the agent that wrote it (the SubagentStop hook enters each agent's stop with the record's hash), and, for a review, to `merge-review`. The gates of the verify and tests stages run the repository's own commands themselves, so they rest on what happened.

`PLUMBLINE` below stands for `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py"`.

The request: $ARGUMENTS

If that is empty, ask the builder what to change, and wait.

## 0. Before you start

- The repository has adopted plumbline when it has a `plumbline.toml`. If it has not, tell the builder that `/plumbline:init` adopts it, and stop.
- Uncommitted work is fine: it is the change, or part of it.
- **A run may be in progress already**, after a compaction or a restart. `.plumbline/runs/ACTIVE` names it. Run `PLUMBLINE status`: when it shows a run with stages that have not passed, continue that run. Take its stages in order (section 4) from the first one that is not `pass`, `supplied` or `recorded`, and keep its run id. Start a new run (section 3, with another `--run-id`) only when the builder asks for a new change, or when the run is stuck: its row is too small for the change, or its stages have used their rounds.

## 1. The intent

Ask for, or infer and confirm, the intent before anything is planned. The intent says why the change is made; it decides which stages of the row run.

| Intent | When | What changes |
| --- | --- | --- |
| `feature` | new behaviour (the default) | the planner writes the spec |
| `spec-supplied` | the builder or an issue gives the spec | the planner is skipped; you give `--spec` |
| `fix` | a bug | reproduce first: a one-criterion spec from the bug report ("this no longer happens"), and its tests must fail on today's code, on an assertion |
| `refactor` | behaviour stays the same | no planner, no new tests; the shipped template spec says behaviour is unchanged and every existing test passes, and the diff leaves the test files as they are |
| `review-only` | the change already exists | nothing is built: verify and review only |

State the intent you infer and the reason, in one sentence, and ask the builder to confirm it or pick another. Skip the question when the request names the intent.

## 2. The row

plumbline measures a change that already exists: its type and its size (S up to 50 changed lines, M up to 400, L more) select the row. When nothing has changed yet, as with a feature, a fix or a refactor on a clean tree, estimate the change and declare the row yourself: `--row code.M` for a code change of size M, a bare type such as `--row docs` for a flat row. Say your estimate with the intent, and let the builder correct both.

The declared row is an estimate, and plumbline measures the change again: `check-diff --run` reports what it measures as, and `pass` refuses a run whose row lacks a stage the measured row selects. A change that measures larger than declared needs a new run for the measured row (`plan --intent <intent> --row <measured row>`). A change that measures as size L ends before reduce: the planner proposes a split into changes of size M or smaller, and each of those gets its own run.

## 3. Start the run

For `spec-supplied` and `fix`, write the spec as a JSON `spec` record (see `${CLAUDE_PLUGIN_ROOT}/schemas/spec.json`) to `.plumbline/supplied-spec.json`. For `fix` it holds exactly one acceptance criterion and one test plan entry that reproduces the bug. Then:

```
PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>]
```

It runs `classify` on the change (or takes your declared row), writes what `classify` produced as the run's intake record (which carries the intent), copies the record the intent supplies, names the run in `.plumbline/runs/ACTIVE` (the hooks read that file to know which run is in progress), then prints the plan as JSON: the `run_id`, the `stages` in order (each with its `role` or `kind`, its `reads` as paths, its record `path`, its `gate`, `on_fail`, `max_rounds` and `lenses`), the repository's `commands`, and what is `supplied`. Keep it; every path below comes from it. If the command refuses, tell the builder why, and stop.

- A plan of only `intake` (or `intake` and `plan`) is size L: nothing is built at size L. If the planner ran, bring its split proposal to the builder; then stop.
- A plan with a `verify` stage or a `tests` stage needs `commands.test`, because `gate` runs the repository's test command itself. If it is missing, ask the builder to add `[commands]` with a `test` command to `plumbline.toml` and commit it, then start a new run (a run that has begun keeps its intake record).

## 4. The stages

Take the stages in order. `intake` was written by `plan`; `reduce` is section 7.

**An agent stage** (`role` is planner, test-writer, builder or verifier): launch the agent `plumbline:<role>` with the Agent tool and `run_in_background: false`, because the next step needs its result. Run the agents in the main checkout, where the run's records live. Then, if the stage has a `gate`, run `PLUMBLINE gate <run_id> <stage id>`. A pass takes you on; a failure sends you to section 6. The gate of the verify stage and the gate of the tests stage run the repository's commands on the change as it is, so leave the files as the agent left them between its stop and the gate. A test suite can take minutes: run those two gates with the Bash tool's `timeout` set to its largest value (600000 ms), or in the background, and wait for them to end. The commands' own limit is `timeout` under `[commands]` in `plumbline.toml` (900 seconds by default).

**Every brief** gives: the run id, the request, the path where the agent writes its record (the stage's `path`), the paths of the records it reads, and the schema of its record, `${CLAUDE_PLUGIN_ROOT}/schemas/<record>.json`. Besides:

- planner: the intake record, and the size. At size L it proposes the split.
- test-writer: the plan record, the repository's test command, and whether the run is a fix (then the tests run against today's code and must fail on an assertion). Its record names each test as the test appears in its file, and the gate opens those files.
- builder: the plan record. From the second round, the failing criteria and error types of the verify record (an `AC-<n>` and the kind of error), and the text of the surviving findings that `merge-review` printed under "for the builder", verbatim. The builder has no Bash and works from the spec and the source, so its brief carries no test file, test name, assertion or tests-lens finding, and no path of a review file: those stay with you and the test-writer.
- verifier: the commands from the plan, the plan record, the tests record when the row has one, and the run id (it runs `check-diff --run <run_id>`, and its `commands[].summary` is one short line such as `13 passed`). When `check-diff` reports a problem about the row, the change has outgrown the run: start a new run for the measured row (section 2), and leave the builder out of it.

If an agent comes back marked partial (it ran out of turns), its stage was too big: resume it once with SendMessage, or split the work into smaller stages.

## 5. Review units

A stage with `kind: review` runs one round at a time, `round-<n>` starting at 1. Its agents write under `.plumbline/runs/<run_id>/<stage id>/round-<n>/`.

Start each round with `PLUMBLINE check-diff --run <run_id>`: it prints the `merge_base` and the `diff_sha256` of the change as it is. Every brief of the round carries both, and every agent copies the `diff_sha256` into its record. `merge-review` refuses a record made against a different change, so the files stay as they are until the round is merged.

1. **Prosecutors.** Launch one `plumbline:prosecutor` per lens of the stage, all in one message, with `run_in_background: true`. Each gets its lens, what is reviewed (the change since the merge base, or, for a review of the tests, the tests record and its files), the paths of the spec and of the verify record where the run has them, the `diff_sha256`, and the record path `.../prosecutor-<lens>.json`. Wait until every prosecutor has reported.
2. **Defenders.** When the stage has `defenders`, launch that many `plumbline:defender` agents together, also in the background, named `defender-1`, `defender-2`, and so on, each with the paths of the findings records, the merge base, the `diff_sha256`, and its record path `.../defender-<k>.json`. A refutation counts when its quote occurs in the change's diff or in the finding's file. Wait until all have reported. Skip this when the stage has no defenders (every finding then stands) or no prosecutor filed a finding.
3. `PLUMBLINE merge-review <run_id> <stage id> --round <n>` builds the stage's record and applies the survival rule. It prints the surviving findings in two lists, one for the builder and one for the test-writer (the tests lens, and findings about test files), and it names any finding whose evidence it could not find (the finding stays, marked unverified). If it names a missing or invalid record, or a record made against another change, run that agent again.
4. **Detective.** When the stage has `detective: true` and the merged record's `blockers_surviving` is 0, launch `plumbline:detective` (foreground) with the merged record's path, the merge base, the `diff_sha256`, the path of the spec (the plan record) and of the tests record where the run has them, and its record path `.../detective.json`, then run `merge-review` for the same round once more. It runs only when no blocker stands.
5. `PLUMBLINE gate <run_id> <stage id>`.

## 6. When a gate fails

`gate` prints "round k of N" for the stage and exits 1 while the stage has rounds left. The stage's `on_fail` names where the run goes back to:

- `plan` and `tests` go back to their own agent (the planner, the test-writer) with the gate's problems, for up to their `max_rounds`.
- `verify` and `review` go back to `build`. Send the builder the failing criteria and error types, and the "for the builder" text of the surviving findings, and run the stages from there again, in order: `build`, then `verify`, then `review` again as a new round.
- The surviving findings under "for the test-writer" go to the test-writer, which revises the tests; then run `tests`, `verify` and `review` again. Once the build exists, the tests stage records its run of the test command and no longer expects the tests to fail.
- A review of the tests goes back to `tests`.

`gate` exits 3 when the stage has used its rounds. When it exits 3, or when `on_fail` is `main`, stop: bring the findings and the failing criteria to the builder, and ask how to go on. The builder decides whether to start a new run or to override: `/plumbline:override` is the builder's command, typed by the builder.

## 7. Reduce

When every stage has passed:

1. Look at `git status` and `git diff`: the change is what was meant.
2. Commit it yourself: add the files of the change and `git commit`. The commit is checked for a symlink, an absolute home path and a key-shaped secret.
3. `PLUMBLINE pass <run_id>`. It needs a clean tree, a row that reaches reduce, and every gate passed. It measures the change again and refuses a run whose row lacks a stage the measured row selects, and it refuses when the change was edited after `verify` and `review` covered it: then run again from `verify`.
4. Report to the builder: `PLUMBLINE status --run <run_id>`, `PLUMBLINE tokens <run_id>`, the intent, the declared and measured row, the rounds each stage took, and the detective's gaps.

Leave the push to the builder. With a pass recorded for HEAD, the push hook lets it through.
