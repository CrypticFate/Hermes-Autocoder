"""Concierge bootstrap: render Hermes config from secrets and verify the effective tool schema (I6).

Runs inside the concierge image with the pinned Hermes interpreter. Keys were verified against the
pinned Hermes release (config: model/platform_toolsets/mcp_servers/memory/approvals/auxiliary;
mem0 plugin: $HERMES_HOME/mem0.json with mode=oss) and mem0ai 2.2 (fastembed embedder, pgvector store).
"""
import json
import os
import shutil
import sys
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
ALLOWED_TOOLSETS = ["memory", "session_search", "clarify", "todo"]
FORBIDDEN_TOOLSETS = {"terminal", "file", "code_execution", "browser", "web", "search", "image_gen", "vision",
                      "video", "video_gen", "delegation", "computer_use", "cronjob", "skills", "kanban",
                      "homeassistant", "spotify", "discord", "discord_admin", "tts", "x_search", "project",
                      "feishu_doc", "feishu_drive", "yuanbao"}
FORBIDDEN_TOOLS = {"terminal", "process", "read_file", "write_file", "patch", "search_files", "execute_code",
                   "delegate_task", "web_search", "web_extract", "image_generate", "computer_use",
                   "skill_manage", "cronjob", "vision_analyze", "send_message"}
AUX_TASKS = ["vision", "web_extract", "compression", "skills_hub", "approval", "mcp", "title_generation",
             "memory_query_rewrite", "tts_audio_tags", "triage_specifier", "kanban_decomposer",
             "profile_describer", "goal_judge", "curator", "monitor", "background_review", "moa_reference",
             "moa_aggregator"]
EMBEDDER = {"model": "BAAI/bge-small-en-v1.5", "embedding_dims": 384}


def read_secret(directory, name):
    value = (Path(directory) / name).read_text().strip()
    if not value or "\n" in value or value.startswith(("<", "PLACEHOLDER")):
        raise SystemExit(f"Missing or placeholder secret: {name}")
    return value


def q(value):
    """JSON is valid YAML: quoting prevents secrets or settings from altering the document structure."""
    return json.dumps(value)


def render(env=os.environ, secrets_dir="/run/secrets", home=None):
    home = Path(home or env.get("HERMES_HOME") or Path.home() / ".hermes")
    home.mkdir(parents=True, exist_ok=True)
    model = env.get("CONCIERGE_MODEL") or sys.exit("CONCIERGE_MODEL is required")
    proxy = env.get("PROXY_URL", "http://model-proxy:8080/v1")
    token = read_secret(secrets_dir, "concierge_model_token")
    mcp_token = read_secret(secrets_dir, "mcp_concierge_token")
    builtin = env.get("CONCIERGE_BUILTIN_MEMORY", "true").lower() == "true"
    auxiliary = {task: {"provider": "custom", "model": model, "base_url": proxy, "api_key": token}
                 for task in AUX_TASKS}
    template = Template((HERE / "config.yaml.tmpl").read_text())
    config = template.substitute(
        MODEL=q(model), PROXY_URL=q(proxy), CONCIERGE_MODEL_TOKEN=q(token), TOOLSETS=q(ALLOWED_TOOLSETS),
        MCP_URL=q(env.get("MCP_URL", "http://controller:8765/mcp")), MCP_AUTHORIZATION=q("Bearer " + mcp_token),
        BUILTIN_MEMORY=q(builtin), AUXILIARY=q(auxiliary))
    mem0 = {"mode": "oss", "user_id": env.get("MEMORY_USER_ID", "operator"), "agent_id": "concierge",
            "oss": {"llm": {"provider": "openai", "config": {
                        "model": model, "api_key": token, "openai_base_url": proxy,
                        "max_tokens": int(env.get("MEM0_MAX_TOKENS", "2000")), "temperature": 0.1}},
                    "embedder": {"provider": "fastembed", "config": EMBEDDER},
                    "vector_store": {"provider": "pgvector", "config": {
                        "host": env.get("MEM0_DB_HOST", "database"), "port": 5432, "dbname": "mem0",
                        "user": "mem0", "password": read_secret(secrets_dir, "db_mem0_password"),
                        "collection_name": "operator_memories",
                        "embedding_model_dims": EMBEDDER["embedding_dims"]}}}}
    for name, text in (("config.yaml", config), ("mem0.json", json.dumps(mem0, indent=2))):
        target = home / name
        target.write_text(text)
        target.chmod(0o600)
    shutil.copyfile(HERE / "SOUL.md", home / "SOUL.md")
    return home


def hermes_resolver(config):
    """Effective tool names using the pinned Hermes resolver (fails closed if unavailable)."""
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset
    toolsets = set()
    for platform in ("cli", "telegram"):
        toolsets |= set(_get_platform_tools(config, platform, include_default_mcp_servers=False))
    tools = set()
    for name in toolsets:
        tools |= set(resolve_toolset(name))
    return toolsets, tools


def kanban_gated_off(config, env=os.environ):
    """Hermes always resolves `kanban`, but its tools only register with HERMES_KANBAN_TASK set or
    `kanban` listed in the profile toolsets; its dispatcher must also be off."""
    return (not env.get("HERMES_KANBAN_TASK") and "kanban" not in (config.get("toolsets") or [])
            and (config.get("kanban") or {}).get("dispatch_in_gateway") is False)


# Any of these would let someone other than the operator steer the concierge.
OPEN_ACCESS_VARIABLES = ("TELEGRAM_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "TELEGRAM_GROUP_ALLOWED_CHATS",
                         "TELEGRAM_GROUP_ALLOWED_USERS", "TELEGRAM_ALLOW_BOTS")


def telegram_problems(config, env=os.environ):
    """Telegram is operator-only: explicit numeric user ids, no allow-all, no groups, no pairing."""
    problems = [f"{name} must not be set" for name in OPEN_ACCESS_VARIABLES if env.get(name, "").strip()
                and env.get(name, "").strip().lower() not in {"false", "0", "none", "no"}]
    if not env.get("TELEGRAM_BOT_TOKEN"):
        return problems
    users = [u.strip() for u in env.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()]
    if not users or not all(u.isdigit() for u in users):
        problems.append("TELEGRAM_ALLOWED_USERS must list your numeric Telegram user id")
    if config.get("unauthorized_dm_behavior") != "ignore":
        problems.append("unauthorized_dm_behavior must be ignore (no pairing for unknown users)")
    return problems


def selfcheck(config, resolver=hermes_resolver, env=os.environ):
    problems = telegram_problems(config, env)
    toolsets, tools = resolver(config)
    if "kanban" in toolsets and kanban_gated_off(config, env):
        toolsets = toolsets - {"kanban"}
        tools = {t for t in tools if not t.startswith("kanban_")}
    bad_sets = sorted(t for t in toolsets if t in FORBIDDEN_TOOLSETS or (
        t not in ALLOWED_TOOLSETS and not t.startswith("mcp-")))
    bad_tools = sorted(tools & FORBIDDEN_TOOLS)
    if bad_sets:
        problems.append("Forbidden toolsets enabled: " + ", ".join(bad_sets))
    if bad_tools:
        problems.append("Forbidden tools in effective schema: " + ", ".join(bad_tools))
    for key in ("toolsets",):
        extra = set(config.get(key) or []) - set(ALLOWED_TOOLSETS)
        if extra:
            problems.append(f"{key} lists non-allowlisted toolsets: " + ", ".join(sorted(extra)))
    if set(config.get("mcp_servers") or {}) != {"autocoder"}:
        problems.append("Only the autocoder MCP server may be configured")
    if (config.get("memory") or {}).get("provider") != "mem0":
        problems.append("memory.provider must be mem0")
    return problems


NOTIFY_JOB = "autocoder-notifications"


def seed_notifications(home, interval=None, jobs=None):
    """Install the operator-defined, model-free notification job (the model has no cronjob tool)."""
    scripts = Path(home) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "autocoder_notify.py", scripts / "autocoder_notify.py")
    if jobs is None:
        from cron import jobs
    schedule = f"every {interval or os.environ.get('NOTIFY_INTERVAL', '2m')}"
    for job in jobs.list_jobs(include_disabled=True):
        if job.get("name") == NOTIFY_JOB:
            jobs.remove_job(job["id"])  # Re-seeded at every start so the definition cannot drift.
    return jobs.create_job(prompt=None, schedule=schedule, name=NOTIFY_JOB, deliver="telegram",
                           script="autocoder_notify.py", no_agent=True)


def main(argv=sys.argv[1:]):
    import yaml
    command = argv[0] if argv else "selfcheck"
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if command == "render":
        render()
        print("concierge config rendered")
        return 0
    if command == "seed-notifications":
        seed_notifications(home)
        print("telegram notification job installed")
        return 0
    config = yaml.safe_load((home / "config.yaml").read_text())
    try:
        problems = selfcheck(config)
    except Exception as exc:  # Fail closed: an unverifiable schema is treated as unsafe.
        problems = [f"Could not resolve effective tools: {type(exc).__name__}: {exc}"]
    result = {"ok": not problems, "problems": problems}
    (home / "selfcheck.json").write_text(json.dumps(result))
    print(json.dumps(result))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
