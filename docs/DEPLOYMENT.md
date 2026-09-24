# Deployment Runbook (v2)

Local first, then a VPS. Day-to-day use is in [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md).

## 1. GitHub setup (once)

1. **Bot account.** Create a second GitHub account (for example `yourname-autocoder`) and enable 2FA.
2. **Collaborator.** For each managed repository: *Settings → Collaborators* → add the bot with the
   **Write** role (not Maintain or Admin; the gatekeeper refuses anything but exactly `write`).
3. **Fine-grained token.** Bot account: *Settings → Developer settings → Fine-grained tokens*. Resource
   owner: the account owning the repositories (approve fine-grained tokens for collaborators if prompted).
   Select only the managed repositories. Permissions:
   - Contents: read and write; Pull requests: read and write; Metadata: read;
   - Checks, Commit statuses, Actions: read only; Administration: **none**.

   Save it as `secrets/github_bot`. It replaces the v1 `github_personal` operator token: do not rename an
   operator token and pretend it is the bot's.
4. **Ruleset** (per repository): *Settings → Rules → Rulesets → New branch ruleset*.
   - Enforcement: Active. Target: default branch. Bypass list: only you, or empty. **Never add the bot.**
   - Rules: Restrict deletions; Block force pushes; Require a pull request before merging (required
     approvals ≥ 1, dismiss stale approvals on push, require approval of the most recent reviewable push).
   Classic branch protection is **not** accepted: the bot cannot read it without admin rights.
5. **Initial commit.** The default branch must exist and should contain `plans/` (plan files or a
   `.gitkeep`) and `report/.gitkeep`.

Optional App mode (`github.mode: app`) uses installation tokens instead of a PAT; add `app_id`,
`installation_id` and `private_key_secret`, and mount the key into the controller only (local override).

## 2. Configuration

```sh
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml`: `owners`, `operator_login`, `github.bot_login`, `github.commit_email` (the bot's
`<id>+<login>@users.noreply.github.com`), `repository_profiles`, and `builder.image` (see section 4).
`scheduler.sequential` must stay `true`. Sidecar images in `services_allowlist` are pinned by digest;
keep them pinned when updating. `data_dir` must equal `AUTOCODER_DATA_DIR` in `.env`, because builder
bind mounts use host paths. Set `AUTOCODER_MODEL` in `.env` to the same value as `model.model`.

## 3. Secrets

Every secret is its own file under the git-ignored `secrets/` directory (mode 0700, files 0600):

| File | Mounted into | Source |
| --- | --- | --- |
| `github_bot` | controller | you (bot PAT) |
| `model_provider` | model-proxy | you (OpenRouter key) |
| `postgres_password` | database | generated |
| `db_autocoder_password` | database, controller, model-proxy | generated |
| `db_mem0_password` | database, concierge | generated |
| `mcp_concierge_token` | controller, concierge | generated |
| `concierge_model_token` | concierge (the database stores only its hash) | generated |

```sh
mkdir -p secrets && chmod 700 secrets
# write github_bot and model_provider yourself, without shell history:
install -m 600 /dev/null secrets/github_bot && $EDITOR secrets/github_bot
install -m 600 /dev/null secrets/model_provider && $EDITOR secrets/model_provider
uv run hc secrets init --dir secrets      # or: python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
```

Rotate generated tokens with `hc secrets rotate mcp_concierge_token|concierge_model_token --dir secrets`,
then `docker compose up -d --force-recreate controller concierge`. Rotate the GitHub, provider and database
credentials at their source. If you use a paid model, **also set a spending cap at the provider**: the
application's daily/monthly ceilings and per-pool request limits are not a substitute.

## 4. Build and pin images

```sh
docker compose build                              # controller/model-proxy and concierge
docker build -f docker/worker.Dockerfile -t hermes-autocoder-worker:local .
docker image inspect hermes-autocoder-worker:local --format '{{.Id}}'   # -> builder.image in config.yaml
```

The builder and the concierge are built from the same pinned Hermes commit (`HERMES_COMMIT`). The
concierge bakes its embedding model (`BAAI/bge-small-en-v1.5`, 384 dimensions) into the image because it has
no network egress at runtime. Postgres (pgvector) and the Docker socket proxy are pinned by digest in
`compose.yaml`.

## 5. Start

```sh
docker compose up -d database docker-proxy
docker compose run --rm -v "$PWD/secrets:/secrets:ro" controller init --secrets-dir /secrets
docker compose up -d
scripts/hc budget set 5 20
scripts/hc doctor
```

`init` migrates the database, starts the system **paused**, and registers the concierge token's hash.
The Postgres init script (`deploy/postgres-init/01-databases.sh`) runs once on an empty volume: it creates
the `autocoder` and `mem0` databases with separate roles (`mem0` cannot reach `autocoder`) and enables
`vector` in `mem0`.

`scripts/hc doctor` checks: configuration, secrets, bot identity (`GET /user` equals `github.bot_login`),
every enabled repository's ruleset, the socket proxy, pinned images (builder and sidecar digests), the
configured runtime (gVisor if `runsc`), model-proxy and MCP health, the builder egress probe, the
concierge tool self-check, the mem0 database with `vector`, and that only `docker-proxy` mounts the socket.

## 6. First repository

```sh
scripts/hc repos add https://github.com/you/demo     # or paste the link in chat
scripts/hc resume
scripts/hc chat                                      # talk to the concierge
```

A refusal names the fix (permission, ruleset, archived, missing default branch). If `plans/` is empty the
first thing you get is a plan PR.

## 7. Network topology

| Network | Internal | Members |
| --- | --- | --- |
| `control` | yes | database, docker-proxy, controller, model-proxy, concierge |
| `hermes-workers` | yes | model-proxy, the active builder |
| `att-<attempt>` | yes (created per attempt) | one builder and its sidecars |
| `hermes-setup-egress` | no | a builder, only while setup commands run |
| `github-egress` | no | controller |
| `provider-egress` | no | model-proxy |

No port is published. The MCP server listens on `controller:8765` inside `control` only. The concierge has
no egress network.

**Telegram.** To chat from Telegram, follow [TELEGRAM.md](TELEGRAM.md): a BotFather token in
`secrets/telegram_bot_token`, your numeric id in `TELEGRAM_ALLOWED_USERS`, and `COMPOSE_FILE=compose.yaml:compose.telegram.yaml`
in `.env`. The override adds only a `concierge-egress` network and the token secret. Do not publish or enable
the Hermes API server or dashboard.

## 8. VPS notes

- Firewall: allow SSH only; no application port needs to be public (Telegram uses outbound polling).
  Chat via SSH + `scripts/hc chat`, or Telegram as above.
- Optional gVisor: install `runsc`, register it in `/etc/docker/daemon.json` (`"runtimes": {"runsc":
  {"path": "/usr/local/bin/runsc"}}`), restart Docker, set `builder.runtime: runsc`, and confirm with
  `scripts/hc doctor`. Builders, check containers and sidecars then run under gVisor.
- Log rotation: every service uses json-file logs capped at 3 × 10 MB; `hc prune` removes finished run logs
  older than `log_retention_days` (the backup timer runs it daily).
- Backups: `scripts/backup.sh /abs/backup/dir` (daily via `deploy/systemd/*`). Back up the `database`
  volume (both databases), the data directory, the `concierge_home` volume, and `secrets/` separately.

## 9. Backups, restore and upgrades

`scripts/backup.sh` briefly stops the controller, proxy and concierge, dumps both databases, archives the
data directory and the concierge home, and restarts what was running. Restore into an empty deployment
with the same images: `pg_restore` each dump, extract the archives, restore secrets, start with the system
paused, check `scripts/hc status`, then resume.

Upgrade: pause, back up, rebuild, run `init` (migrations are reversible and preserve rows), start, run
`doctor`, resume. Roll back by restoring the previous images **and** the matching backup.

## 10. Migrating from v1

- `github_personal` → `github_bot` (a bot-owned token; see section 1).
- The controller no longer mounts the Docker socket or the whole secrets directory; `docker-proxy` does.
- v1 `goal add` becomes `plans draft <repo> <goal>` (a plan PR you approve). v1 `plan/<plan-id>/…` and
  `report/<task>/<attempt>.md` outputs no longer exist; reports live at `report/NN-slug.md` in each PR.
- The single `pgpass`/`postgres` role is replaced by separate `autocoder` and `mem0` roles. Dump the v1
  database, start a fresh v2 volume, and restore the dump into the `autocoder` database, then run `init`.
