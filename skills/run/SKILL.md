---
name: run
description: Run a change through plumbline's pipeline in an adopted repository - classify it, plan, write tests, build, verify, review with prosecutors and defenders, then commit and record a pass so it can be pushed. Use when a change has to be made, or checked, before it is pushed.
argument-hint: what to change, or the change to review
allowed-tools: Bash(python3 *) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git add *) Bash(git commit *)
---

# Run the pipeline

You are the reduce. Stage agents do the work and write typed records; you launch them, evaluate the gates, decide what happens next, and alone commit and record the pass. You write no run file by hand: `plumbline.py` does, and the hooks refuse an edit of `.plumbline/pass/` or of a ledger.

`PLUMBLINE` below stands for `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py"`.

The request: $ARGUMENTS

If that is empty, ask the builder what to change, and wait.

## 0. Before you start

- The repository has adopted plumbline when it has a `plumbline.toml`. If it has not, tell the builder that `/plumbline:init` adopts it, and stop.
- Uncommitted work is fine: it is the change, or part of it.

## 1. The intent

Ask for, or infer and confirm, the intent before anything is planned. The intent says why the change is made; it decides which stages of the row run.

| Intent | When | What changes |
| --- | --- | --- |
| `feature` | new behaviour (the default) | the planner writes the spec |
| `spec-supplied` | the builder or an issue gives the spec | the planner is skipped; you give `--spec` |
| `fix` | a bug | reproduce first: a one-criterion spec from the bug report ("this no longer happens"), and its tests must fail on today's code, on an assertion |
| `refactor` | behaviour stays the same | no planner, no new tests; the shipped template spec says behaviour is unchanged and every existing test passes |
| `review-only` | the change already exists | nothing is built: verify and review only |

State the intent you infer and the reason, in one sentence, and ask the builder to confirm it or pick another. Skip the question when the request names the intent.

## 2. The row

plumbline measures a change that already exists: its type and its size (S up to 50 changed lines, M up to 400, L more) select the row. When nothing has changed yet, as with a feature, a fix or a refactor on a clean tree, estimate the change and declare the row yourself: `--row code.M` for a code change of size M, a bare type such as `--row docs` for a flat row. Say your estimate with the intent, and let the builder correct both.

## 3. Start the run

For `spec-supplied` and `fix`, write the spec as a JSON `spec` record (see `${CLAUDE_PLUGIN_ROOT}/schemas/spec.json`) to `.plumbline/supplied-spec.json`. For `fix` it holds one acceptance criterion and one test plan entry that reproduces the bug. Then:

```
PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>]
```

It runs `classify` on the change (or takes your declared row), writes what `classify` produced as the run's intake record (which carries the intent), copies the record the intent supplies, then prints the plan as JSON: the `run_id`, the `stages` in order (each with its `role` or `kind`, its `reads` as paths, its record `path`, its `gate`, `on_fail`, `max_rounds` and `lenses`), the repository's `commands`, and what is `supplied`. Keep it; every path below comes from it. If the command refuses, tell the builder why, and stop.

- A plan of only `intake` (or `intake` and `plan`) is size L: nothing is built at size L. If the planner ran, bring its split proposal to the builder; then stop.
- A plan with a `verify` stage needs `commands.test`. If it is missing, ask the builder to add `[commands]` with a `test` command to `plumbline.toml` and commit it, then start a new run (a run that has begun is never restarted).

## 4. The stages

Take the stages in order. `intake` was written by `plan`; `reduce` is section 7.

**An agent stage** (`role` is planner, test-writer, builder or verifier): launch the agent `plumbline:<role>` with the Agent tool and `run_in_background: false`, because the next step needs its result. Run the agents in the main checkout, never in a worktree: records live here. Then, if the stage has a `gate`, run `PLUMBLINE gate <run_id> <stage id>`. A pass takes you on; a failure sends you to section 6.

**Every brief** gives: the run id, the request, the path where the agent writes its record (the stage's `path`), the paths of the records it reads, and the schema of its record, `${CLAUDE_PLUGIN_ROOT}/schemas/<record>.json`. Besides:

- planner: the intake record, and the size. At size L it proposes the split.
- test-writer: the plan record, the repository's test command, and whether the run is a fix (then the tests run against today's code and must fail on an assertion).
- builder: the plan record. From the second round, the failing criteria and error types of the verify record, or the surviving findings of the review record, by path. Give it nothing from the tests: no test file, no test name, no assertion. The builder has no Bash and works blind to them.
- verifier: the commands from the plan, the plan record, the tests record when the row has one, and the run id (it runs `check-diff --run <run_id>`).

If an agent comes back marked partial (it ran out of turns), its stage was too big: resume it once with SendMessage, or split the work into smaller stages.

## 5. Review units

A stage with `kind: review` runs one round at a time, `round-<n>` starting at 1. Its agents write under `.plumbline/runs/<run_id>/<stage id>/round-<n>/`.

1. **Prosecutors.** Launch one `plumbline:prosecutor` per lens of the stage, all in one message, with `run_in_background: true`. Each gets its lens, what is reviewed (the change since the merge base, or, for a review of the tests, the tests record and its files), and the record path `.../prosecutor-<lens>.json`. Wait until every prosecutor has reported.
2. **Defenders.** When the stage has `defenders`, launch that many `plumbline:defender` agents together, also in the background, named `defender-1`, `defender-2`, and so on, each with the paths of the findings records and its record path `.../defender-<k>.json`. Wait until all have reported. Skip this when the stage has no defenders (every finding then stands) or no prosecutor filed a finding.
3. `PLUMBLINE merge-review <run_id> <stage id> --round <n>` builds the stage's record and applies the survival rule. If it names a missing or invalid record, run that agent again.
4. **Detective.** When the stage has `detective: true` and the merged record's `blockers_surviving` is 0, launch `plumbline:detective` (foreground) with the merged record's path and its record path `.../detective.json`, then run `merge-review` for the same round once more. It runs only when no blocker stands.
5. `PLUMBLINE gate <run_id> <stage id>`.

## 6. When a gate fails

The stage's `on_fail` names where the run goes back to. Send that stage's agent the failure (the gate's problems, the failing criteria, the surviving findings) and run the stages from there again, in order: `build`, then `verify`, then `review` again as a new round. A review of the tests goes back to `tests`. Count the rounds per stage. When `on_fail` is `main`, or the stage's `max_rounds` is used up, stop: bring the findings and the failing criteria to the builder, and ask how to go on. Never skip the pipeline yourself; `/plumbline:override` is the builder's command, typed by the builder.

## 7. Reduce

When every stage has passed:

1. Look at `git status` and `git diff`: the change is what was meant.
2. Commit it yourself: add the files of the change and `git commit`. The commit is checked for a symlink, an absolute home path and a key-shaped secret.
3. `PLUMBLINE pass <run_id>`. It needs a clean tree and every gate passed, and it refuses when the change was edited after `verify` and `review` covered it: then run again from `verify`.
4. Report to the builder: `PLUMBLINE status --run <run_id>`, `PLUMBLINE tokens <run_id>`, the intent, the rounds each stage took, and the detective's gaps.

Leave the push to the builder. With a pass recorded for HEAD, the push hook lets it through.
