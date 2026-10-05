---
name: run
description: Run a change through plumbline's pipeline in an adopted repository - settle its intent and size, start the run, let the orchestrator agent take it through the stages (plan, tests, build, verify, review), then commit and record a pass so it can be pushed. Use when a change has to be made, or checked, before it is pushed.
argument-hint: what to change, or the change to review
allowed-tools: Bash(python3 *) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git add *) Bash(git commit *)
---

# Run the pipeline

You are the reduce, and the builder's contact. You settle the intent and the size, start the run, put to the builder what only the builder decides, and alone commit and record the pass. The orchestrator agent (`plumbline:orchestrator`) takes the run through its stages in between, and the stage agents write typed records. `plumbline.py` writes every other file of a run: the intake record, the review records that `merge-review` builds, the ledger and the pass record.

`PLUMBLINE` below stands for `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py"`.

The request: $ARGUMENTS

If that is empty, ask the builder what to change, and wait.

## 0. Before you start

- The repository has adopted plumbline when it has a `plumbline.toml`. If it has not, tell the builder that `/plumbline:init` adopts it, and stop.
- Uncommitted work is fine: it is the change, or part of it.
- **A run may be in progress already**, after a compaction or a restart. `.plumbline/runs/ACTIVE` names it. Run `PLUMBLINE status`: when it shows a run with stages that have not passed, start a leg for that run (section 3). The orchestrator takes its stages in order, from the first that is not `pass`, `supplied` or `recorded`. Start a new run only when the builder asks for a new change, or when the run is stuck: its row is too small for the change, or its stages have used their rounds.

## 1. Intake

Settle the intent and the row before anything is planned, state both in a sentence each, and let the builder confirm or correct them in one question. The question also lists any other plausible reading of the request.

**The intent** says why the change is made, and decides which stages of the row run. Ask for it, or infer it and give the reason. Skip the question when the request names the intent.

| Intent | When | What changes |
| --- | --- | --- |
| `feature` | new behaviour (the default) | the planner writes the spec |
| `spec-supplied` | the builder or an issue gives the spec | the planner is skipped; you give `--spec` |
| `fix` | a bug | reproduce first: a one-criterion spec from the bug report ("this no longer happens"), and its tests must fail on today's code, on an assertion |
| `refactor` | behaviour stays the same | no planner, no new tests; the shipped template spec says behaviour is unchanged and every existing test passes, and the diff leaves the test files as they are |
| `review-only` | the change already exists | nothing is built: verify and review only |

**The row.** plumbline measures a change that already exists: its type and its size (S up to 50 weighted changed lines, M up to 400, L more; a line of a test file counts for half) select the row. When nothing has changed yet, as with a feature, a fix or a refactor on a clean tree, estimate the change and declare the row yourself: `--row code.M` for a code change of size M, a bare type such as `--row docs` for a flat row.

The declared row is an estimate, and plumbline measures the change again: `check-diff --run` reports what it measures as, and `pass` refuses a run whose row lacks a stage the measured row selects. A change that measures larger than declared needs a new run for the measured row (`plan --intent <intent> --row <measured row>`). A change that measures as size L ends before reduce: the planner proposes a split into changes of size M or smaller, and each of those gets its own run.

## 2. Start the run

For `spec-supplied` and `fix`, write the spec as a JSON `spec` record (see `${CLAUDE_PLUGIN_ROOT}/schemas/spec.json`) to `.plumbline/supplied-spec.json`. For `fix` it holds exactly one acceptance criterion and one test plan entry that reproduces the bug. Write the request, as the builder gave it, to a file too (`.plumbline/request.md` is a good place: git ignores it), because the run stores it and the review of the spec compares the spec with it. Then:

```
PLUMBLINE plan --intent <intent> [--row <row>] [--spec <file>] --request-file <file> [--calibrate]
```

`--calibrate` marks a calibration run: ask for it only when the builder wants to measure whether the defenders can refute a finding at all (each review round that has defenders then also gets a canary, a planted false finding).

It runs `classify` on the change (or takes your declared row), writes the run's intake record (which carries the intent), copies the record the intent supplies, stores the request as the run's `request.md` (its hash goes into the ledger first, as the intake record's does), names the run in `.plumbline/runs/ACTIVE` (the hooks read that file to know which run is in progress), and prints the plan as JSON. Keep the `run_id` it prints: the orchestrator reads the rest of the plan itself. If the command refuses, tell the builder why, and stop.

## 3. Run a leg

Launch the agent `plumbline:orchestrator` with the Agent tool, `run_in_background: true`, without `model` (it is pinned to Sonnet) and without `isolation`. Its brief is the run id and, after a report that needed the builder, the builder's decision in a sentence or two. Then wait for its report, without polling and without commands of your own: the completion arrives as one notification, which is one turn of yours.

The report is at most 15 lines: the run id, where the leg stopped and why, the options with the orchestrator's recommendation, the rounds each stage used, and the open findings as ids with one line each. A leg that ends with no report of that shape (it reached its turn limit, say) is followed by another leg with the same run id: the run's state is on disk, so the next leg continues from the first stage that has not passed.

## 4. A report that needs the builder

A leg ends with every gate passed (section 5), or with a decision only the builder can make: a hook refused a call that the orchestrator would have had to work around, the change measures larger than its row or at size L, a stage reached `max_rounds`, non-blocking findings or gaps are left once every gate passed (fix or ship), or an error stays unresolved. Ask with AskUserQuestion: give the options as the report lists them, with the orchestrator's recommendation first. Then act on the answer:

- The run goes on (fix the open findings first, or ship them): start the next leg (section 3) with the builder's decision in its brief. To ship, go on to section 5.
- A new run (for the measured row, or for one part of a split at size L): start it (sections 1 and 2).
- The builder skips the pipeline: `/plumbline:override` is the builder's command, typed by the builder.
- `plumbline.toml` needs a change (the `test` command under `[commands]`, say): ask the builder to make it and commit it, then start a new run.

## 5. When every gate has passed

1. Look at `git status` and `git diff`: the change is what was meant.
2. Commit it yourself: add the files of the change and `git commit`. The commit is checked for a symlink, an absolute home path and a key-shaped secret.
3. `PLUMBLINE pass <run_id>`. It needs a clean tree, a row that reaches reduce, and every gate passed. It measures the change again and refuses a run whose row lacks a stage the measured row selects, and it refuses when the change was edited after `verify` and `review` covered it: then start a leg again, which runs from `verify`.
4. Report to the builder: `PLUMBLINE status --run <run_id>` (it ends with the rounds each stage took), `PLUMBLINE tokens <run_id>` (an output count whose `output_lower_bound` is above 0 is at least that; `by_model` is what the stage agents used and `orchestration` what the orchestrator's legs used, which sat in your own session before), the intent, the declared and measured row, the spec's `risks` and the build note's `assumptions`, what the run leaves open: the surviving findings that are not BLOCKING and the detective's gaps (`PLUMBLINE open`, section 7), and, for a calibration run, what the canary measured (the pass record's notes have it).

## 6. Push

Leave the push to the builder, until the builder asks you to push. With a pass recorded for HEAD, the push hook lets it through.

## 7. A follow-up from the open findings

`pass` writes what the run leaves open into its pass record: each surviving finding that is not BLOCKING (with its stage, id, lens, severity, file, line, claim and failure scenario) and each gap the detective named. The review files of a run stay in `.plumbline/`, which git ignores, so the pass record is where they last. `PLUMBLINE open [RUN] [--json]` lists them for the run that covers HEAD, for a named run, or for every passed run with `--all`, and `PLUMBLINE status` shows their count. When the builder asks to take some of them up, each follow-up is a run of its own, started from what the record holds:

- **A finding that describes a failure** becomes a `fix` run, one finding per run. Its failure scenario is the bug report: write the one-criterion spec from it ("this no longer happens", section 2) and start the run with `plan --intent fix`.
- **Gaps of the kinds `missing_test` and `uncovered_ac`** become a change of the `tests` row: write the tests, then start a run of that row (`plan --intent feature`, with `--row tests` while nothing has changed yet). A gap of the kind `edge_case` that describes a failure is taken up like a finding.
- **The request names where it came from**: the run id and the ids of the findings or gaps it takes up, for example `Follow-up of run <run id>, finding review/correctness-2`. Write it to the request file (section 2), so that the new run's `request.md` shows its origin.

## If the orchestrator is not available

When the Agent tool has no `plumbline:orchestrator`, follow the procedure in `${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.md` yourself, in this session: read it, take the stages in order as it says, and put to the builder what it would hand back.
