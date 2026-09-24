import json
from datetime import datetime, timezone

import yaml
from sqlalchemy import func, select

from autocoder.models import Charge, Task
from autocoder.security import redact, safe_write


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else "pending"


def render_plan(session, task, workspace):
    tasks = session.scalars(select(Task).where(Task.plan_id == task.plan_id).order_by(Task.created_at)).all()
    rows = [f"# Plan {task.plan_id}", "", "Status is a snapshot; current status is available through autocoder status.", ""]
    for index, item in enumerate(tasks, 1):
        relative = f"plan/{task.plan_id}/{index:02d}-{item.id}.md"
        rows.append(f"- [{item.title}]({index:02d}-{item.id}.md): {item.state}")
        meta = {"schema_version": 1, "task_id": item.id, "plan_id": item.plan_id,
                "state": item.state, "dependencies": item.dependencies, "source": item.source}
        content = "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n"
        content += f"# {item.title}\n\n{item.objective}\n\n## Evidence\n\n{item.evidence}\n\n## Acceptance\n\n"
        content += "\n".join(f"- {entry}" for entry in item.acceptance) + "\n"
        content += "\n## Validation Commands\n\n```json\n" + json.dumps(item.validation_commands, indent=2) + "\n```\n"
        safe_write(workspace, relative, redact(content))
    safe_write(workspace, f"plan/{task.plan_id}/overview.md", redact("\n".join(rows) + "\n"))


def render_report(session, task, attempt, workspace=None):
    charges = session.scalars(select(Charge).where(Charge.attempt_id == attempt.id)).all()
    cost = sum(c.micro_usd for c in charges) / 1_000_000
    estimated = any(c.state != "measured" for c in charges)
    content = f"""# {task.title}: attempt {attempt.id}

- Task: {task.id}
- Outcome: {attempt.outcome}
- Started: {stamp(attempt.started_at)}
- Finished: {stamp(attempt.finished_at)}
- Base SHA: {task.base_sha or 'unknown'}
- Tested SHA: {task.tested_sha or 'not established'}
- Model cost: USD {cost:.6f} ({'includes conservative estimates' if estimated else 'recorded usage'})
- PR: {task.pr_url or 'pending publication'}

## Objective

{task.objective}

## Result and Next Action

{attempt.detail or task.reason or 'Review the pull request; merging remains a human action.'}

## Changed Files

"""
    content += "\n".join(f"- `{path}`" for path in attempt.changed_files) or "None"
    for title, results in (("Dependency Setup", attempt.setup), ("Baseline Checks", attempt.baseline),
                           ("Validation Checks", attempt.checks)):
        content += f"\n\n## {title}\n\n"
        if not results:
            content += "Not run.\n"
        for result in results:
            content += f"Command: `{json.dumps(result['command'])}`; exit: {result['exit_code']}\n\n"
            content += "<details><summary>Captured output</summary>\n\n```text\n"
            content += result["output"].replace("```", "[code fence]")[-4000:] + "\n```\n</details>\n\n"
    content = redact(content)
    relative = f"report/{task.id}/{attempt.id}.md"
    if workspace:
        safe_write(workspace, relative, content)
    return relative, content


def usage(session):
    # PostgreSQL sums BIGINT as Decimal; normalize before JSON serialization.
    return int(session.scalar(select(func.coalesce(func.sum(Charge.micro_usd), 0)))) / 1_000_000
