"""Hermes cron script (no_agent): print new gatekeeper notifications for delivery to Telegram.

Runs every few minutes inside the concierge. It makes no model calls: it reads notifications through
the gatekeeper MCP server and prints the ones not yet delivered. Empty output means nothing is sent.
Notifications are not acknowledged here, so the concierge still mentions them in the next chat.
"""
import json
import os
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
STATE = HOME / "autocoder_notify_state.json"
URL = os.environ.get("MCP_URL", "http://controller:8765/mcp")
ICONS = {"action_required": "Action required", "error": "Error", "info": "Info"}


def call_tool(name, arguments):
    token = Path("/run/secrets/mcp_concierge_token").read_text().strip()
    request = urllib.request.Request(URL, method="POST", data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}}).encode(), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    result = payload["result"]
    if result.get("isError"):
        raise RuntimeError("get_notifications failed")
    return json.loads(result["content"][0]["text"])


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"delivered": []}
    delivered = set(state["delivered"])
    notes = call_tool("get_notifications", {"unacknowledged_only": True})["notifications"]
    fresh = [n for n in notes if n["id"] not in delivered]
    lines = []
    for note in fresh:
        message = note["message"]["untrusted_text"] if isinstance(note["message"], dict) else note["message"]
        lines.append(f"[{ICONS.get(note['level'], note['level'])}] {message[:600]}")
    if lines:
        print("Hermes Autocoder:\n" + "\n\n".join(lines[:20]))
        if len(lines) > 20:
            print(f"\n…and {len(lines) - 20} more. Ask me for notifications.")
    # Remember only ids that are still unacknowledged, so the state file stays small.
    STATE.write_text(json.dumps({"delivered": sorted({n["id"] for n in notes} & (delivered | {n["id"] for n in fresh}))}))


if __name__ == "__main__":
    main()
