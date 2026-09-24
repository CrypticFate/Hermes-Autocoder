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
