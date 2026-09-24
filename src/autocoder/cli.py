import json
import logging
import time
from decimal import Decimal
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from autocoder.artifacts import usage
from autocoder.config import load_settings
from autocoder.controller import Controller
from autocoder.db import initialize, locked, micro, require_task, session_factory
from autocoder.domain import fingerprint, transition
from autocoder.github import for_owner
from autocoder.models import Attempt, Control, Repository, Task
from autocoder.onboarding import register_repository, verify_repository
from autocoder.redaction import install_log_redaction, redact

app = typer.Typer(no_args_is_help=True)
repos = typer.Typer(no_args_is_help=True)
goals = typer.Typer(no_args_is_help=True)
budget = typer.Typer(no_args_is_help=True)
reports = typer.Typer(no_args_is_help=True)
app.add_typer(repos, name="repos")
app.add_typer(goals, name="goal")
app.add_typer(budget, name="budget")
app.add_typer(reports, name="report")


@app.callback()
def main(ctx: typer.Context, config: Path = typer.Option(Path("config.yaml"), "--config", "-c")):
    """Operate the Hermes autonomous maintenance service."""
    install_log_redaction()
    try:
        settings = load_settings(config)
    except (ValueError, OSError) as exc:
        typer.echo(redact(str(exc)), err=True)
        raise typer.Exit(1) from None
    ctx.obj = settings, session_factory(settings)


@app.command()
def init(ctx: typer.Context):
    """Migrate the database and initialize the service in paused mode."""
    settings, factory = ctx.obj
    configuration = Config("alembic.ini")
    configuration.attributes["database_url"] = settings.database_url
    command.upgrade(configuration, "head")
    initialize(factory, settings)
    typer.echo("Database ready. New installations start paused.")


@app.command()
def doctor(ctx: typer.Context):
    """Check deployment prerequisites without calling a paid model."""
    import docker
    settings, factory = ctx.obj
    problems = settings.paid_errors()
    try:
        login = for_owner(settings, settings.owners[0]).verify_identity()
        typer.echo(f"GitHub bot identity verified: {login}")
    except Exception as exc:
        problems.append(f"GitHub identity check: {redact(str(exc))}")
    try:
        with factory() as session:
            control = session.get(Control, 1)
            if not control or not control.daily_micro or not control.monthly_micro:
                problems.append("Daily/monthly spending limits required")
        client = docker.from_env()
        client.ping()
        client.networks.get(settings.worker_network)
        client.images.get(settings.builder.image)
    except Exception as exc:
        problems.append(f"Infrastructure check: {type(exc).__name__}")
    typer.echo(redact("\n".join(problems)) if problems else "Phase 1 checks passed; v2 deployment checks pending")
    if problems:
        raise typer.Exit(1)


@app.command()
def status(ctx: typer.Context):
    _, factory = ctx.obj
    with factory() as session:
        control = session.get(Control, 1)
        typer.echo(json.dumps({"paused": control.paused, "heartbeat": control.heartbeat,
                              "charged_usd": usage(session),
                              "daily_usd": None if control.daily_micro is None else control.daily_micro / 1e6,
                              "monthly_usd": None if control.monthly_micro is None else control.monthly_micro / 1e6,
                              "tasks": [{"id": t.id, "title": t.title, "state": t.state,
                                         "reason": t.reason, "pr": t.pr_url} for t in session.scalars(
                                             select(Task).order_by(Task.created_at.desc()).limit(100))]}, indent=2))


@app.command()
def pause(ctx: typer.Context):
    """Stop new work and interrupt active workers on their next heartbeat."""
    with locked(ctx.obj[1]) as (_, control):
        control.paused = True
    typer.echo("Paused")


@app.command()
def resume(ctx: typer.Context):
    settings, factory = ctx.obj
    errors = settings.paid_errors()
    if errors:
        raise typer.BadParameter("; ".join(errors))
    with locked(factory) as (_, control):
        if not control.daily_micro or not control.monthly_micro:
            raise typer.BadParameter("Set daily and monthly budgets first")
        control.paused = False
    typer.echo("Resumed")


@app.command()
def cancel(ctx: typer.Context, task_id: str):
    with locked(ctx.obj[1]) as (session, _):
        transition(session, require_task(session, task_id), "cancelled", "Cancelled by operator")
    typer.echo("Cancelled; active worker stops on its next heartbeat")


@app.command()
def retry(ctx: typer.Context, task_id: str):
    """Requeue a blocked/failed task and reset its attempt allowance."""
    with locked(ctx.obj[1]) as (session, _):
        task = require_task(session, task_id)
        if task.state not in {"blocked", "failed"}:
            raise typer.BadParameter("Only blocked or failed tasks can be retried")
        task.attempt_count = 0
        transition(session, task, "queued" if task.source in {"scan", "goal"} else "ready", "Operator retry")
    typer.echo("Queued for retry")


@budget.command("set")
def budget_set(ctx: typer.Context, daily: str, monthly: str):
    try:
        values = Decimal(daily), Decimal(monthly)
        if any(not x.is_finite() or x <= 0 for x in values):
            raise ValueError()
    except Exception:
        raise typer.BadParameter("Budget values must be positive finite USD amounts") from None
    with locked(ctx.obj[1]) as (session, control):
        control.daily_micro, control.monthly_micro = map(micro, values)
        for task in session.scalars(select(Task).where(Task.reason == "Budget exhausted; waiting for allowance")):
            task.retry_at = 0
    typer.echo("Budgets updated (UTC day and calendar month)")


@repos.command("list")
def list_repositories(ctx: typer.Context):
    with ctx.obj[1]() as session:
        for repo in session.scalars(select(Repository).order_by(Repository.name)):
            typer.echo(f"{repo.name}\t{'enabled' if repo.enabled else 'disabled'}\t{repo.reason}")


@repos.command("add")
def add_repository(ctx: typer.Context, url: str):
    result = register_repository(*ctx.obj, url)
    typer.echo(result.model_dump_json())
    if not result.accepted:
        raise typer.Exit(1)


@repos.command("verify")
def verify_repo(ctx: typer.Context, repo: str):
    result = verify_repository(*ctx.obj, repo)
    typer.echo(result.model_dump_json())
    if not result.verified:
        raise typer.Exit(1)


@repos.command("create")
def create_repository(ctx: typer.Context, repository: str):
    """Create a private GitHub repository; does not automatically enable coding."""
    settings, _ = ctx.obj
    parts = repository.split("/")
    if len(parts) != 2:
        raise typer.BadParameter("Use OWNER/REPOSITORY")
    owner = next((o for o in settings.owners if o.lower() == parts[0].lower()), None)
    if owner is None:
        raise typer.BadParameter("Owner is not configured")
    result = for_owner(settings, owner).create_repository(owner, parts[1])
    typer.echo(result["html_url"])


def toggle_repo(ctx, repository, enabled):
    with locked(ctx.obj[1]) as (session, _):
        repo = session.scalar(select(Repository).where(Repository.name == repository))
        if not repo:
            raise typer.BadParameter("Unknown repository; run discover first")
        if enabled and not repo.accessible:
            raise typer.BadParameter(repo.reason)
        repo.enabled, repo.manual_enabled = enabled, enabled


@repos.command("enable")
def enable_repository(ctx: typer.Context, repository: str):
    toggle_repo(ctx, repository, True)


@repos.command("disable")
def disable_repository(ctx: typer.Context, repository: str):
    toggle_repo(ctx, repository, False)


@app.command()
def discover(ctx: typer.Context):
    controller = Controller(*ctx.obj)
    if not controller.acquire():
        raise typer.BadParameter("Controller is active; stop it before manual discovery")
    try:
        controller.discover()
    finally:
        with locked(ctx.obj[1]) as (_, control):
            if control.lease_owner == controller.identity:
                control.lease_owner, control.lease_until = None, 0
    list_repositories(ctx)


@goals.command("add")
def add_goal(ctx: typer.Context, repository: str, objective: str,
             acceptance: list[str] = typer.Option(..., "--acceptance", "-a")):
    with locked(ctx.obj[1]) as (session, _):
        repo = session.scalar(select(Repository).where(Repository.name == repository))
        if not repo:
            raise typer.BadParameter("Unknown repository")
        key = fingerprint(objective)
        task = session.scalar(select(Task).where(Task.repo_id == repo.id, Task.fingerprint == key))
        if not task:
            task = Task(repo_id=repo.id, source="goal", title=objective[:160], objective=objective,
                        acceptance=acceptance, fingerprint=key)
            session.add(task)
            session.flush()
        typer.echo(task.id)


@reports.command("show")
def show_report(ctx: typer.Context, task_id: str):
    with ctx.obj[1]() as session:
        attempt = session.scalar(select(Attempt).where(Attempt.task_id == task_id,
                                                       Attempt.report_path.is_not(None)).order_by(
                                                           Attempt.started_at.desc()))
        if not attempt:
            raise typer.BadParameter("No report available")
        typer.echo(Path(attempt.report_path).read_text())


@app.command()
def run(ctx: typer.Context, once: bool = False):
    """Run the durable scheduler, or execute one scheduling cycle."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    install_log_redaction()
    controller = Controller(*ctx.obj)
    import signal
    def terminate(_signal, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        controller.tick() if once else controller.run()
    finally:
        with locked(ctx.obj[1]) as (_, control):
            if control.lease_owner == controller.identity:
                control.lease_owner, control.lease_until = None, 0


@app.command()
def proxy(ctx: typer.Context, host: str = "0.0.0.0", port: int = 8080):
    """Run the internal model proxy; do not expose this port publicly."""
    import uvicorn

    from autocoder.proxy import create_app
    uvicorn.run(create_app(*ctx.obj), host=host, port=port, access_log=False)


@app.command()
def health(ctx: typer.Context):
    with ctx.obj[1]() as session:
        control = session.get(Control, 1)
        if not control or time.time() - control.heartbeat > 180:
            raise typer.Exit(1)
    typer.echo("ok")


@app.command()
def prune(ctx: typer.Context):
    """Prune only old finished-run logs, never workspaces or reports."""
    settings, factory = ctx.obj
    cutoff = time.time() - settings.log_retention_days * 86400
    with factory() as session:
        items = session.scalars(select(Attempt).where(Attempt.finished_at < cutoff,
                                                      Attempt.outcome != "publication_pending")).all()
    for attempt in items:
        for path in (settings.data_dir / "runs" / attempt.id).glob("*.log"):
            if path.is_file() and not path.is_symlink():
                path.unlink()


if __name__ == "__main__":
    app()
