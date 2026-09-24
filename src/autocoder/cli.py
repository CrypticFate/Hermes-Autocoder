"""`autocoder` / `hc` CLI. Operator commands wrap the same Operations the MCP tools use."""
import json
import logging
import os
import secrets as secrets_module
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import typer
from sqlalchemy import select

from autocoder.config import load_settings
from autocoder.db import initialize, locked, micro, require_task, session_factory
from autocoder.domain import transition
from autocoder.github import for_owner
from autocoder.models import Attempt, Control, Repository
from autocoder.redaction import install_log_redaction, redact, register_secret

app = typer.Typer(no_args_is_help=True)
repos = typer.Typer(no_args_is_help=True, help="Managed repositories")
plans = typer.Typer(no_args_is_help=True, help="Plans in a repository's plans/ folder")
tasks = typer.Typer(no_args_is_help=True, help="Task detail")
reports = typer.Typer(no_args_is_help=True, help="Rendered reports")
budget = typer.Typer(no_args_is_help=True, help="Spending ceilings")
secrets_cli = typer.Typer(no_args_is_help=True, help="Generated secrets")
memory = typer.Typer(no_args_is_help=True, help="Concierge memories (mem0)")
for group, name in ((repos, "repos"), (plans, "plans"), (tasks, "task"), (reports, "report"), (budget, "budget"),
                    (secrets_cli, "secrets"), (memory, "memory")):
    app.add_typer(group, name=name)

GENERATED = ("postgres_password", "db_autocoder_password", "db_mem0_password", "mcp_concierge_token",
             "concierge_model_token")


class State:
    """Lazily loads configuration so host-side commands (chat, memory) work without it."""

    def __init__(self, path):
        self.path, self._settings, self._factory = path, None, None

    @property
    def settings(self):
        if self._settings is None:
            try:
                self._settings = load_settings(self.path)
            except (ValueError, OSError) as exc:
                typer.echo(redact(str(exc)), err=True)
                raise typer.Exit(1) from None
        return self._settings

    @property
    def factory(self):
        if self._factory is None:
            self._factory = session_factory(self.settings)
        return self._factory

    def ops(self):
        from autocoder.mcp_server import Operations
        return Operations(self.settings, self.factory, for_owner)

    def __iter__(self):  # Backwards-compatible `settings, factory = ctx.obj`.
        return iter((self.settings, self.factory))


def configure_logging():
    handler = logging.StreamHandler()
    if os.environ.get("AUTOCODER_LOG_FORMAT") == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers, root.level = [handler], logging.INFO
    install_log_redaction()


class JsonFormatter(logging.Formatter):
    FIELDS = ("repo", "task_id", "attempt_id", "plan")

    def format(self, record):
        message = record.getMessage()
        payload = {"at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                   "logger": record.name, "event": message.split(" ", 1)[0]}
        for token in message.split():
            key, sep, value = token.partition("=")
            if sep and key in self.FIELDS + ("reason", "pr"):
                payload[key] = value
        payload["message"] = message
        if record.exc_text:
            payload["exception"] = record.exc_text
        return redact(json.dumps(payload))


def emit(value):
    typer.echo(redact(json.dumps(value, indent=2, default=str)))


def fail(exc):
    typer.echo(redact(str(exc)), err=True)
    raise typer.Exit(1)


def run_ops(ctx, method, *args, **kwargs):
    try:
        emit(getattr(ctx.obj.ops(), method)(*args, **kwargs))
    except (ValueError, LookupError) as exc:
        fail(exc)


@app.callback()
def main(ctx: typer.Context, config: Path = typer.Option(Path("config.yaml"), "--config", "-c")):
    """Operate Hermes Autocoder."""
    install_log_redaction()
    ctx.obj = State(config)


# ------------------------------------------------------------------ lifecycle
@app.command()
def init(ctx: typer.Context, secrets_dir: Path = typer.Option(None, "--secrets-dir",
         help="Generate missing random secrets here and register the concierge token hash")):
    """Migrate the database, initialize it paused, and optionally provision generated secrets."""
    from alembic import command
    from alembic.config import Config
    settings, factory = ctx.obj
    configuration = Config("alembic.ini")
    configuration.attributes["database_url"] = factory.kw["bind"].url.render_as_string(hide_password=False)
    command.upgrade(configuration, "head")
    initialize(factory, settings)
    if secrets_dir:
        created = generate_secrets(secrets_dir)
        register_concierge(factory, secrets_dir)
        typer.echo("Generated secrets: " + (", ".join(created) or "none (all present)"))
    typer.echo("Database ready. New installations start paused.")


def generate_secrets(directory: Path, names=GENERATED):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    created = []
    for name in names:
        path = directory / name
        if path.exists() and path.read_text().strip() and not path.read_text().startswith(("<", "PLACEHOLDER")):
            continue
        write_secret(path, secrets_module.token_urlsafe(32))
        created.append(name)
    return created


def write_secret(path: Path, value: str):
    register_secret(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value + "\n")


def register_concierge(factory, directory: Path):
    from autocoder.budget import register_concierge_token
    path = directory / "concierge_model_token"
    if path.exists():
        register_concierge_token(factory, path.read_text().strip())


@secrets_cli.command("init")
def secrets_init(dir: Path = typer.Option(Path("secrets"), "--dir")):
    """Create any missing generated secrets (never overwrites existing ones)."""
    created = generate_secrets(dir)
    typer.echo("Generated: " + (", ".join(created) or "nothing; all present"))
    typer.echo("Provide github_bot and model_provider yourself, then run init --secrets-dir to register "
               "the concierge token.")


@secrets_cli.command("rotate")
def secrets_rotate(ctx: typer.Context, name: str, dir: Path = typer.Option(Path("secrets"), "--dir")):
    """Rotate mcp_concierge_token or concierge_model_token; restart controller and concierge afterwards."""
    if name not in {"mcp_concierge_token", "concierge_model_token"}:
        raise typer.BadParameter("Only mcp_concierge_token and concierge_model_token are rotated here; rotate "
                                 "provider, GitHub and database credentials at their source.")
    write_secret(dir / name, secrets_module.token_urlsafe(32))
    if name == "concierge_model_token":
        register_concierge(ctx.obj.factory, dir)
    typer.echo(f"Rotated {name}. Run: docker compose up -d --force-recreate controller concierge")


# ---------------------------------------------------------------- doctor
@app.command()
def doctor(ctx: typer.Context):
    """Verify deployment prerequisites without calling a paid model."""
    settings, factory = ctx.obj
    results = []

    def check(name, function):
        try:
            detail = function()
            results.append((name, True, detail or "ok"))
        except Exception as exc:
            results.append((name, False, redact(str(exc) or type(exc).__name__)[:300]))

    check("config", lambda: "valid")

    def secrets_present():
        from autocoder.config import secret
        for path in filter(None, [settings.github.private_key_secret if settings.github.mode == "app" else
                                  settings.github.token_secret, settings.mcp.token_secret,
                                  settings.database_password_file]):
            secret(Path(path), multiline=settings.github.mode == "app")
        return "present"
    check("secrets", secrets_present)
    check("github bot identity", lambda: "login " + for_owner(settings, settings.owners[0]).verify_identity())
    if settings.github.mode == "operator_pat":
        results.append(("github mode", True, "operator_pat: PRs are opened with your own token. GitHub will not "
                        "let you approve them; merge protection relies on the ruleset and this code."))

    def budgets():
        with factory() as session:
            control = session.get(Control, 1)
            if not control or not control.daily_micro or not control.monthly_micro:
                raise ValueError("Daily/monthly spending limits required (run init)")
        return "set"
    check("database and budgets", budgets)
    with factory() as session:
        managed = session.scalars(select(Repository).where(Repository.enabled.is_(True))).all()
    for repo in managed:
        def ruleset(repo=repo):
            from autocoder.onboarding import verify_repository
            result = verify_repository(settings, factory, repo.name, force=True)
            if not result.verified:
                raise ValueError("; ".join(result.problems))
            return "verified"
        check(f"ruleset {repo.name}", ruleset)
    client_holder = {}

    def docker_proxy():
        import docker
        client_holder["client"] = client = docker.from_env()
        client.ping()
        return os.environ.get("DOCKER_HOST", "local socket")
    check("docker (socket proxy)", docker_proxy)

    def images():
        client = client_holder["client"]
        client.images.get(settings.builder.image)
        missing = []
        for name, service in settings.services_allowlist.items():
            try:
                client.images.get(service.image)
            except Exception:
                try:
                    client.images.get_registry_data(service.image)
                except Exception:
                    missing.append(name)
        if missing:
            raise ValueError("Sidecar digests not present or pullable: " + ", ".join(missing))
        return "builder and sidecars pinned"
    check("pinned images", images)

    def runtime():
        info = client_holder["client"].info()
        if settings.builder.runtime not in (info.get("Runtimes") or {"runc": {}}):
            raise ValueError(f"Runtime {settings.builder.runtime} is not installed")
        return settings.builder.runtime
    check("builder runtime", runtime)

    def http(url):
        import httpx
        httpx.get(url, timeout=5).raise_for_status()
        return "healthy"
    check("model proxy", lambda: http(settings.proxy_url.rstrip("/").removesuffix("/v1") + "/health"))
    check("mcp server", lambda: http(f"http://127.0.0.1:{settings.mcp.listen.rpartition(':')[2]}/healthz"))

    def egress_probe():
        from autocoder.networks import isolation_script
        client = client_holder["client"]
        container = client.containers.run(settings.builder.image, ["python3", "-c", isolation_script()],
            detach=True, network=settings.worker_network, read_only=True, cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], runtime=settings.builder.runtime,
            labels={"autocoder.managed": "true", "autocoder.attempt": "doctor-probe"})
        try:
            code = container.wait(timeout=60)["StatusCode"]
        finally:
            container.remove(force=True)
        if code:
            raise ValueError("A builder on the workers network can reach the internet")
        return "no egress"
    check("builder egress isolation", egress_probe)
    for name, ok, detail in results:
        typer.echo(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    typer.echo("Concierge self-check and mem0 checks run on the host: scripts/hc doctor")
    if not all(ok for _, ok, _ in results):
        raise typer.Exit(1)


# ---------------------------------------------------------------- status & control
@app.command()
def status(ctx: typer.Context, repo: str = typer.Argument(None)):
    """Builder-pool state, per-repo queues, current tasks and budget use."""
    run_ops(ctx, "get_status", repo)


@app.command()
def pause(ctx: typer.Context, repo: str = typer.Argument(None)):
    """Pause the builder pool (or one repository). The concierge stays available."""
    run_ops(ctx, "pause", repo)


@app.command()
def resume(ctx: typer.Context, repo: str = typer.Argument(None)):
    run_ops(ctx, "resume", repo)


@app.command()
def skip(ctx: typer.Context, repo: str, seq: int):
    run_ops(ctx, "skip_plan", repo, seq)


@app.command()
def retry(ctx: typer.Context, repo: str, seq: int):
    """Retry a blocked or closed plan (a closed PR gets a new -r<k> branch)."""
    run_ops(ctx, "retry_plan", repo, seq)


@app.command()
def cancel(ctx: typer.Context, task_id: str):
    with locked(ctx.obj.factory) as (session, _):
        transition(session, require_task(session, task_id), "cancelled", "Cancelled by operator")
    typer.echo("Cancelled; an active worker stops on its next heartbeat")


@app.command()
def notifications(ctx: typer.Context, all: bool = typer.Option(False, "--all")):
    run_ops(ctx, "get_notifications", None, not all)


@app.command()
def ack(ctx: typer.Context, ids: list[str]):
    run_ops(ctx, "acknowledge_notifications", ids)


@budget.command("set")
def budget_set(ctx: typer.Context, daily: str, monthly: str):
    try:
        values = Decimal(daily), Decimal(monthly)
        if any(not x.is_finite() or x <= 0 for x in values):
            raise ValueError()
    except Exception:
        raise typer.BadParameter("Budget values must be positive finite USD amounts") from None
    with locked(ctx.obj.factory) as (_, control):
        control.daily_micro, control.monthly_micro = map(micro, values)
    typer.echo("Budgets updated (UTC day and calendar month)")


# ---------------------------------------------------------------- repos / plans / tasks
@repos.command("list")
def list_repositories(ctx: typer.Context):
    run_ops(ctx, "list_repositories")


@repos.command("add")
def add_repository(ctx: typer.Context, url: str):
    try:
        result = ctx.obj.ops().add_repository(url)
    except ValueError as exc:
        fail(exc)
    emit(result)
    if not result["accepted"]:
        raise typer.Exit(1)


@repos.command("verify")
def verify_repo(ctx: typer.Context, repo: str):
    try:
        result = ctx.obj.ops().verify_repository(repo)
    except ValueError as exc:
        fail(exc)
    emit(result)
    if not result["verified"]:
        raise typer.Exit(1)


def toggle_repo(ctx, repository, enabled):
    with locked(ctx.obj.factory) as (session, _):
        repo = session.scalar(select(Repository).where(Repository.name == repository))
        if not repo:
            raise typer.BadParameter("Unknown repository; add it first")
        if enabled and not (repo.ruleset_report or {}).get("verified"):
            raise typer.BadParameter("Repository protection is not verified; run repos verify")
        repo.enabled, repo.manual_enabled = enabled, enabled


@repos.command("enable")
def enable_repository(ctx: typer.Context, repository: str):
    toggle_repo(ctx, repository, True)


@repos.command("disable")
def disable_repository(ctx: typer.Context, repository: str):
    toggle_repo(ctx, repository, False)


@plans.command("list")
def list_plans(ctx: typer.Context, repo: str):
    run_ops(ctx, "list_plans", repo)


@plans.command("rescan")
def rescan_plans(ctx: typer.Context, repo: str):
    from autocoder.plans import scan_repository
    settings, factory = ctx.obj
    try:
        found = scan_repository(settings, factory, repo, force=True)
    except ValueError as exc:
        fail(exc)
    emit([{"path": p.path, "valid": not hasattr(p, "reason"), "reason": getattr(p, "reason", None)}
          for p in found])


@plans.command("add")
def add_plan(ctx: typer.Context, repo: str, title: str, objective: str,
             criterion: list[str] = typer.Option(..., "--criterion", "-a"),
             service: list[str] = typer.Option([], "--service")):
    """Render one plan deterministically and open a plan PR (no builder)."""
    run_ops(ctx, "add_plan", repo, title, objective, criterion, service)


@plans.command("draft")
def draft_plans(ctx: typer.Context, repo: str, goal: str):
    """Ask a builder to draft plans for a goal; a plan PR follows (replaces v1 goals)."""
    run_ops(ctx, "request_plan_draft", repo, goal)


@tasks.command("show")
def show_task(ctx: typer.Context, task_id: str):
    run_ops(ctx, "get_task", task_id)


@reports.command("show")
def show_report(ctx: typer.Context, repo: str, seq: int):
    try:
        result = ctx.obj.ops().get_report(repo, seq)
    except ValueError as exc:
        fail(exc)
    if not result["available"]:
        fail(f"No report available yet for {result['report_path']}")
    typer.echo(result["report"]["untrusted_text"])


# ---------------------------------------------------------------- concierge (host side)
def compose(*args, interactive=False):
    command = ["docker", "compose", "exec", *([] if interactive else ["-T"]), *args]
    return subprocess.call(command)


@app.command()
def chat():
    """Chat with the concierge (docker compose exec -it concierge hermes)."""
    raise typer.Exit(compose("concierge", "hermes", interactive=True))


@memory.command("list")
def memory_list():
    raise typer.Exit(compose("concierge", "python", "/opt/concierge/mem0_admin.py", "list"))


@memory.command("search")
def memory_search(query: str):
    raise typer.Exit(compose("concierge", "python", "/opt/concierge/mem0_admin.py", "search", query))


@memory.command("delete")
def memory_delete(memory_id: str):
    raise typer.Exit(compose("concierge", "python", "/opt/concierge/mem0_admin.py", "delete", memory_id))


@memory.command("reset")
def memory_reset(yes: bool = typer.Option(False, "--yes")):
    if not yes:
        raise typer.BadParameter("Pass --yes to delete every concierge memory")
    raise typer.Exit(compose("concierge", "python", "/opt/concierge/mem0_admin.py", "reset", "--yes"))


# ---------------------------------------------------------------- services
@app.command()
def run(ctx: typer.Context, once: bool = False, mcp: bool = typer.Option(True, "--mcp/--no-mcp")):
    """Run the durable scheduler (and the MCP server thread), or one scheduling cycle."""
    import signal

    from autocoder.controller import Controller
    configure_logging()
    settings, factory = ctx.obj
    if mcp and not once and settings.mcp.token_secret.exists():
        from autocoder.config import secret
        from autocoder.mcp_server import serve_in_thread
        serve_in_thread(settings, factory, secret(settings.mcp.token_secret), for_owner)
    controller = Controller(settings, factory)

    def terminate(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        controller.tick() if once else controller.run()
    finally:
        controller.release()


@app.command("mcp")
def mcp_command(ctx: typer.Context):
    """Run only the gatekeeper MCP server (the controller normally runs it as a thread)."""
    from autocoder.config import secret
    from autocoder.mcp_server import serve
    configure_logging()
    settings, factory = ctx.obj
    serve(settings, factory, secret(settings.mcp.token_secret), for_owner)


@app.command()
def proxy(ctx: typer.Context, host: str = "0.0.0.0", port: int = 8080):
    """Run the internal model proxy; never publish this port."""
    import uvicorn

    from autocoder.proxy import create_app
    configure_logging()
    uvicorn.run(create_app(*ctx.obj), host=host, port=port, access_log=False)


@app.command()
def discover(ctx: typer.Context):
    """Refresh managed repositories and rescan their plans once."""
    from autocoder.controller import Controller
    controller = Controller(*ctx.obj)
    if not controller.acquire():
        raise typer.BadParameter("Controller is active; its next tick will refresh repositories")
    try:
        controller.discover()
    finally:
        controller.release()
    list_repositories(ctx)


@app.command()
def health(ctx: typer.Context):
    with ctx.obj.factory() as session:
        control = session.get(Control, 1)
        if not control or time.time() - control.heartbeat > 180:
            raise typer.Exit(1)
    typer.echo("ok")


@app.command()
def prune(ctx: typer.Context):
    """Prune old finished-run logs, never workspaces or reports."""
    settings, factory = ctx.obj
    cutoff = time.time() - settings.log_retention_days * 86400
    with factory() as session:
        items = session.scalars(select(Attempt).where(Attempt.finished_at < cutoff,
                                                      Attempt.outcome != "publication_pending")).all()
    for attempt in items:
        for path in (settings.data_dir / "runs" / attempt.id).rglob("*.log"):
            if path.is_file() and not path.is_symlink():
                path.unlink()


if __name__ == "__main__":
    app()
