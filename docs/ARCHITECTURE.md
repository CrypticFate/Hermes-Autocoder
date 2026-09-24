# Architecture and Contracts (v2)

## Modules

| Module | Responsibility |
| --- | --- |
| `controller.py` | Tick loop: reconcile → poll PRs → refresh repos/plans → claim → execute → publish |
| `scheduler.py` | The only place task states change (`transition`), sequential `next_task`, claims, pause/skip/retry |
| `onboarding.py` | URL normalization, bot identity/permission, ruleset verification (`verify_repository`) |
| `plans.py` | Discovery from committed blobs, strict parsing, idempotent reconcile, `add_plan`, drafts |
| `worker.py` | Builder/check container lifecycle, hardening, setup-only egress, isolation probe |
| `hermes_runner.py` | Builder-side Hermes config (memory off, terminal+file only) and prompt (Appendix F) |
| `sidecars.py`, `networks.py` | Allowlisted per-attempt services on internal `att-<id>` networks; labeled-object guard |
| `gitops.py` | Git with hooks/fsmonitor disabled, gates, sensitive-change detection, agent-only push, rebase |
| `reports.py` | Report (Appendix C) and PR body (Appendix D) rendering from DB rows and measured checks |
| `feedback.py` | PR feedback collection, operator filter, debounce, fenced CI excerpts, repair context |
| `github.py` | `GitHubClient`: explicit methods only, no generic request, no merge method |
| `budget.py`, `proxy.py` | Capabilities (attempt/concierge), pools, ceilings; OpenAI-compatible proxy |
| `mcp_server.py` | `Operations` (shared with the CLI) and the Appendix G MCP registry |
| `notifications.py`, `redaction.py` | Deduplicated notifications; redaction for logs and all outbound text |

## Task state machine

```text
pending → queued → preparing → running → validating → publishing → pr_open ─┬→ merged
   │         ▲                              │                        │        ├→ closed_unmerged → (retry) queued
   │         └──────── attempts remain ─────┘                        │        └→ changes_requested → queued (repair)
   ├→ invalid → pending (plan fixed)        attempts exhausted → blocked → (retry) queued
   ├→ cancelled (plan deleted)   └→ skipped (operator)
```

`scheduler.transition` validates every move against `TRANSITIONS`, writes an `events` row in the same
transaction, and handles side effects (a closed implementation PR pauses its repository). A test forbids
state assignments anywhere else. `next_task` returns the lowest pending plan only when no task of the
repository is active and every lower plan is merged, skipped or cancelled; plan drafts go first.
One builder runs at a time across all repositories under a database lease.

## Attempt pipeline

1. Re-verify the ruleset (cached 10 minutes), fetch `main`, create or reuse `agent/NN-slug`.
2. Create `att-<attempt>` (internal), start allowlisted sidecars and wait for their health checks.
3. Start the builder on `hermes-workers` + `att-<attempt>`; attach `hermes-setup-egress` only for setup
   commands, detach, then prove isolation with a probe.
4. Run baseline checks, issue an attempt capability, run Hermes with `context.json` (plan fields, service
   env **names**, checks, fenced repair data). The token is revoked when Hermes exits.
5. Validate `result.json` (criteria 1:1 with the plan), run the hard gates (protected paths, secrets,
   symlinks, binaries, size, escapes, `.gitattributes` drivers, `.gitmodules`, `plans/`, `report/`).
6. Run checks in a **fresh** read-only container (no token, no egress), then a fresh Hermes review session.
   A tree digest proves neither changed the implementation.
7. Commit `plan NN: <title>` (or `address review (repair k)`), render and commit `report/NN-slug.md`.
8. Publish: force re-verify the ruleset and bot permission, check HEAD equals the validated commit, push
   `refs/heads/agent/*` only (asserted in code), create/update the PR (ready or draft), add the label.
9. `finally`: revoke tokens, remove containers, sidecars and the network (only labeled objects).

## Contracts

- `TaskContext` (controller → builder): kind, mode, plan fields, `plan_path`/`report_path`, base SHA,
  branch, service env names, checks, `repair` (`previous_attempt_summary`, `operator_feedback[]`,
  `ci_failures[]`, optional `conflict`), model and limits.
- `RunResult` (builder → controller, untrusted): `summary` ≤ 2,000 chars, `criteria[]` matching the plan
  1:1 in order, `files_changed_rationale[]`, `tests_added[]`, `deviations[]`, `follow_ups[]`,
  `open_questions[]`. Extra fields are rejected.
- `ReviewResult`: `accepted`, `acceptance_met[]` in criterion order, `objections[]`.

## Publication decision

Ready PR only if every gatekeeper check passed, the reviewer marked every criterion met with no
objections, and no sensitive path changed. Otherwise a draft whose report and body start with
"Needs attention". A hard-gate failure publishes nothing and retries while attempts remain.

## Recovery

On startup and every tick: attempts left running are marked `interrupted`, their tokens revoked, their
labeled containers/networks and sidecar rows removed, and the task requeued (or blocked when attempts
are exhausted). `publication_pending` attempts are re-published without rerunning the builder. A sweep
removes any labeled object whose attempt is finished or unknown. PR state changes that happened while the
controller was down are applied on the next poll.
