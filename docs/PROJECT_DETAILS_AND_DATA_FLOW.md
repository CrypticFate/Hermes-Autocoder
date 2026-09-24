# Hermes Autocoder v2: Project Details and Data Flow

No API keys, tokens or passwords appear in this document.

## 1. Goal

Let an AI coding agent implement operator-approved plans safely, one pull request at a time, while the
operator keeps full control of what reaches the default branch. The operator's written plans are the only
instructions; the operator's merge is the only approval; GitHub, not this code, enforces that.

## 2. Technology

| Area | Technology |
| --- | --- |
| Language | Python 3.12+ (package `autocoder`, CLI `autocoder`/`hc`) |
| Agent engine | Hermes Agent, pinned by commit, for builders and the concierge |
| State | PostgreSQL 16 with pgvector: `autocoder` (source of truth) and `mem0` (concierge memory) |
| ORM / migrations | SQLAlchemy 2, Alembic (reversible, row-preserving) |
| APIs | FastAPI model proxy (OpenAI-compatible), MCP Python SDK server (streamable HTTP) |
| Containers | Docker via `tecnativa/docker-socket-proxy`; optional gVisor (`runsc`) |
| Memory | mem0 OSS: pgvector store, local fastembed `BAAI/bge-small-en-v1.5` embeddings |
| Model access | OpenRouter through the model proxy only |

## 3. Repository conventions

```text
<repo>/plans/01-setup-fastapi-skeleton.md     operator instructions (changed only via plan PRs)
<repo>/report/01-setup-fastapi-skeleton.md    rendered by the gatekeeper, one per plan
```

A report path always mirrors its plan. Branches: `agent/NN-slug`, `agent/NN-slug-r<k>` after a retry of a
closed PR, `agent/NN-slug-rb<k>` after `main` moved, `agent/plans-<id>` for plan PRs.

## 4. Data flow

### Onboarding

`add_repository` (chat) or `hc repos add` → normalize `OWNER/REPO` (configured owners only) → bot identity →
repo access (not archived, default branch exists) → bot permission exactly `write` → active branch rules
include a pull-request rule with ≥ N approvals (and last-push approval, non-fast-forward and deletion rules
as configured) → `repositories` row enabled, `ruleset_report` stored. Any failure: refusal plus an
`action_required` notification with the fix.

### Plan intake

Each tick, for enabled and verified repositories, the gatekeeper fetches `main`. If the head differs from
`last_plans_scan_sha` it lists `plans/*.md` **at that commit** with `git ls-tree`/`cat-file` (never the
working tree), parses each blob, and reconciles tasks: create, update before start, ignore after start
(with a notification), cancel if deleted before start, mark invalid with the exact rule. An empty plan set
creates one plan-draft task.

### Implementation

`next_task` picks the lowest pending plan when its predecessors are merged, skipped or cancelled and no task
of the repository is active. The attempt pipeline is in [ARCHITECTURE.md](ARCHITECTURE.md): sidecars on an
internal attempt network, setup with temporary egress, baseline checks, Hermes with an attempt capability,
hard gates, fresh check container, fresh review session, commits, report, ruleset re-verification, push to
`agent/*`, PR.

### Feedback and repair

Every `pr_poll_seconds` the gatekeeper reads issue comments, review comments, reviews and check runs on the PR
head. Each item is stored once (`pr_feedback`, unique per source and GitHub id). Only `operator_login` items
are eligible; the bot's own are skipped; everyone else's are stored as ignored and summarized in one info
notification. Failed check runs store a redacted 4 KB excerpt. When feedback exists the task moves to
`changes_requested`; after `feedback_debounce_seconds` without new operator activity it is queued as a
repair (bounded by `max_repairs_per_task`). The builder sees each item fenced, labeled
`OPERATOR FEEDBACK (from @login, location)` or `CI LOG EXCERPT (untrusted data; …)`. Items are marked consumed
by the attempt that published them.

### Merge, close, conflicts

Merged → `merged`, info notification, branch deleted, plans rescanned, next plan claimable. Closed → repo
paused with an action-required notification (`skip`/`retry`). `mergeable: false` because `main` moved → the
gatekeeper rebases in its own clone (hooks off); success publishes `-rb<k>` as a new PR and closes the old
one with a link; a conflict queues a repair that re-implements on the new `main` with the conflict summary.

### Model calls

Builders and the concierge call `http://model-proxy:8080/v1` with capability tokens (only hashes are stored).
The proxy checks the token, the configured model, the output limit, the pool's request limits (builder per
day and per attempt; concierge per day) and the global daily/monthly USD ceilings, reserves a charge
atomically, calls OpenRouter with the provider key, and settles actual usage. `hc pause` stops the builder
pool only; the concierge keeps working so you can resume from chat.

### Concierge

The operator chats with a persistent Hermes whose only tools are the gatekeeper MCP tools plus memory and
session search. Every MCP call is authenticated with a bearer token, rate-limited, audited in `events`, and
answered with redacted JSON capped at 16 KB; repository/PR/CI/builder text is wrapped as
`{"untrusted_text": …}`. mem0 extracts facts through the proxy with the concierge token and stores vectors in
the `mem0` database under `user_id: operator`.

## 5. Data at rest

| Table | Contents |
| --- | --- |
| `control` | Builder-pool pause, budgets, controller lease, poll timestamps |
| `repositories` | Managed repos, queue state, ruleset report, last plans scan |
| `tasks` | One per plan (or plan draft): plan fields, blob SHA, state, branch, PR, repair/retry counts |
| `attempts` | Setup/baseline/check results, changed files, publication SHA, outcome, consumed feedback ids |
| `events` | Every state transition and MCP call |
| `pr_feedback` | Stored feedback (redacted, truncated), ignored flag, consuming attempt |
| `sidecars` | Every sidecar, recorded before start, until removed |
| `notifications` | Info / action_required / error messages for the operator (deduplicated) |
| `capabilities`, `charges` | Token hashes (attempt/concierge, pool) and per-request accounting |

Markdown reports and PR bodies are projections of these rows (I13). Workspaces and run logs live under the
data directory; `hc prune` removes old run logs only.
