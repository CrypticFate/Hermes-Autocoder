#!/bin/sh
# Render the concierge configuration from Docker secrets, prove the tool schema is safe (I6), then start.
set -eu
umask 077
python=/opt/hermes/.venv/bin/python
"$python" /opt/concierge/concierge_setup.py render
"$python" /opt/concierge/concierge_setup.py selfcheck
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
if [ -s /run/secrets/telegram_bot_token ] && [ -n "${TELEGRAM_ALLOWED_USERS:-}" ]; then
    TELEGRAM_BOT_TOKEN=$(cat /run/secrets/telegram_bot_token)
    export TELEGRAM_BOT_TOKEN
    exec hermes gateway run
fi
echo "concierge ready: chat with 'hc chat' (docker compose exec -it concierge hermes)"
exec sleep infinity
