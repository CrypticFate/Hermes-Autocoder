"""Inspect or reset the concierge's mem0 memories. Runs inside the concierge container.

Usage: mem0_admin.py list | search <query> | delete <id> | reset --yes
Exposed on the host as `hc memory list|search|delete|reset`.
"""
import json
import os
import sys
from pathlib import Path


def memory():
    from mem0 import Memory
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    config = json.loads((home / "mem0.json").read_text())
    oss = config["oss"]
    oss["vector_store"]["config"].setdefault("embedding_model_dims", oss["embedder"]["config"]["embedding_dims"])
    return Memory.from_config({**oss, "version": "v1.1"}), config.get("user_id", "operator")


def rows(result):
    items = result.get("results", result) if isinstance(result, dict) else result
    return [{"id": m.get("id"), "memory": m.get("memory"), "updated_at": m.get("updated_at")} for m in items]


def main(argv):
    if not argv or argv[0] not in {"list", "search", "delete", "reset"}:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    command, args = argv[0], argv[1:]
    store, user_id = memory()
    if command == "list":
        print(json.dumps(rows(store.get_all(filters={"user_id": user_id})), indent=2))
    elif command == "search":
        if not args:
            print("search requires a query", file=sys.stderr)
            return 2
        print(json.dumps(rows(store.search(" ".join(args), filters={"user_id": user_id})), indent=2))
    elif command == "delete":
        if len(args) != 1:
            print("delete requires one memory id", file=sys.stderr)
            return 2
        store.delete(args[0])
        print(json.dumps({"deleted": args[0]}))
    else:
        if args != ["--yes"]:
            print("reset permanently deletes every concierge memory; pass --yes", file=sys.stderr)
            return 2
        store.delete_all(user_id=user_id)
        print(json.dumps({"reset": user_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
