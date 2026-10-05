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

The declared row is an estimate, and plumbline measures the change again: `check-diff --run` reports what it measures as, and `pass` refuses a run whose row lacks a stage the measured row selects. A change that measures larger than declared needs a new run for the measured row (`plan --intent <intent> --row <measured row>`); the problem names the files that contribute most to the size. A change that measures as size L ends before reduce: the planner proposes a split into changes of size M or smaller, and each of those gets its own run.

## 3. Start the run

For `spec-supplied` and `fix`, write the spec as a JSON `spec` record (see `${CLAUDE_PLUGIN_ROOT}/schemas/spec.json`) to `.plumbline/supplied-spec.json`. For `fix` it holds exactly one acceptance criterion and one test plan entry that reproduces the bug. Write the request, as the builder gave it, to a file too (`.plumbline/request.md` is a good place: git ignores it), because the run stores it and the review of the spec compares the spec with it. Then:

```
PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>] --request-file <file> [--calibrate]
```

`--calibrate` marks a calibration run (section 5 says what changes): ask for it only when the builder wants to measure whether the defenders can refute a finding at all.

It runs `classify` on the change (or takes your declared row), writes what `classify` produced as the run's intake record (which carries the intent), copies the record the intent supplies, stores the request as the run's `request.md` (its hash goes into the ledger first, as the intake record's does), names the run in `.plumbline/runs/ACTIVE` (the hooks read that file to know which run is in progress), then prints the plan as JSON: the `run_id`, the `stages` in order (each with its `role` or `kind`, its `reads` as paths, its record `path`, its `gate`, `on_fail`, `max_rounds` and `lenses`), the `request` file, the `stubs_dir` of the run, the repository's `commands`, and what is `supplied`. Keep it; every path below comes from it. If the command refuses, tell the builder why, and stop.

- A plan of only `intake` (or `intake` and `plan`) is size L: nothing is built at size L. If the planner ran, bring its split proposal to the builder; then stop.
- A plan with a `verify` stage or a `tests` stage needs `commands.test`, because `gate` runs the repository's test command itself. If it is missing, ask the builder to add `[commands]` with a `test` command to `plumbline.toml` and commit it, then start a new run (a run that has begun keeps its intake record).

## 4. The stages

Take the stages in order. `intake` was written by `plan`; `reduce` is section 7.

**An agent stage** (`role` is planner, test-writer, builder or verifier): launch the agent `plumbline:<role>` with the Agent tool and `run_in_background: false`, because the next step needs its result. Run the agents in the main checkout, where the run's records live. Then, if the stage has a `gate`, run `PLUMBLINE gate <run_id> <stage id>`. A pass takes you on; a failure sends you to section 6. The gate of the verify stage and the gate of the tests stage run the repository's commands on the change as it is, so leave the files as the agent left them between its stop and the gate. A test suite can take minutes: run those two gates with the Bash tool's `timeout` set to its largest value (600000 ms), or in the background, and wait for them to end. The commands' own limit is `timeout` under `[commands]` in `plumbline.toml` (900 seconds by default).

**Launch every plumbline agent, review agents included, without `model`:** each is pinned in its definition (Sonnet for the planner, test-writer, builder, prosecutor, detective and canary; Haiku for the verifier and defender), and the launch hook holds it to that pin. Leave `isolation` out as well, because the records live in the main checkout.

**Every brief** gives: the run id, the request, the path where the agent writes its record (the stage's `path`), the paths of the records it reads, and the schema of its record, `${CLAUDE_PLUGIN_ROOT}/schemas/<record>.json`. Besides:

- planner: the intake record, and the size. At size L it proposes the split. From the second round of the spec review, the text of the surviving findings that `merge-review` printed under "for the planner", verbatim.
- test-writer: the plan record, the repository's test command, the plan's `stubs_dir`, and whether the run is a fix (then the tests run against today's code and must fail on an assertion). Tell it how the tests stage runs. For a change to modules that already exist, the tests run against today's code, with the new names imported inside the test functions, so that each test fails when it runs (on the import of a name the change has yet to add, or on an assertion) and not at collection. Stubs are for brand-new modules only: they go in `stubs_dir`, outside the change and hidden from the builder, and the test-writer runs the tests with them first on the import path (for pytest, the test command followed by `-o pythonpath="<stubs_dir> ."`). Its `files_written` lists the stubs too. Its record names each test as the test appears in its file, and the gate opens those files.
- builder: the plan record. From the second round, the failing criteria and error types of the verify record (an `AC-<n>` and the kind of error), and the text of the surviving findings that `merge-review` printed under "for the builder", verbatim. The builder has no Bash and works from the spec and the source, so its brief carries no test file, test name, assertion or tests-lens finding, and no path of a review file: those stay with you and the test-writer.
- verifier: the commands from the plan, the plan record, the tests record when the row has one, and the run id (it runs `check-diff --run <run_id>`, and its `commands[].summary` is one short line such as `13 passed`). When `check-diff` reports a problem about the row, the change has outgrown the run: start a new run for the measured row (section 2), and leave the builder out of it.

If an agent comes back marked partial (it ran out of turns), its stage was too big: resume it once with SendMessage, or split the work into smaller stages.

## 5. Review units

A stage with `kind: review` runs one round at a time, `round-<n>` starting at 1. Its agents write under `.plumbline/runs/<run_id>/<stage id>/round-<n>/`, and the hook holds them to the highest round directory their stage has. Round 1's directory appears when its first agent writes. Every later round's directory comes from `gate`: when a review's gate fails with blockers standing and the stage has rounds left, `gate` creates `round-<n+1>/` itself and says so (`round 2 of 3 is open: ...`). Launch the next round's agents after that, with the new round's paths in their briefs; `merge-review` with no `--round` merges the highest round.

Start each round with `PLUMBLINE check-diff --run <run_id>`: it prints the `merge_base` and the `diff_sha256` of the change as it is. Every brief of the round carries both, and every agent copies the `diff_sha256` into its record. `merge-review` refuses a record made against a different change, so the files stay as they are until the round is merged.

1. **Prosecutors.** Launch one `plumbline:prosecutor` per lens of the stage, all in one message, with `run_in_background: true`. Each gets its lens, what is reviewed (the change since the merge base, or, for a review of the tests, the tests record and its files, or, for the review of the spec, the plan record against the run's `request.md`), the paths of the spec and of the verify record where the run has them, the `diff_sha256`, and the record path `.../prosecutor-<lens>.json`. In a calibration run, launch `plumbline:canary` in the same message as the prosecutors, with the brief of a prosecutor (the change since the merge base, the `diff_sha256`, the path of the spec) and the record paths `.../prosecutor-canary.json` and `.../canary-key.json`. Wait until every prosecutor, and the canary, has reported.
2. **Defenders.** Who defends depends on what the prosecutors filed. Skip this when the stage has no defenders (every finding then stands) or no prosecutor filed a finding.
   - **A BLOCKING finding was filed:** the full panel defends all the findings. Launch `defenders` `plumbline:defender` agents together (without `model`: the defender is pinned to Haiku), also in the background, named `defender-1`, `defender-2`, and so on, each with the paths of the findings records, the path of the spec (the plan record) where the run has one, the merge base, the `diff_sha256`, and its record path `.../defender-<k>.json`. A refutation counts when its quote occurs in the change's diff or in the finding's file.
   - **No BLOCKING finding, and the stage has `screen_defenders`:** launch that many screening defenders instead, the same agent with the same brief, named `screen-1`, `screen-2`, and so on, each with its record path `.../screen-<k>.json`. They answer all the findings. A screening defender's refutation with a valid quote ends a finding, and a `severity_claim` of BLOCKING sends it to the full panel (step 3).
   - **No BLOCKING finding, and `screen_defenders` is 0:** no defender runs, and every finding stands.

   A stage that has no `screen_defenders` runs the full panel in every round. In a calibration run the canary's record is one of the findings records: list its path with the others, written the same way, and say nothing of a canary (the launch hook holds a defender's brief to that). The canary is a finding to answer, so launch the defenders the round's rule names even when no prosecutor filed a finding. Wait until all have reported.
3. `PLUMBLINE merge-review <run_id> <stage id> --round <n>` builds the stage's record and applies the survival rule. It prints the surviving findings in two lists, one for the builder and one for the test-writer (the tests lens, and findings about test files), it names any finding whose evidence it could not find (the finding stays, marked unverified), and it says which findings it raised in severity: when enough defenders claim a higher one (`severity_claim` on a conceded verdict), the finding stands at that level, and a finding raised to BLOCKING is a blocker like any other. When a screening defender claims a finding is BLOCKING, or only a screening defender answered a finding that was filed BLOCKING, it says "panel needed": the record's `panel_needed` lists those findings, and `gate` fails until the full panel has defended them. Launch the panel then, in this round and not in the next, named `defender-1` and so on and briefed as in step 2, and tell it to answer only the findings of `panel_needed`. Run `merge-review` for the same round again: the panel's records now decide those findings, and a claim of BLOCKING from enough of the panel raises one, as in a full-panel round. In a calibration run it also prints `canary: refuted by 2 of 3 defenders` (or the like): tell the builder how the defenders answered it. A defender that concedes the canary concedes whatever it reads. If it names a missing or invalid record, or a record made against another change, run that agent again.
4. **Detective.** When the stage has `detective: true`, the merged record's `blockers_surviving` is 0 and its `panel_needed` is empty, launch `plumbline:detective` (foreground) with the merged record's path, the merge base, the `diff_sha256`, the path of the spec (the plan record) and of the tests record where the run has them, and its record path `.../detective.json`, then run `merge-review` for the same round once more. It runs only when no blocker stands.
5. `PLUMBLINE gate <run_id> <stage id>`.

**A calibration run** answers a question the normal runs leave open: can the defenders refute a finding at all? Each review round that has defenders also gets the canary, one planted false finding (steps 1 to 3 say what changes), and `merge-review` reports how the defenders answered it in the record's `canary` field. The canary is no finding of the review: it is in no list of survivors, no route, no count of blockers and no open finding, and the pass record's notes keep what it measured.

## 6. When a gate fails

`gate` prints "round k of N" for the stage and exits 1 while the stage has rounds left. The stage's `on_fail` names where the run goes back to:

- `plan` and `tests` go back to their own agent (the planner, the test-writer) with the gate's problems, for up to their `max_rounds`.
- `spec-review` goes back to `plan`. Send the planner the text under "for the planner" (the findings of the requirements lens), and run `plan` again, then `spec-review` again as a new round, the one whose directory `gate` opened. Where the intent supplied the spec, `plan` is not in the run, the failure goes to the builder (`on_fail` is `main`), and a corrected spec means a new run.
- `verify` and `review` go back to `build`. Send the builder the failing criteria and error types, and the "for the builder" text of the surviving findings, and run the stages from there again, in order: `build`, then `verify`, then `review` again as a new round, the one whose directory `gate` opened.
- A review that fails with "the screening defender claims finding X is BLOCKING" goes back to no stage: it asks for the full panel in the round it is in (section 5, step 3), and `gate` counts no round for it and opens none.
- The surviving findings under "for the test-writer" go to the test-writer, which revises the tests; then run `tests`, `verify` and `review` again. Once the build exists, the tests stage records its run of the test command and no longer expects the tests to fail.
- A review of the tests goes back to `tests`.

Each problem of the tests and verify gates starts with where it comes from: `the agent's record` (what the agent typed) or `the measured run` (what `gate` saw when it ran the repository's commands). A problem of the record goes back to the agent that wrote it. A problem of the measured run is about the change or the tests, except when it says the commands could not run (`no test command is declared`): that one is yours to settle in `plumbline.toml`.

`gate` exits 3 when the stage has used its rounds. When it exits 3, or when `on_fail` is `main`, stop: bring the findings and the failing criteria to the builder, and ask how to go on. The builder decides whether to start a new run or to override: `/plumbline:override` is the builder's command, typed by the builder.

## 7. Reduce

When every stage has passed:

1. Look at `git status` and `git diff`: the change is what was meant.
2. Commit it yourself: add the files of the change and `git commit`. The commit is checked for a symlink, an absolute home path and a key-shaped secret.
3. `PLUMBLINE pass <run_id>`. It needs a clean tree, a row that reaches reduce, and every gate passed. It measures the change again and refuses a run whose row lacks a stage the measured row selects, and it refuses when the change was edited after `verify` and `review` covered it: then run again from `verify`.
4. Report to the builder: `PLUMBLINE status --run <run_id>`, `PLUMBLINE tokens <run_id>` (an output count whose `output_lower_bound` is above 0 is at least that), the intent, the declared and measured row, the rounds each stage took, what the run leaves open: the surviving findings that are not BLOCKING and the detective's gaps (`PLUMBLINE open`, section 8), and, for a calibration run, what the canary measured (the pass record's notes have it).

Leave the push to the builder. With a pass recorded for HEAD, the push hook lets it through.

## 8. A follow-up from the open findings

`pass` writes what the run leaves open into its pass record: each surviving finding that is not BLOCKING (with its stage, id, lens, severity, file, line, claim and failure scenario) and each gap the detective named. The review files of a run stay in `.plumbline/`, which git ignores, so the pass record is where they last. `PLUMBLINE open [RUN] [--json]` lists them for the run that covers HEAD, for a named run, or for every passed run with `--all`, and `PLUMBLINE status` shows their count. When the builder asks to take some of them up, each follow-up is a run of its own, started from what the record holds:

- **A finding that describes a failure** becomes a `fix` run, one finding per run. Its failure scenario is the bug report: write the one-criterion spec from it ("this no longer happens", section 3) and start the run with `plan --intent fix`.
- **Gaps of the kinds `missing_test` and `uncovered_ac`** become a change of the `tests` row: write the tests, then start a run of that row (`plan --intent feature`, with `--row tests` while nothing has changed yet). A gap of the kind `edge_case` that describes a failure is taken up like a finding.
- **The request names where it came from**: the run id and the ids of the findings or gaps it takes up, for example `Follow-up of run <run id>, finding review/correctness-2`. Write it to the request file (section 3), so that the new run's `request.md` shows its origin.
