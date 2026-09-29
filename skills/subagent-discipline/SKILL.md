---
name: subagent-discipline
description: How to carry out any task in this workspace that is more than a lookup - through subagents sized to the job, Haiku for mechanical steps and Sonnet where judgement matters, with the orchestrating session keeping the brief, the review and the decisions. Use at the start of any multi-step task, and whenever about to do substantive work in the main session.
---

# Appropriately sized subagents, always

The builder's standing rule: do the work through subagents, each on the smallest model and effort that does its job. The builder's Claude Code usage is billed the way API usage is, and orchestrating at full effort in the main session is the expensive path.

## Sizing
- **Haiku** for mechanical work from a precise brief: running commands, applying scripted edits, git and file plumbing, collecting numbers, polling for files.
- **Sonnet** where judgement matters: code changes that need their surroundings read, reviews, audits, personas, blind judges.
- The main session keeps the brief, the review of what comes back, and the decisions. A single-fact lookup in a known file is done directly; it does not need an agent.

## Briefs
- Give each agent its own scratch directory and exact file paths, and say what it must return.
- Say what it must never read or print: .env files, environment variables, keys, credentials, and anything the project marks private.
- Give the exact commit trailer. After the agent commits, check it with `git log -1 --format='%(trailers:key=Co-Authored-By)'`: smaller models substitute their own model name.
- Say "once" when once is meant. Smaller models apply "in each of three places" literally.

## Shared files: map and reduce
Subagents and worktrees are the maps; the main session, the one the builder talks to, is the reduce. Files the whole project shares (status documents, the decision log, memory, and anything synced out of the repository) are written only by the main session.
- Workers never edit those files and never run the sync. Their briefs say so, and their reports carry what the files need: numbers, decisions made or proposed, open questions, status lines.
- The main session consolidates the reports into those files. It resolves any conflict between workers deliberately and says how, and never lets the last writer win. It writes each file once and syncs once, at the end.

## Running them
- Agents working in parallel need separate git worktrees; agents running one after another may share one.
- An orchestrating agent waits for its children's output files in a foreground polling loop (plain `sleep` may be blocked; use a short Python loop), not on notifications.
- Read every diff a small model produces before building on it: small models add checks nobody asked for.
- In the report to the builder, say which model did what.
