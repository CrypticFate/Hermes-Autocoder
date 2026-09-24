# Hermes Autocoder

A persistent GitHub maintenance service powered by NousResearch Hermes Agent.
It discovers owned repositories, creates plans, implements and tests changes in
Docker workers, writes reports, and opens pull requests. A human merges them.

The controller, database, model proxy, and CLI are implemented here. Deployment
starts paused and repository enrollment is off until a pilot is configured.
No GitHub credential or hosted model key is required to run the automated tests.

## Components

| Component | Responsibility |
| --- | --- |
| Controller | Repository discovery, durable scheduling, git, PRs, recovery |
| PostgreSQL | Tasks, dependencies, attempts, leases, spending and audit events |
| Model proxy | Expiring run credentials, atomic budget reservations, provider calls |
| Disposable worker | Pinned Hermes, a single repository, setup and acceptance checks |
| CLI | Goals, repository controls, budgets, status, pause, cancel and reports |

The controller has host Docker access and is a trusted service. Workers never
receive that socket, GitHub tokens, database credentials, or provider credentials.
The worker's `.git` directory is mounted read-only. No public ports are required.

## Development

```sh
uv sync --frozen
uv run ruff check .
uv run pytest -q
```

To include disposable PostgreSQL and real Hermes worker tests after building the
worker image:

```sh
WORKER_IMAGE=$(docker image inspect hermes-autocoder-worker:local --format '{{.Id}}') \
RUN_DOCKER_TESTS=1 uv run pytest -q
```

The worker integration test uses a local simulated provider; it makes no paid
model calls. It verifies the real Hermes tool loop, isolation and command timeout.

See [the deployment runbook](docs/DEPLOYMENT.md) for configuration, Docker builds,
VPS installation, pilot activation, backups and recovery. See
[architecture and contracts](docs/ARCHITECTURE.md) for state and security details.

## Operation

Commands below run inside the configured controller. Use
`docker compose exec controller autocoder -c /app/config.yaml COMMAND` for an
already-running service, or `docker compose run --rm controller COMMAND` while
the controller is stopped.

```text
doctor
repos list
repos enable OWNER/REPO
repos disable OWNER/REPO
goal add OWNER/REPO 'Fix export validation' --acceptance 'Malformed rows are rejected'
status
report show TASK_ID
budget set 10 100
pause
resume
cancel TASK_ID
retry TASK_ID
```

`budget set` arguments are daily and monthly USD ceilings, not a purchase or
provider billing limit. Prices must be configured for the selected model.
There is no default spending authorization. Account-level provider limits are
an additional control and may use different accounting.

## Repository Outputs

```text
plan/<plan-id>/overview.md
plan/<plan-id>/<sequence>-<task-id>.md
report/<task-id>/<attempt-id>.md
```

These files accompany the implementation PR; the agent never writes directly
to the default branch. Plan statuses are snapshots. Current state and final
merge status live in PostgreSQL and the CLI, avoiding extra status-only PRs.
Failed attempts also produce reports on the VPS even when publication is blocked.

## Validation Boundary

Automated tests include a seeded local repository workflow with a deterministic
agent double, real git operations, simulated GitHub responses, budget/proxy
tests, and optional Docker/PostgreSQL checks. Such tests do not establish the
quality of a real model or successful operation against a real GitHub account.
The live GitHub acceptance run and 48-hour VPS pilot require configured
credentials, a chosen model, explicit budgets, and a target VPS.

[Implementation phases](plan/README.md) and [verification reports](report/README.md)
track those separate milestones.
