# Implementation Verification: 2026-09-23

## Outcome

Implemented the Hermes-based autonomous maintenance service in this workspace.
The controller, model proxy, durable database schema, worker adapter, repository
planning, coding/validation, PR management, reports, CLI, Docker deployment,
backup script and daily backup timer are present.

Local verification passed. This is not a record of a live GitHub deployment or
a completed 48-hour VPS pilot.

## Verified Results

| Check | Result |
| --- | --- |
| Ruff static checks | Passed |
| Full pytest suite with Docker and worker enabled | 35 passed in 46.94 seconds |
| PostgreSQL migrations and concurrent spending reservations | Passed in disposable PostgreSQL container |
| Seeded repository workflow | Plan, code change, failing baseline, passing check, report, simulated PR and merge passed |
| Real Hermes adapter | Imported pinned Hermes and completed a real terminal tool call using a local simulated provider |
| Worker isolation and command timeout | Passed |
| Controller container initialization | Migrated database and confirmed initial paused state |
| Controller and worker Docker builds | Passed |
| Compose configuration validation | Passed |
| Backup shell syntax | Passed |

The test run emitted one upstream Starlette/httpx deprecation warning. No test
failed or was skipped in the full Docker-enabled run.

## Built Artifacts

- Hermes source commit: `38c9611791d3c8eccee5bb3fdad8075ec1d58565`
- Controller image: `hermes-autocoder-controller:local`
- Controller image ID: `sha256:09d8cedc0f3eb5a6ebcaf47fd02137e947eb2c76bd6a9279e32ea54647bec751`
- Worker image: `hermes-autocoder-worker:local`
- Worker image ID: `sha256:10d326e1783fd0c4233a87328d253f57e1fec240378a8c9f5d34aefddd0f1279`

Hermes dependencies are installed from its upstream lock using uv 0.11.6,
independently of the controller environment. Provider completions are buffered,
accounted, and adapted to SSE for Hermes. The adapter test exercises this
compatibility path and a real tool invocation, but the response provider is a
deterministic local fixture, not an AI model.

## Evidence Boundaries

- No paid model requests or authenticated GitHub writes were performed.
- Existing `key.text` was not read or modified; Git and Docker ignore rules exclude it.
- GitHub pagination, rate limits, PR identity, feedback and merge handling use
  simulated API responses in automated tests.
- Restart and interrupted-publication behavior are exercised by controller tests;
  VPS reboot, full backup restoration and a live pilot still need operational testing.
- The service has not been activated against any real repository or deployed to a VPS.

## Required Deployment Inputs

Configure owner-to-token mappings, a hosted model and its pricing, explicit daily
and monthly spending limits, the service's own repository exclusion, a pilot
repository, and the target VPS. Follow `docs/DEPLOYMENT.md`, run the real GitHub
acceptance flow, then observe the 48-hour pilot before enabling all owned repositories.

## Reproduce

```sh
uv sync --frozen
uv run ruff check .
WORKER_IMAGE=$(docker image inspect hermes-autocoder-worker:local --format '{{.Id}}') \
RUN_DOCKER_TESTS=1 uv run pytest -q
docker compose config --quiet
bash -n scripts/backup.sh
```
