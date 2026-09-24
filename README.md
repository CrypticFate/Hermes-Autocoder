# Hermes Autocoder

Plan-driven coding with a human merge. You put numbered plan files in a repository's `plans/` folder (or
ask the concierge to draft them). For each plan, in order, a disposable [Hermes Agent](https://github.com/NousResearch/hermes-agent)
builder implements it with no internet access. A gatekeeper checks the result, renders `report/NN-slug.md`,
and opens a pull request from a dedicated bot account. You review and merge on GitHub; merging starts the
next plan. Nothing in the system can merge, and a GitHub ruleset enforces that.

```text
Operator ──chat──▶ Concierge (Hermes + mem0, no code tools) ──MCP──▶ Gatekeeper (controller)
   │                                                                    │  scheduler, gates, reports,
   └──review / merge on GitHub ◀──── PRs from the bot account ◀──────────┘  GitHub, Docker via socket proxy
                                                                         ▼
                                  Builder (disposable Hermes, no egress) + per-attempt service sidecars
                                         └── model calls ──▶ Model proxy (budgets, pools) ──▶ OpenRouter
```

## Components

| Component | Holds | Never holds |
| --- | --- | --- |
| Gatekeeper (`controller`) | Bot token, Docker API via socket proxy, DB access, MCP server | Provider key |
| Model proxy | Provider key, DB access for capabilities and budgets | GitHub token, Docker |
| Concierge | Its model capability, MCP token, mem0 DB access | GitHub token, provider key, Docker, terminal/file/code tools |
| Builder (per attempt) | A checkout, an attempt capability, sidecar connection env | Any long-lived credential |
| Sidecars (per attempt) | Random one-time passwords | Anything else |
| Postgres + pgvector | `autocoder` and `mem0` databases with separate roles | — |

Invariants (never merge, GitHub-enforced protection, credential isolation, no builder egress, operator-only
feedback, gatekeeper-rendered reports, …) are listed in the plan's section 2 and each has a test in
`tests/invariants/`.

## Documentation

- [Operator guide](docs/OPERATOR_GUIDE.md): plan format, daily flow, chat commands, draft PRs, reports.
- [Telegram](docs/TELEGRAM.md): chat with the concierge and get notifications from your phone.
- [Deployment](docs/DEPLOYMENT.md): bot account, ruleset, secrets, images, `docker compose up`, VPS.
- [Architecture](docs/ARCHITECTURE.md) and [project details and data flow](docs/PROJECT_DETAILS_AND_DATA_FLOW.md).
- [End-to-end scenarios](docs/E2E.md), [implementation log](docs/IMPLEMENTATION_LOG.md), [audit](docs/AUDIT.md).
- [v2 build specification](HERMES_AUTOCODER_V2_IMPLEMENTATION_PLAN.md).

## Development

```sh
uv sync --frozen
uv run ruff check .
uv run pytest -q                      # unit, workflow and invariant tests; no GitHub, Docker or model needed
RUN_DOCKER_TESTS=1 WORKER_IMAGE=<builder image id> uv run pytest -q -m docker
```

The CLI is `autocoder` (alias `hc`). On a deployed stack use `scripts/hc <command>`, which runs it inside
the controller container (and handles `chat`, `memory` and the host half of `doctor`).
