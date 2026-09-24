# Running Hermes Autocoder from Telegram

You message a Telegram bot; the concierge (a Hermes agent) answers and operates the system for you:
adds repositories, writes plans, starts, pauses and retries work, and reads reports. It also pushes
notifications to you ("PR #4 is ready for review", "plan 02 is blocked") without being asked.

## How a Telegram prompt becomes code

```text
You (Telegram) ──▶ concierge ──add_plan / request_plan_draft──▶ gatekeeper opens a plan PR
                                                                     │  you merge it (approval)
      Telegram ◀── "PR #5 is ready for review" ◀── builder implements plan ◀──┘
```

1. You write: *"In you/demo, add a plan: a /health endpoint that returns 200 with {"ok": true}."*
2. The concierge confirms the title and acceptance criteria with you, then calls `add_plan`.
3. The gatekeeper opens a plan PR adding `plans/NN-add-health-endpoint.md`. **Merging it is your approval**:
   plans are your instructions, so they only reach `main` through you (invariant I8).
4. A builder implements the plan in an isolated container and a PR opens. You get a Telegram message.
5. Review and merge on GitHub (the GitHub mobile app works). Comment on the PR to request changes.

For bigger goals say *"draft plans for a notes-sync CLI in you/demo"*: a builder drafts up to 8 plans in one
plan PR; merge it and they run in order.

The concierge itself cannot write code, run commands or merge. That is deliberate: a Telegram message
can only ever turn into a plan you approve.

## Setup (about 5 minutes)

Prerequisite: the stack from [DEPLOYMENT.md](DEPLOYMENT.md) is running and `scripts/hc doctor` passes.

1. **Create the bot.** In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username.
   Copy the token it gives you.
2. **Save the token as a secret** (not in `.env`, not in shell history):
   ```sh
   install -m 600 /dev/null secrets/telegram_bot_token && $EDITOR secrets/telegram_bot_token
   ```
3. **Find your numeric user id.** Message **@userinfobot** (or any "user id" bot); it replies with a number
   like `123456789`. Your `@username` will not work.
4. **Edit `.env`:**
   ```sh
   TELEGRAM_ALLOWED_USERS=123456789
   COMPOSE_FILE=compose.yaml:compose.telegram.yaml
   ```
   `COMPOSE_FILE` makes every `docker compose` and `scripts/hc` command include the Telegram override.
5. **Start it:**
   ```sh
   docker compose up -d --build concierge
   docker compose logs concierge | tail     # expect: concierge ready: Telegram gateway for user(s) 123456789
   ```
6. **Say hello.** Open your bot in Telegram and send `status`. The first reply lists any items that need
   your attention.

## What you can send

| Message | Result |
| --- | --- |
| `status` / "what's happening with demo?" | Queue state, current plan, open PR link, budget use |
| "add https://github.com/you/demo" | Onboards the repo, or explains exactly what to fix (ruleset, bot access) |
| "add a plan to …" | Confirms criteria, then opens a plan PR |
| "draft plans for …" | A builder drafts several plans in one plan PR |
| "show the report for plan 2" | The rendered report (summary, criteria, checks) |
| "pause" / "resume" / "pause demo" | Stops or restarts builders; chat keeps working |
| "skip plan 3" / "retry plan 3" | Unblocks the queue |
| "any notifications?" | Everything unacknowledged |
| "remember I prefer short answers" | Stored in the concierge's memory (mem0) |

## Notifications

A small script checks the gatekeeper every 2 minutes and messages you about new notifications (PR opened,
merged, closed, blocked, ruleset problems). It uses no model calls, so it costs nothing. Change the interval
with `NOTIFY_INTERVAL=5m` in `.env`. Notifications stay unacknowledged, so the
concierge still mentions them the next time you chat; say "acknowledge them" to clear them.

## Security model

- **Only you.** Access is limited to the numeric ids in `TELEGRAM_ALLOWED_USERS`. Unknown users get no
  reply and no pairing code (`unauthorized_dm_behavior: ignore`). The concierge refuses to start if
  `TELEGRAM_ALLOWED_USERS` is missing or not numeric, or if any allow-all, group or bot-access variable is
  set (`TELEGRAM_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`, `TELEGRAM_GROUP_ALLOWED_CHATS`,
  `TELEGRAM_GROUP_ALLOWED_USERS`, `TELEGRAM_ALLOW_BOTS`).
- **Minimal egress.** The override adds one network, `concierge-egress`, so the concierge can reach
  `api.telegram.org`. It still holds no GitHub token, no provider key and no Docker access. It reaches the
  model only through the proxy with its own budget pool, and the system only through the gatekeeper's
  15 audited tools.
- **No code tools.** The startup self-check still fails if terminal, file, code, browser or web tools
  appear. No Hermes API server or dashboard is published.
- **If your phone or Telegram account is compromised**, the attacker can do what you can do from chat:
  add plans (which still need a merge on GitHub), pause or skip work. They cannot merge or touch secrets.
  Revoke the bot token in @BotFather (`/revoke`) and restart the concierge.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Container exits at start with `TELEGRAM_ALLOWED_USERS must list your numeric Telegram user id` | Use the number from @userinfobot, not `@username` |
| Bot never answers | `docker compose logs concierge`; check the token file, and that `COMPOSE_FILE` includes `compose.telegram.yaml` |
| Answers but tool calls fail | `scripts/hc doctor` (MCP server health); check the controller is running |
| "Concierge pool daily request limit reached" | Raise `budgets.pools.concierge.requests_per_day` in `config.yaml`, restart the model proxy |
| No push notifications | `docker compose exec concierge hermes cron list` should show `autocoder-notifications` |
