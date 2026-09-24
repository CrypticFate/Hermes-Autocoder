import time

from sqlalchemy import select

from autocoder.db import locked
from autocoder.models import Attempt, Event, Repository, Task
from autocoder.notifications import notify

ACTIVE = {"queued", "preparing", "running", "validating", "publishing", "pr_open", "changes_requested"}
FINISHED = {"merged", "skipped", "cancelled"}
TRANSITIONS = {
    "pending": {"queued", "invalid", "cancelled", "skipped"},
    "queued": {"preparing", "planning", "blocked", "cancelled", "skipped"},
    "preparing": {"running", "queued", "blocked", "cancelled"},
    "running": {"validating", "queued", "blocked", "failed", "cancelled"},
    "validating": {"publishing", "queued", "blocked", "cancelled", "running", "pr_open", "failed"},
    "publishing": {"pr_open", "blocked", "cancelled"},
    "pr_open": {"changes_requested", "merged", "closed_unmerged", "skipped", "awaiting_review", "blocked", "cancelled"},
    "changes_requested": {"queued", "blocked", "merged", "closed_unmerged", "skipped"},
    "blocked": {"queued", "ready", "pr_open", "merged", "closed_unmerged", "cancelled", "skipped"},
    "invalid": {"pending", "cancelled", "skipped"},
    "closed_unmerged": {"queued", "skipped"}, "cancelled": {"pending"},
    "merged": set(), "skipped": set(),
    # Historical v1 states remain readable/recoverable during migration.
    "planning": {"ready", "planned", "blocked", "failed", "cancelled"},
    "ready": {"running", "blocked", "cancelled"},
    "awaiting_review": {"ready", "merged", "closed_unmerged", "blocked", "cancelled"},
    "failed": {"queued", "ready", "cancelled"}, "planned": set(),
}


def transition(session, task, to_state, reason=""):
    if to_state == task.state:
        return
    if to_state not in TRANSITIONS.get(task.state, set()):
        raise ValueError(f"Illegal task transition: {task.state} -> {to_state}")
    session.add(Event(task_id=task.id, kind=f"{task.state}->{to_state}", detail=reason))
    task.state, task.reason, task.updated_at = to_state, reason, time.time()
    if to_state == "closed_unmerged" and task.kind == "plan_draft":
        notify(session, f"Plan PR #{task.pr_number} was closed without merging. No new plan draft will be "
               "requested until you ask for one.", level="action_required", repo_id=task.repo_id,
               task_id=task.id, key=f"closed:{task.id}:{task.pr_number}")
    elif to_state == "closed_unmerged":
        repo = session.get(Repository, task.repo_id)
        repo.queue_state = "paused"
        seq = f"{task.plan_seq:02d}" if task.plan_seq is not None else "?"
        notify(session, f"PR #{task.pr_number} for plan {seq} was closed. Say 'skip plan {seq}', "
               f"'retry plan {seq}', or edit the plan.", level="action_required", repo_id=repo.id,
               task_id=task.id, key=f"closed:{task.id}:{task.pr_number}")


def next_task(session, repo):
    if not repo.enabled or repo.queue_state != "active":
        return None
    tasks = session.scalars(select(Task).where(Task.repo_id == repo.id).order_by(Task.plan_seq, Task.created_at)).all()
    if any(t.state in ACTIVE for t in tasks):
        return None
    drafts = [t for t in tasks if t.kind == "plan_draft" and t.state == "pending"]
    if drafts:
        return drafts[0]
    for task in (t for t in tasks if t.kind == "implementation" and t.plan_path):
        if task.state in FINISHED:
            continue
        if task.state == "pending":
            return task
        notify(session, f"Plan {task.plan_seq} is {task.state}; the queue is stalled", level="action_required",
               repo_id=repo.id, task_id=task.id, key=f"stall:{task.id}:{task.state}")
        return None
    return None


def pause(factory, repo=None, *, paused=True):
    with locked(factory) as (session, control):
        if repo is None:
            control.paused = paused
        else:
            row = session.scalar(select(Repository).where(Repository.name == repo))
            if row is None:
                raise ValueError("Unknown managed repository")
            if not paused and row.queue_state in {"blocked_ruleset", "blocked_invalid_plans"}:
                raise ValueError("Verify repository protection and fix invalid plans before resuming")
            row.queue_state = "paused" if paused else "active"
    return {"paused": paused, "repo": repo}


def control_plan(factory, repo, seq, *, retry=False):
    with locked(factory) as (session, _):
        row = session.scalar(select(Repository).where(Repository.name == repo))
        if row is None:
            raise ValueError("Unknown managed repository")
        task = session.scalar(select(Task).where(Task.repo_id == row.id, Task.plan_seq == seq))
        if task is None:
            raise ValueError("Unknown plan")
        if retry:
            if task.state not in {"blocked", "closed_unmerged"}:
                raise ValueError("Only blocked or closed plans can be retried")
            if any(t.id != task.id and t.state in ACTIVE for t in session.scalars(
                    select(Task).where(Task.repo_id == row.id))):
                raise ValueError("Another task is active")
            if task.state == "closed_unmerged":
                task.retry_count += 1
                task.branch = f"agent/{task.plan_seq:02d}-{task.plan_slug}-r{task.retry_count}"
                task.pr_number = task.pr_url = None
            task.attempt_count = task.repair_count = 0
            transition(session, task, "queued", "Operator retry")
            row.queue_state = "active"
        else:
            transition(session, task, "skipped", "Operator skip")
        return {"task_id": task.id, "state": task.state}


def claim(factory, settings, identity):
    with locked(factory) as (session, control):
        if control.paused or control.lease_owner != identity:
            return None
        if session.scalar(select(Task.id).where(Task.state.in_({"preparing", "running", "validating", "publishing"}))):
            return None
        for repo in session.scalars(select(Repository).order_by(Repository.id)):
            if not repo.enabled or repo.queue_state != "active":
                continue
            task = session.scalar(select(Task).where(Task.repo_id == repo.id, Task.state == "queued")
                                  .order_by(Task.created_at).limit(1))
            if task is None:
                task = next_task(session, repo)
                if task:
                    transition(session, task, "queued", "Next plan in sequence")
            if task is None:
                continue
            if task.attempt_count >= settings.scheduler.max_attempts_per_task:
                transition(session, task, "blocked", "Initial attempts exhausted")
                continue
            transition(session, task, "preparing", "Claimed by controller")
            task.attempt_count += 1
            if not task.branch:
                task.branch = (f"agent/plans-{task.id[:12]}" if task.kind == "plan_draft"
                               else f"agent/{task.plan_seq:02d}-{task.plan_slug}")
            attempt = Attempt(task_id=task.id, mode="plan" if task.kind == "plan_draft" else "implement")
            session.add(attempt)
            session.flush()
            return task.id, attempt.id
