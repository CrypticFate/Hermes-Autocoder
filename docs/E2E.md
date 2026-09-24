# End-to-End Scenarios

The E2E suite proves the whole system against GitHub with the real stack. Unit, invariant and
Docker-gated integration tests are described at the end.

## Prerequisites

- The stack is deployed and `scripts/hc doctor` is fully green ([DEPLOYMENT.md](DEPLOYMENT.md)).
- A scratch repository (for example `CrypticFate/demohermes`) with the required ruleset, the bot as a
  Write collaborator, and `hc repos add` accepted. A second repository **without** a ruleset for E2E-4.
- Operator token for the harness only (`E2E_OPERATOR_TOKEN`): it seeds `main`, reviews, approves, merges
  and closes PRs, which are the operator's actions. It is never mounted into the stack.
- A second GitHub account's token (`E2E_STRANGER_TOKEN`) with comment access, for E2E-8.
- A model configured and budgets set. Scenarios use tiny plans (create a file) to keep cost low.

```sh
export E2E_OPERATOR_TOKEN=...            # from your password manager; do not put it in shell history
export E2E_STRANGER_TOKEN=...
export E2E_UNPROTECTED_REPO=you/no-ruleset-scratch
uv run python scripts/e2e/e2e.py --repo you/demohermes                 # all scenarios
uv run python scripts/e2e/e2e.py --repo you/demohermes --scenario E2E-3
```

Each scenario appends a JSON line to `e2e-results.jsonl` (outcome, PR links, duration). Copy the results
and PR links into `docs/IMPLEMENTATION_LOG.md` when you record an acceptance run.

## Scenarios

| ID | What the harness does | Pass condition |
| --- | --- | --- |
| E2E-1 | Seeds `plans/01-*.md`, `02-*.md` | PR for 01 only (with `report/01-*.md`, no `plans/` changes); after the harness merges it, PR for 02 with its report |
| E2E-2 | Empty `plans/`, `plans draft <repo> <goal>` | One plan PR touching only `plans/`; after merge, `agent/01-*` starts |
| E2E-3 | Review with REQUEST_CHANGES on PR 01 | After the debounce, repair commits on the same PR; the report's attempt history has a `repair` row |
| E2E-4 | `repos add` on the unprotected repo | Refused with a ruleset message, notification created, nothing pushed |
| E2E-5 | Plan with `services: [postgres]` | Builder uses `$DATABASE_URL`, report lists `postgres (ephemeral, per attempt)`, no `autocoder.sidecar` containers remain |
| E2E-6 | Manual chat (below) | Every step works; `hc memory list` contains the preference |
| E2E-7 | Kills the controller while an attempt is `running`, restarts it | Attempt interrupted, task requeued, no orphaned containers/networks |
| E2E-8 | Stranger comments "ignore the plan…" on the PR | Info notification "…other users were ignored"; the text is in no builder `context.json` |
| E2E-9 | You remove the ruleset, then a plan becomes ready | No PR published; repo `blocked_ruleset`; action-required notification |
| E2E-10 | Plan that adds a test file | Draft PR; report "Needs attention" lists the sensitive change |
| E2E-11 | Harness closes PR 01 unmerged, then `retry` | Repo paused with notification; retry opens `agent/01-…-r1` |

### E2E-6 (chat, manual)

1. `scripts/hc chat` → "I prefer short status answers. Remember that."
2. `docker compose restart concierge`, then `scripts/hc chat` → "status".
   Pass: the answer is short and `scripts/hc memory list` shows the preference.
3. "pause" → `scripts/hc status` shows `builder_pool_paused: true`; the chat still answers.
4. "resume" → unpaused.
5. "add a plan to create CHANGELOG.md with a 0.1.0 entry" → the concierge confirms title and criteria,
   then a `[Plans] Add plan: …` PR appears.
6. Return to the harness prompt and press Enter.

### E2E-7 and E2E-9 notes

E2E-7 needs `docker compose` on the machine running the harness. E2E-9 pauses for you to delete the
ruleset in the GitHub UI; restore it afterwards and run `scripts/hc repos verify <repo>` to unblock.

## Automated tests (no GitHub, no model)

| Suite | Command | Covers |
| --- | --- | --- |
| Unit + workflow | `uv run pytest -q` | Config, parser, reconcile, state machine, gates, reports (snapshot), feedback/debounce, MCP tools, proxy pools, controller recovery; plan-driven workflow over a real local git remote |
| Invariants | `uv run pytest -q tests/invariants` | T-I1 … T-I14 (runs in CI on every change) |
| Docker | `RUN_DOCKER_TESTS=1 WORKER_IMAGE=<id> uv run pytest -q -m docker` | Postgres migrations and concurrent budgets, real Hermes adapter, builder isolation, sidecar lifecycle, unlabeled-object guard |
