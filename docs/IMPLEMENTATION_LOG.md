# Implementation Log

## 2026-09-24: Phase 0 Audit

- Read the full v2 specification and audited existing production code, deployment, migrations, tests and pilot helpers.
- Added AUDIT.md with I1-I14 and every numbered task in Phases 1-15, plus the legacy artifact path inventory.
- Baseline `uv run pytest`: 40 passed, 2 skipped, 1 Starlette/httpx deprecation warning. Docker/Postgres integration tests are opt-in and were not run by this command.
- Baseline `uv run ruff check .`: failed on one import ordering issue inside a generated pilot checkout under data/. Added an explicit Ruff data/ exclusion; this is tool configuration only. No production code changed in Phase 0.
- No root Git metadata exists; Git status and history checks cannot run here. Existing secret files and live configuration were not read or changed.
- Phase 0 final checks: `uv run ruff check .` passed; `uv run pytest` passed (40 passed, 2 skipped, 1 warning). All Phase 0 acceptance checks passed before Phase 1 production edits.

## 2026-09-24: Phase 1 Configuration, Secrets and Identity

- Added Appendix A configuration, strict identity/sequential/image validation, shell-command profiles, and the hc alias. Existing runtime callers consume nested settings; legacy live config is deliberately not rewritten.
- Added GitHubClient with private HTTP primitives, dedicated bot identity verification, App JWT/installation token refresh, and configured commit attribution. Retired the v1 pilot helpers, including direct default-branch seeding.
- Added shared loaded-secret/pattern redaction and logging filters. Removed the controller's provider secret and blanket secrets-directory mounts. Only placeholder material was added; no existing credentials were read or changed.
- Verification: `uv run pytest`: 84 passed, 2 skipped, 1 existing third-party warning. `uv run ruff check .`: passed. CLI identity checks use HTTP fixtures, not live GitHub credentials.
- API verification: GitHub REST version 2022-11-28 remains pinned. App mode verifies GET /app with an RS256 JWT; GET /user is for bot PATs. See https://docs.github.com/en/rest/apps/apps and https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app . PyJWT[crypto] is pinned to 2.10.1.
- Deviation: retained deployment/runtime fields absent from Appendix A so existing persistence and lease behavior remains usable during migration. Example YAML exactly follows Appendix A; real image pins must replace its placeholders.
- Live bot identity and Docker deployment acceptance pending credentials and deployed images.

## Phase Gate Authorization

The operator explicitly instructed: "Continue all phases; defer live checks" on 2026-09-24. Subsequent phase gates use offline tests; credential-dependent GitHub, provider, Docker image and VPS checks remain explicitly pending. No mock result will be reported as live acceptance.

## 2026-09-24: Phase 2 Data Model

- Added reversible 0002 migration, repository verification state, numbered-plan metadata, feedback, sidecars, notifications and capability/accounting pool fields. Existing v1 states remain allowed to preserve historical rows; the v2 scheduler will restrict new transitions.
- Added feedback cursors, ignored flags, notification deduplication and retry counters needed by later phases. Charges also allow null attempt IDs for concierge accounting. New repository timestamps use timezone-aware columns and new plan JSON fields use PostgreSQL JSONB with SQLite variants.
- Upgrade/downgrade/upgrade against a v1 SQLite database preserved existing repository/control rows; metadata comparison passed. Downgrade refuses to discard concierge charges with null attempt IDs.
- `uv run ruff check .` passed; `uv run pytest`: 85 passed, 2 skipped, 1 warning. PostgreSQL live migration testing remains pending.

## 2026-09-24: Phase 3 Onboarding

- Added strict GitHub URL normalization, configured-owner checks, bot identity/access/permission checks and active default-branch rules validation. Archived, uninitialized, overprivileged and unprotected repositories fail closed with notifications.
- Added repos add/verify CLI commands and forced fresh verification before the existing publisher can push. Cached status is available for scheduler ticks.
- Offline refusal/success/revoked-rules tests and existing workflows pass. `uv run ruff check .` passed; `uv run pytest`: 104 passed, 2 skipped, 1 warning. Live ruleset verification deferred.

## 2026-09-24: Phase 4 Plan Intake

- Added strict numbered-file/frontmatter/section parsing, committed-blob discovery, duplicate rejection and idempotent create/update/cancel/ignore reconciliation with notifications.
- Added deduplicated plan-draft requests, deterministic operator plan rendering, and protected plan-only publication. The full draft worker execution is connected in the builder phase; live empty-repository plan PR acceptance is deferred.
- Existing artifact generation remains in the legacy controller until the runtime replacement; it is not used by the new intake functions.
- `uv run ruff check .` passed; parser/reconciliation and full suite: 112 passed, 2 skipped, 1 warning.

## 2026-09-24: Phase 5 Scheduler

- Centralized transitions in scheduler.py, with transaction-scoped events, strict predecessor ordering, stalled-queue notifications, global claim serialization, pause controls and retry branch suffixes.
- Retained historical v1 transitions only for existing rows; new tasks use the v2 paths. Legacy domain imports delegate to the single transition function.
- Randomized predecessor-state tests, complete transition-table coverage and closed-PR retry tests passed. `uv run ruff check .` passed; `uv run pytest`: 133 passed, 2 skipped, 1 warning.
- Runtime dispatch switches to this scheduler in Phase 6 alongside the builder context/result migration.

## 2026-09-24: Phase 6 Builder Runtime

- Added internal attempt networks, managed-object guards, setup-only egress detach/probe, configurable runtime/limits, structured plan context and strict criterion correspondence.
- Builder entrypoint writes a fresh tmpfs Hermes configuration, disables compression and memory, constrains tools and emits effective tool/memory flags before executing. Output is validated independently by the controller-side runner.
- Verified constructor, tool registry and memory flags directly from installed Hermes image sha256:dae6bb094499d8b8e96105dc330b2cb51a615436ff8f7d54e68a82c12c4bcef6, labeled commit 38c9611791d3c8eccee5bb3fdad8075ec1d58565. Remote raw-source access returned 429; local pinned source was available.
- `uv run ruff check .` passed; `uv run pytest`: 136 passed, 2 skipped, 1 warning. Live model execution and full egress acceptance remain pending. Controller orchestration is completed with gates/publication in Phases 8-9 to avoid exposing an ungated runtime.

## 2026-09-24: Phase 7 Sidecars

- Added allowlisted per-attempt service creation with random passwords, redaction registration, health deadlines, durable pre-start rows and managed cleanup. Credentials only enter container environments; state records contain IDs and service names.
- Tests exercise successful start/health/cleanup, password absence from state, and unknown-service rejection. `uv run ruff check .` passed; `uv run pytest`: 138 passed, 2 skipped, 1 warning.
- Real database-image healthchecks and connectivity remain pending digest provisioning and Docker acceptance.

## 2026-09-24: Phase 8 Gates and Reports

- Added implementation plans/report immutability gates, Git driver/submodule rejection, sensitive-change detection and escaped/redacted report rendering from measured checks and independent review.
- Added fresh read-only checks-container runner without a model token, sharing only the attempt network/services and dependency environment. CI workflow changes now force draft review instead of being unconditionally prohibited, as requested.
- Gate and report tests verify forged success claims cannot override failing checks. `uv run ruff check .` passed; `uv run pytest`: 144 passed, 2 skipped, 1 warning. Live checks-container behavior remains pending Docker acceptance.

## 2026-09-24: Snapshot repair before Phase 9

- The committed snapshot did not match the Phase 8 entry above: `uv run pytest` gave 136 passed, 8 failed, 2 skipped, and `ruff` reported 2 errors in controller.py. The failures were v1 tests (goal planner, `save_plan`, `awaiting_review`, `plan/<id>` artifacts) exercising interfaces the v2 controller no longer has.
- Rewrote the controller for the v2 flow and replaced those tests with v2 tests that drive a real local git remote (tests/helpers.py). No invariant was weakened to make a test pass.

## 2026-09-24: Phase 9 Publication

- Branches `agent/NN-slug`, `-r<k>` after retry, `-rb<k>` after rebase, `agent/plans-<id>`. Commits `plan NN: <title>` then `report NN: <title>`; repairs add `plan NN: address review (repair k)` and `report NN: <title> (repair k)`. Never force-pushed.
- Before every push: forced ruleset/permission verification (fails closed), HEAD equals the validated commit, clean tree, and `assert_agent_branch` (regex, refspec `HEAD:refs/heads/agent/*`) in code. PR title/body per Appendix D, draft state kept in sync (GraphQL draft/ready), `autocoder` label, merged branches deleted.
- Tests: tests/test_workflow.py (real git: titles, bodies, commits, report content, pushes only to agent/*, no merge calls); T-I1/T-I2 invariants.

## 2026-09-24: Phase 10 Feedback and Repair

- feedback.py: per-source cursors, dedupe by `(task, source, github_id)`, operator-only eligibility, bot items skipped, others stored ignored with one info notification, CHANGES_REQUESTED/COMMENTED-with-body repair, APPROVED ignored, failed/timed-out check runs stored as redacted 4 KB excerpts, debounce on the latest operator activity, fenced rendering, consumption on publication.
- Conflicts: clean rebase in a gatekeeper clone (hooks disabled) publishes `-rb<k>` as a new PR and closes the old one with a link. Deviation: on a real conflict the repair re-implements the plan on a new `-rb<k>` branch from the new `main` (with the conflict summary and previous summary as untrusted context), because builders cannot rebase (read-only `.git`) and the ruleset blocks force-push. The superseded PR is closed when the new PR opens.
- Plan PRs (kind `plan_draft`) do not collect feedback; the operator edits or closes them. Closing a plan PR notifies but does not pause the repository.
- Tests: non-operator injection never reaches context.json, debounce groups comments, CI excerpts fenced and capped, merge → next plan, close → pause + retry `-r1`, conflict and clean-rebase paths, repair limit.

## 2026-09-24: Phase 11 Pools

- Capabilities carry `kind`/`pool`; `register_concierge_token` stores only the hash and revokes the previous one. Per-pool daily request limits plus builder per-attempt limits, both under the global USD ceilings. Pause blocks only builder capabilities.
- Proxy accepts `response_format` (text/json_object/json_schema) and text-part content lists; mem0 2.2's OpenAI LLM sends `temperature`, `top_p`, `max_tokens`, optional `response_format`/`tools`. mem0 adds OpenRouter-only fields only when `OPENROUTER_API_KEY` is set in its environment, which the concierge never has.
- Tests: concierge works while paused, independent pool limits, rotation, one test per request shape.

## 2026-09-24: Phase 12 MCP Server

- Pinned `mcp` 2.2.0 (`mcp.server.mcpserver.MCPServer`; FastMCP was renamed in 2.x). Streamable HTTP at `/mcp`, stateless JSON responses. DNS-rebinding host checks are disabled because the listener is reachable only on the internal network and every request needs the bearer token (constant-time compare); 60 calls/minute; `/healthz` unauthenticated.
- Exactly the 15 Appendix G tools; Pydantic-validated arguments; audit `events` rows (`mcp_call`); redacted output capped at 16 KB with untrusted text truncated first; repository/PR/CI/builder text wrapped as `untrusted_text`. `Operations` is shared with the CLI.
- Runs as a thread inside `autocoder run` (also `autocoder mcp` standalone).

## 2026-09-24: Phase 13 Concierge

- Verified against hermes-agent 0.19.0 from PyPI (the pinned commit's source was not downloadable here: codeload returned 403): config keys `model.{default,provider,base_url,api_key}`, `platform_toolsets`, `mcp_servers.<name>.{url,headers}`, `memory.{memory_enabled,user_profile_enabled,provider}`, `approvals.mode` (the builder config used `approval`, now fixed to `approvals`), `auxiliary.<task>`, `kanban.dispatch_in_gateway`. The mem0 plugin reads `$HERMES_HOME/mem0.json` with `mode: oss` and passes `oss.{llm,embedder,vector_store}` to `Memory.from_config`.
- Found with the real Hermes resolver: Hermes always resolves a `kanban` toolset. Its tools register only with `HERMES_KANBAN_TASK` set or `kanban` in the profile toolsets, but its gateway dispatcher can spawn tool-enabled workers. The template disables the dispatcher and the self-check accepts `kanban` only when gated off.
- Self-check (fail closed): allowlisted toolsets only (memory, session_search, clarify, todo, mcp-*), no forbidden tool names, only the `autocoder` MCP server, `memory.provider: mem0`. Checked against the real 0.19 resolver: the rendered config passes; adding `terminal` fails.
- mem0 2.2.0: `fastembed` embedder (`BAAI/bge-small-en-v1.5`, 384 dims, baked into the image, offline at runtime), `pgvector` store (psycopg3 pool), OpenAI-compatible LLM via the proxy with the concierge token, `user_id: operator`.
- Deviation: secrets are rendered into the concierge home volume at each start (mode 0600) because Hermes and mem0 read them from config files. Documented in DEPLOYMENT.md.
- Pending live: concierge image build, E2E-6.

## 2026-09-24: Phase 14 Hardening

- docker-socket-proxy v0.4.2 (digest-pinned) is the only socket holder (CONTAINERS, NETWORKS, IMAGES, DISTRIBUTION, EXEC, POST, start/stop; no volumes, swarm, services, secrets, build or system). Controller uses `DOCKER_HOST=tcp://docker-proxy:2375`.
- Networks per section 4.2; pgvector/pgvector:pg16 by digest; Postgres init script creates `autocoder` and `mem0` with separate roles and `vector`; sidecar allowlist digests pinned in config.example.yaml. Health checks, `restart: unless-stopped`, memory limits, `no-new-privileges`. Deviation: the proxy uses the `autocoder` role (the plan allows this; a narrower role was not needed).
- Labeled-object guards apply to remove/exec/network operations in worker.py, sidecars.py and networks.py.
- `docker compose config` validates. Deploy acceptance is pending (no Docker daemon in the build environment).

## 2026-09-24: Phase 15 Recovery, Logs, CLI

- Reconcile revokes tokens, removes labeled containers/networks and sidecar rows of interrupted attempts, requeues or blocks, re-publishes `publication_pending`, and sweeps labeled objects of finished or unknown attempts. Chaos tests cover restarts during preparing/running/validating and publishing.
- JSON logs (`AUTOCODER_LOG_FORMAT=json`) with repo/task_id/attempt_id/plan fields, redaction filter on every handler.
- CLI per Phase 15.3; `scripts/hc` host wrapper for chat, memory and the host half of doctor (concierge self-check, mem0 `vector`, socket mounts).

## 2026-09-24: Phase 16 Tests

- tests/invariants/test_invariants.py covers T-I1..T-I14 (plus test_redaction.py). They run in CI with the rest of the suite.
- Docker-gated tests (`-m docker`, `RUN_DOCKER_TESTS=1`): Postgres migrations/concurrent budgets, real Hermes adapter, builder egress isolation, Postgres sidecar lifecycle, unlabeled-object guard. Skipped here: no Docker daemon.
- scripts/e2e/e2e.py implements E2E-1..11 (E2E-6 and E2E-9 have manual steps); docs/E2E.md documents them.
- Final checks in the build environment: `uv run ruff check .` passed; `uv run pytest`: 224 passed, 5 skipped (Docker-gated); invariants: 60 passed.

## 2026-09-24: Phase 17 Documentation

- Added OPERATOR_GUIDE.md and E2E.md; rewrote DEPLOYMENT.md, README.md, ARCHITECTURE.md, PROJECT_DETAILS_AND_DATA_FLOW.md and HERMES_WORKFLOW_STEP_BY_STEP.md for v2; marked plan/ as historical v1 notes. backup.sh now dumps both databases and the concierge home.

## Outstanding live acceptance (not claimed)

These need infrastructure unavailable in the build environment and remain open in the Definition of done:
Docker-gated tests; building the worker and concierge images; `hc doctor` fully green locally and on the VPS; E2E-1..11 on a real repository with the ruleset (record PR links here); a secret scan over `git log -p` and the last E2E run's logs.

## 2026-09-24: Telegram chat for the concierge (Phase 13.4, 13.7)

- Verified against hermes-agent 0.19.0: the Telegram adapter needs the `messaging` extra (python-telegram-bot), now installed in the concierge image. The gateway enables Telegram from `TELEGRAM_BOT_TOKEN`, authorizes default-deny via `TELEGRAM_ALLOWED_USERS`, and by default answers unknown users with a pairing code, so the template sets `unauthorized_dm_behavior: ignore`.
- The self-check now also fails on open access: missing or non-numeric `TELEGRAM_ALLOWED_USERS`, or any of `TELEGRAM_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`, `TELEGRAM_GROUP_ALLOWED_CHATS`, `TELEGRAM_GROUP_ALLOWED_USERS`, `TELEGRAM_ALLOW_BOTS`.
- compose.telegram.yaml adds only a `concierge-egress` network and the `telegram_bot_token` secret (enable with `COMPOSE_FILE` in .env).
- Proactive notifications: an operator-defined Hermes cron job (`no_agent`, script-only, re-seeded at each start) runs `autocoder_notify.py` every 2 minutes, reads unacknowledged notifications through the MCP server, and prints only new ones for delivery to `TELEGRAM_HOME_CHANNEL` (the operator's id). It makes no model calls, and the model still has no cronjob tool. Seeding was verified against the real `cron.jobs` module.
- Tests: tests/test_telegram.py (access rules, override contents, image extra, model-free idempotent job, notifier against the real MCP app). `uv run pytest`: 239 passed, 5 skipped. Live Telegram acceptance is pending a bot token.
