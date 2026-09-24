import time
from unittest.mock import Mock

import pytest
from helpers import FakeDocker, github_double
from sqlalchemy import select

from autocoder.budget import issue_capability
from autocoder.controller import Controller, Stopped
from autocoder.models import Attempt, Capability, Control, Notification, Sidecar, Task


def add_task(factory, id="work", **values):
    with factory.begin() as session:
        session.add(Task(id=id, repo_id=1, title="Fix bug", objective="Fix a demonstrated defect",
                         fingerprint=id, plan_path=f"plans/0{values.get('plan_seq', 1)}-fix.md",
                         plan_slug="fix", **{"plan_seq": 1, **values}))


def test_single_controller_lease(settings, factory):
    first, second = Controller(settings, factory), Controller(settings, factory)
    assert first.acquire()
    assert not second.acquire()
    with factory.begin() as session:
        session.get(Control, 1).lease_until = time.time() - 1
    assert second.acquire()
    with pytest.raises(Stopped):
        first.heartbeat(force=True)


def test_interrupted_attempt_is_requeued_and_token_revoked(settings, factory, active, monkeypatch):
    cleanup = Mock()
    monkeypatch.setattr("autocoder.controller.cleanup_attempt", cleanup)
    with factory.begin() as session:
        session.get(Task, "task").attempt_count = 1
        session.add(Sidecar(attempt_id=active, service_name="postgres", image="x", status="healthy"))
    issue_capability(factory, active, 60)
    docker = FakeDocker()
    controller = Controller(settings, factory, docker_client=docker)
    controller.acquire()
    controller.reconcile()
    with factory() as session:
        assert session.get(Task, "task").state == "queued"
        attempt = session.get(Attempt, active)
        assert attempt.outcome == "interrupted" and "restarted" in attempt.detail
        assert all(c.revoked for c in session.scalars(select(Capability)))
        assert session.scalar(select(Sidecar)).status == "removed"
    cleanup.assert_called_once_with(docker, active)


def test_exhausted_interrupted_attempt_blocks(settings, factory, active, monkeypatch):
    monkeypatch.setattr("autocoder.controller.cleanup_attempt", Mock())
    with factory.begin() as session:
        session.get(Task, "task").attempt_count = settings.scheduler.max_attempts_per_task
    controller = Controller(settings, factory, docker_client=FakeDocker())
    controller.acquire()
    controller.reconcile()
    with factory() as session:
        assert session.get(Task, "task").state == "blocked"


def test_publication_reconciliation_does_not_repeat_coding(settings, factory, active, monkeypatch, tmp_path):
    monkeypatch.setattr("autocoder.controller.cleanup_attempt", Mock())
    with factory.begin() as session:
        task = session.get(Task, "task")
        task.state, task.branch, task.plan_seq, task.plan_path = "publishing", "agent/01-fix", 1, "plans/01-fix.md"
        attempt = session.get(Attempt, active)
        attempt.outcome, attempt.workspace, attempt.publication_sha = "publication_pending", str(tmp_path), "sha"
        attempt.detail, attempt.ready_for_review, attempt.finished_at = '{"body": "b"}', True, time.time()
    github = github_double(42)
    git = Mock()
    git.run.side_effect = lambda _dir, *args, **_: "sha" if args[0] == "rev-parse" else ""
    runner = Mock()
    controller = Controller(settings, factory, runner, lambda *_: github, docker_client=FakeDocker(),
                            git_factory=lambda _: git)
    controller.acquire()
    controller.reconcile()
    controller.reconcile()
    assert github.ensure_pr.call_count == 1 and git.push.call_count == 1
    runner.assert_not_called()
    with factory() as session:
        assert session.get(Task, "task").state == "pr_open"
        assert session.get(Task, "task").pr_number == 42


def test_pr_merge_and_rejection(settings, factory):
    add_task(factory, "merged", state="pr_open", pr_number=1, branch="agent/01-fix")
    add_task(factory, "rejected", state="pr_open", pr_number=2, branch="agent/02-fix", plan_seq=2)
    github = github_double()
    github.pull.side_effect = lambda _repo, number: ({"merged_at": "2020-01-01T00:00:00Z", "state": "closed"}
                                                     if number == 1 else {"merged_at": None, "state": "closed"})
    controller = Controller(settings, factory, github_factory=lambda *_: github)
    controller.acquire()
    controller.poll_prs(force=True)
    with factory() as session:
        assert session.get(Task, "merged").state == "merged"
        assert session.get(Task, "rejected").state == "closed_unmerged"
        from autocoder.models import Repository
        assert session.get(Repository, 1).queue_state == "paused"
        levels = {n.level for n in session.scalars(select(Notification))}
        assert levels == {"info", "action_required"}
    github.delete_branch.assert_called_once_with("owner/repo", "agent/01-fix")


def test_poll_is_rate_limited(settings, factory):
    add_task(factory, "open", state="pr_open", pr_number=1, branch="agent/01-fix")
    github = github_double()
    github.pull.return_value = {"state": "open", "head": {"sha": "x"}, "number": 1}
    controller = Controller(settings, factory, github_factory=lambda *_: github)
    controller.acquire()
    controller.poll_prs()
    controller.poll_prs()
    assert github.pull.call_count == 1


def test_repair_limit_blocks_with_notification(settings, factory):
    settings.scheduler.max_repairs_per_task = 1
    settings.scheduler.feedback_debounce_seconds = 0
    add_task(factory, "open", state="pr_open", pr_number=1, branch="agent/01-fix", repair_count=1)
    github = github_double()
    github.pull.return_value = {"state": "open", "head": {"sha": "x"}, "number": 1}
    github.issue_comments.return_value = [{"id": 1, "user": {"login": "owner"}, "created_at":
                                           "2020-01-01T00:00:00Z", "body": "again"}]
    controller = Controller(settings, factory, github_factory=lambda *_: github)
    controller.acquire()
    controller.poll_prs(force=True)
    with factory() as session:
        assert session.get(Task, "open").state == "blocked"
        assert session.scalar(select(Notification).where(Notification.level == "action_required"))


def test_sweep_removes_only_labeled_orphans(settings, factory, active):
    docker = FakeDocker()
    orphan = Mock(labels={"autocoder.managed": "true", "autocoder.attempt": "gone"})
    running = Mock(labels={"autocoder.managed": "true", "autocoder.attempt": active})
    docker.containers.list.return_value = [orphan, running]
    controller = Controller(settings, factory, docker_client=docker)
    controller._sweep_docker()
    orphan.remove.assert_called_once()
    running.remove.assert_not_called()
