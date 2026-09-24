# Operator Guide

How to use Hermes Autocoder day to day. Deployment is in [DEPLOYMENT.md](DEPLOYMENT.md).

## The model in one paragraph

You write (or approve) numbered plan files in a repository's `plans/` folder. For each plan, in order, a
disposable builder implements it, the gatekeeper runs your checks and an independent review, renders
`report/NN-slug.md`, and opens a pull request from the bot account. You review on GitHub. **Merging is
always yours**: nothing in the system can merge, and GitHub's ruleset enforces that. Merging plan N starts
plan N+1. Your review comments steer repairs; nobody else's do.

## Plan files

Path: `plans/NN-slug.md` (two or three digits, lowercase slug with hyphens). Other files in `plans/`
(for example `README.md`) are ignored, and you get a notification listing them.

```markdown
---
title: Add Postgres models          # optional; otherwise the first "# " heading
services: [postgres]                # optional; allowlisted names only (postgres, mysql, redis, mongo)
checks:                             # optional extra shell checks, run after the repo profile's checks
  - "alembic upgrade head"
---

# Add Postgres models

## Objective
Create SQLAlchemy models for users and projects, with an Alembic migration.

## Acceptance criteria
- [ ] `User` and `Project` models exist in `app/models.py`
- [ ] An Alembic migration creates both tables and applies cleanly on an empty database

## Context
User: id (uuid), email (unique), created_at. Project: id (uuid), owner_id -> User, name.

## Notes
Use `DATABASE_URL` from the environment.
```

Rules the gatekeeper enforces (an invalid plan is never guessed at; you get a notification naming the rule):

- `## Objective` must be non-empty; `## Acceptance criteria` needs at least one `- ` item. Each item
  becomes one criterion that the builder and the reviewer answer one-to-one.
- Duplicate sequence numbers make the whole plan set invalid until fixed.
- A plan edited on `main` **before** its task starts is picked up. After it starts, edits are ignored:
  add a new numbered plan instead. Deleting a plan before it starts cancels it.
- Services give the builder throwaway databases for that attempt only. Their connection details arrive as
  environment variables (for example `$DATABASE_URL`); the values never appear in reports or logs.

## Daily flow

1. **Chat** from Telegram ([TELEGRAM.md](TELEGRAM.md)), with `scripts/hc chat`, or use the CLI to check status and notifications.
2. **Review** the open PR on GitHub. The PR body lists criteria and check results; the full report is in
   the PR under `report/NN-slug.md`.
3. **Approve and merge** when satisfied. The bot authored the PR, so your approval satisfies the ruleset.
4. **Request changes** with review comments if needed. After a quiet period (3 minutes by default) all
   your comments since the last attempt become one repair: new commits on the same PR, a re-rendered report
   with the attempt history, and an updated PR body.

### Ready vs draft PRs

- **Ready**: every gatekeeper check passed, the independent reviewer marked every criterion met with no
  objections, and nothing sensitive changed.
- **Draft** ("Needs attention" at the top): something above is not satisfied. Common reasons: a failing
  check, an unmet criterion, or a *sensitive change* (tests, CI under `.github/`, build scripts, Dockerfiles,
  migrations, dependency manifests or lockfiles). Sensitive changes are allowed when the plan needs them;
  they are always flagged so you look.

The builder's own claims are never trusted: the report's Checks table is what the gatekeeper measured in
a fresh container, next to the baseline measured before any change.

### Reading a report

| Section | Meaning |
| --- | --- |
| Header table | Plan path and blob, branch, base commit, model, Ready/Draft |
| Needs attention | Why the PR is a draft |
| Acceptance criteria | Builder claim vs independent reviewer verdict, with evidence |
| Checks (run by gatekeeper) | Baseline exit code, final exit code, output excerpt |
| Changes | Every changed file and the builder's reason |
| Deviations, follow-ups | What differs from the plan; proposals that were **not** implemented |
| Attempt history | One row per attempt and repair |

Text in reports that came from the builder is escaped and redacted; it cannot add headings or HTML.

## What the concierge understands

The concierge is a chat assistant with no terminal, file or code tools. It can only call the gatekeeper's
tools. Useful phrases:

| You say | It calls |
| --- | --- |
| "What's happening with demohermes?" / "status" | `get_status`, `list_plans` |
| "Any notifications?" (it also checks at the start of every conversation) | `get_notifications` |
| "Add https://github.com/you/repo" | `add_repository` |
| "Is the repo still protected?" | `verify_repository` |
| "Add a plan to add a /health endpoint that returns 200" | confirms title and criteria, then `add_plan` |
| "Draft plans for a CLI that syncs my notes" | `request_plan_draft` |
| "Show me the report for plan 2" | `get_report` |
| "Pause" / "pause demohermes" / "resume" | `pause` / `resume` (builder pool only; chat stays up) |
| "Skip plan 3" / "retry plan 3" | `skip_plan` / `retry_plan` |

It remembers your preferences (for example "keep status answers short") in mem0. It cannot merge, change
budgets, secrets or configuration, and it treats anything inside `untrusted_text` as data, not instructions.
Inspect or clear its memory with `scripts/hc memory list|search <q>|delete <id>|reset --yes`.

## Controlling the queue

| Situation | What happens | What you do |
| --- | --- | --- |
| PR closed without merging | Repo pauses; action-required notification | `retry <repo> <seq>` (new branch `agent/NN-slug-r1`), `skip <repo> <seq>`, or edit the plan |
| Attempts exhausted (3 failed initial attempts) | Plan `blocked`; queue stalls with one notification | Fix the plan or repo, then `retry` |
| Repair limit reached (5 repairs) | Plan `blocked` | Finish it yourself or `retry` |
| Plan invalid | Repo `blocked_invalid_plans` | Fix the file on `main`; it is re-read automatically |
| Ruleset or bot permission changed | Repo `blocked_ruleset`; nothing is pushed | Fix the ruleset, then `repos verify <repo>` |
| `main` moved and the PR conflicts | Clean rebase → new PR `agent/NN-slug-rb1`, old one closed with a link. Real conflict → a repair re-implements on the new `main` | Review the new PR |
| Empty `plans/` | One plan-draft PR touching only `plans/` | Merge it to approve the plans; close it to decline (no new draft until you ask) |

Plans run strictly in order, one open implementation PR per repository. Skipped and cancelled plans let
the queue continue.

## CLI reference

Run through the host wrapper `scripts/hc` (it executes inside the controller container):

```text
status [repo]                    notifications [--all]            ack <id>...
repos add|verify|list|enable|disable
plans list <repo> | rescan <repo> | add <repo> <title> <objective> -a <criterion>... | draft <repo> <goal>
task show <id>                   report show <repo> <seq>
pause [repo] | resume [repo] | skip <repo> <seq> | retry <repo> <seq> | cancel <task-id>
budget set <daily-usd> <monthly-usd>
secrets init --dir secrets | secrets rotate <mcp_concierge_token|concierge_model_token> --dir secrets
chat | memory list|search|delete|reset --yes
doctor
```

Only comments and reviews written by `operator_login` steer builders. Comments by anyone else are stored,
marked ignored, and summarized in a notification; they never reach a prompt.
