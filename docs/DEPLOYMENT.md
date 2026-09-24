# Deployment Runbook

## v2 Implementation Status

Phase 0 is complete. Phase 1 implements configuration, identity and redaction; later v2 phases are not yet accepted. The existing runtime still has the gaps recorded in AUDIT.md. Do not treat a passing Phase 1 doctor as full v2 deployment acceptance. Existing live config, secret contents, databases and running containers were not modified by this migration.

## Configuration Migration (Phase 1)

The example now matches Appendix A of HERMES_AUTOCODER_V2_IMPLEMENTATION_PLAN.md. v1 owner/token mappings and flat model/budget settings are rejected, with no automatic rewriting of your live config.

- Set owners to a list of account names and set operator_login to your account.
- Create a separate bot account. Set github.bot_login to that account and github.commit_name / github.commit_email to its name and GitHub noreply address.
- Replace the old github_personal credential with a new bot-owned fine-grained PAT in secrets/github_bot. Do not rename an operator token and assume it has become a bot token.
- Scope the bot PAT to the intended repositories: Contents and Pull requests read/write; Metadata, Checks, Commit statuses and Actions read; no Administration permission. Invite the bot with Write access. GitHub owner/organization policy may constrain fine-grained PAT access; verify the actual access before onboarding.
- Move model settings under model and budgets under budgets. Runtime limits are under builder and scheduler; profile setup/checks are shell command strings. Old operational fields such as database_url remain supported until the later phases migrate deployment.
- Replace builder.image and all services_allowlist image placeholders with actual sha256 image IDs or registry digests. Placeholders deliberately fail validation; they are not usable image pins.
- scheduler.sequential must remain true. Configuration validation alone does not implement the Phase 5 state machine.
- Set data_dir to the same absolute host path as AUTOCODER_DATA_DIR in Compose while the current host bind mounts are in use.

autocoder and hc invoke the same CLI. init still migrates only the existing database schema and initializes it paused; Phase 2 has not yet changed that schema.

## Secrets

Only placeholders are supplied in deploy/secrets.example. Provision actual files under the ignored secrets/ directory when ready, with directory mode 0700 and file mode 0600. Never put credential values in YAML or commands that will be recorded.

Current Compose mounts:

| File | Container |
| --- | --- |
| secrets/github_bot | controller only |
| secrets/model_provider | model-proxy only |
| secrets/pgpass | controller and model-proxy |
| secrets/postgres_password | database only |

The provider key is now file-backed, not read from OPENROUTER_API_KEY in .env. The controller no longer mounts the entire secrets directory. Keep provider_key_file at /run/secrets/model_provider; only the proxy reads it. doctor verifies GitHub identity but does not read the provider key from the controller.

pgpass contains database:5432:autocoder:autocoder:<DATABASE_PASSWORD>; postgres_password contains the same password. Escape colons and backslashes according to pgpass syntax. Separate autocoder/mem0 roles and their v2 secret names arrive in Phase 14; placeholders for those names are listed for preparation only.

MCP and concierge tokens are also placeholders until their capability lifecycle and secret rotation are implemented. Do not start the later services based solely on this file.

## GitHub App Alternative

Bot PAT is the default. App mode additionally needs:

```yaml
github:
  mode: app
  bot_login: your-app-slug[bot]
  commit_name: Hermes Autocoder
  commit_email: "<bot-id>+your-app-slug[bot]@users.noreply.github.com"
  app_id: 12345
  installation_id: 67890
  private_key_secret: /run/secrets/github_app_private_key
```

These IDs are examples, not credentials. Mount the private-key secret only into the controller when using this mode. The default Compose file is PAT-only; add the private-key secret mount in a local Compose override for App mode.

The client signs RS256 JWTs and refreshes installation credentials before expiry. App identity is checked with GET /app using a JWT; installation tokens do not represent a user for GET /user. Bot-PAT identity uses GET /user and must equal github.bot_login. This documented API distinction is recorded as a plan deviation in IMPLEMENTATION_LOG.md. Future onboarding must still enforce I2; App mode does not bypass permission/ruleset checks.

## Verification

Run uv run ruff check . and uv run pytest for local checks. Integration tests are opt-in; mock HTTP tests do not establish live GitHub acceptance.

After provisioning the bot credential, migrated configuration, initialized database, worker image and network, run:

```sh
uv run hc --config config.yaml doctor
```

The Phase 1 doctor validates configuration, verifies the bot identity, and checks existing database/Docker prerequisites. It explicitly identifies itself as Phase 1 verification. Full v2 doctor checks are specified in Phase 15.

Do not reuse the retired provision_local_test.py or seed_local_test.py helpers. The operator creates and initializes repositories. Onboarding, numbered plan intake and ruleset-gated publication will be added in subsequent phases.

For paid models, also set a spending cap at the provider. Application accounting does not replace a provider-enforced spending cap.

## Existing Operational Procedures

The following backup and rollback procedures describe the existing v1 services. They will be updated for concierge volumes and the final v2 schema during Phase 17.

## Backups and Restore

Run `scripts/backup.sh /absolute/backup/directory` from the checkout. It briefly
stops the controller and proxy, captures PostgreSQL and persistent artifacts,
and restarts only services that were running. Schedule it daily with a systemd
timer, then copy the backup to encrypted operator-managed offsite storage.
Back up secret files separately using your secret-management system.

The supplied `deploy/systemd/hermes-autocoder-backup.service` and `.timer` run at
02:00 UTC and prune finished logs afterward. They assume the checkout is installed
at `/opt/hermes-autocoder`; adjust that path and backup destination when deploying.
Install both units in `/etc/systemd/system`, run `systemctl daemon-reload`, and
enable `hermes-autocoder-backup.timer` with `systemctl enable --now`.

Restore into an empty deployment with the same images and schema version:

1. Stop controller and proxy, leaving PostgreSQL running.
2. Restore the database with `docker compose exec -T database pg_restore -U
   autocoder -d autocoder < DATABASE.dump` into the empty initialized database.
3. Extract the data archive into the configured absolute data directory and
   restore the original ownership. Restore secrets separately.
4. Start the controller and proxy, inspect `status`, and keep repositories paused
   until branch/PR reconciliation has completed. Interrupted work is preserved.

Exercise restore on a separate deployment before relying on backups. The backup
script does not erase old backups; set retention in the offsite backup system.
Run `prune` daily to rotate old finished-run text logs; it preserves reports,
workspaces and pending publication records.

## Upgrades and Rollback

Pause work and back up state. Build a new controller/worker image, run automated
tests and the fixture acceptance flow, stop services, and run `init` to migrate.
Start the pinned new images. If compatibility checks fail, stop services and
restore the previous image and matching database backup. Do not downgrade a
database by assuming the older image understands newer schemas.

## Pilot Acceptance

Use a repository you control with a seeded defect and an independent regression
check. Verify plan creation, implementation, baseline/final evidence, report, PR,
and cancellation. Restart during execution and publication. Merge a PR yourself
and verify a dependent task starts only afterward. Test revoked GitHub access,
provider failure and spending exhaustion. Then observe a 48-hour pilot before
enabling all owned repositories. Record real PR links, costs, restarts and failures
in the pilot report; do not replace missing evidence with fixture results.
