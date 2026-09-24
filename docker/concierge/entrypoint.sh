#!/bin/sh
# Render the concierge configuration from Docker secrets, prove the tool schema is safe (I6), then start.
set -eu
umask 077
python=/opt/hermes/.venv/bin/python
"$python" /opt/concierge/concierge_setup.py render
telegram=false
if [ -s /run/secrets/telegram_bot_token ]; then
    TELEGRAM_BOT_TOKEN=$(cat /run/secrets/telegram_bot_token)
    # Notifications and scheduled messages go to the operator's own chat.
    operator_id=${TELEGRAM_ALLOWED_USERS:-}
    TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL:-${operator_id%%,*}}
    export TELEGRAM_BOT_TOKEN TELEGRAM_HOME_CHANNEL
    telegram=true
fi
# Fails (and the container stops) on forbidden tools or open Telegram access.
"$python" /opt/concierge/concierge_setup.py selfcheck
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
if [ "$telegram" = true ]; then
    "$python" /opt/concierge/concierge_setup.py seed-notifications
    echo "concierge ready: Telegram gateway for user(s) ${TELEGRAM_ALLOWED_USERS:-}"
    exec hermes gateway run
fi
echo "concierge ready: chat with 'scripts/hc chat' (docker compose exec -it concierge hermes)"
exec sleep infinity
