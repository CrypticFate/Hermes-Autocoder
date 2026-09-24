import time
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from autocoder.controller import Controller, Stopped
from autocoder.domain import PlanResult, transition
from autocoder.models import Attempt, Control, Repository, Task


def add_task(factory, id="work", **values):
    with factory.begin() as session:
        session.add(Task(id=id, repo_id=1, title="Fix bug", objective="Fix a demonstrated defect",
                         fingerprint=id, **values))


def test_single_controller_lease(settings, factory):
    first, second = Controller(settings, factory), Controller(settings, factory)
    assert first.acquire()
    assert not second.acquire()
    with factory.begin() as session:
        session.get(Control, 1).lease_until = time.time() - 1
    assert second.acquire()
    with pytest.raises(Stopped):
        first.heartbeat(force=True)


def test_dependency_requires_actual_merge(settings, factory):
    add_task(factory, "first", state="awaiting_review", pr_number=1)
    add_task(factory, "second", state="ready", dependencies=["first"])
    controller = Controller(settings, factory)
    assert controller.acquire()
    assert controller.claim() is None
    with factory.begin() as session:
        transition(session, session.get(Task, "first"), "merged")
    assert controller.claim()[0] == "second"


def test_pr_backlog_cap(settings, factory):
    settings.max_open_prs = 1
    add_task(factory, "first", state="awaiting_review", pr_number=1)
    add_task(factory, "second", state="ready")
    controller = Controller(settings, factory)
    controller.acquire()
    assert controller.claim() is None
    controller.enqueue_scans()
    with factory() as session:
        assert len(session.scalars(select(Task)).all()) == 2


def test_plan_graph_and_deduplication(settings, factory):
    controller = Controller(settings, factory)
    add_task(factory, "parent", state="planning", source="scan")
    proposals = {"tasks": [
        {"key": "a", "title": "Fix parser", "objective": "Fix parser rejecting valid input",
         "evidence": "Parser fails on documented valid input", "acceptance": ["Parser test passes"], "category": "bug"},
        {"key": "b", "title": "Document parser", "objective": "Document supported parser input",
         "evidence": "Current example omits valid parser input", "acceptance": ["Example verified"],
         "category": "documentation", "dependencies": ["a"]}]}
    controller.save_plan("parent", PlanResult.model_validate(proposals))
    add_task(factory, "other", state="planning", source="scan")
    controller.save_plan("other", PlanResult.model_validate(proposals))
    with factory() as session:
        children = session.scalars(select(Task).where(Task.source == "maintenance")).all()
        assert len(children) == 2
        child = next(t for t in children if t.title == "Document parser")
        assert child.dependencies == [next(t.id for t in children if t.title == "Fix parser")]
    proposals["tasks"][0]["dependencies"] = ["b"]
    with pytest.raises(ValueError, match="cycle"):
        PlanResult.model_validate(proposals)


def test_discovery_pagination_scope_and_revocation(settings, factory):
    settings.repositories.auto_enroll = True
    github = Mock()
    github.repositories.return_value = [
        {"id": 1, "full_name": "owner/renamed", "size": 10, "permissions": {"push": True}},
        {"id": 2, "full_name": "owner/archive", "size": 10, "archived": True, "permissions": {"push": True}},
        {"id": 3, "full_name": "owner/fork", "size": 10, "fork": True, "permissions": {"push": True}}]
    controller = Controller(settings, factory, github_factory=lambda *_: github)
    controller.acquire()
    controller.discover()
    with factory() as session:
        assert session.get(Repository, 1).name == "owner/renamed"
        assert session.get(Repository, 1).enabled
        assert not session.get(Repository, 2).enabled
        assert not session.get(Repository, 3).enabled
    github.repositories.return_value = []
    controller.discover()
    with factory() as session:
        assert not session.get(Repository, 1).accessible


def test_restart_preserves_workspace_requeues_and_reports(settings, factory, active, monkeypatch):
    cleanup = Mock()
    monkeypatch.setattr("autocoder.controller.cleanup_attempt", cleanup)
    controller = Controller(settings, factory)
    controller.acquire()
    controller.reconcile()
    with factory() as session:
        assert session.get(Task, "task").state == "ready"
        attempt = session.get(Attempt, active)
        assert attempt.outcome == "interrupted"
        assert "restarted" in attempt.detail
        assert attempt.report_path
    cleanup.assert_called_once_with(active)


def test_publication_reconciliation_does_not_repeat_coding(settings, factory, active, monkeypatch, tmp_path):
    monkeypatch.setattr("autocoder.controller.cleanup_attempt", Mock())
    with factory.begin() as session:
        task = session.get(Task, "task")
        task.state, task.branch = "validating", "agent/task"
        attempt = session.get(Attempt, active)
        attempt.outcome, attempt.workspace = "publication_pending", str(tmp_path)
        attempt.checks = [{"exit_code": 0}]
    github = Mock()
    github.ensure_pr.return_value = {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}
    github.repository.return_value = {"id": 1, "default_branch": "main", "archived": False}
    github.permission.return_value = {"permission": "write"}
    github.branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1,
                                                 "require_last_push_approval": True}},
        {"type": "deletion"}, {"type": "non_fast_forward"}]
    controller = Controller(settings, factory, github_factory=lambda *_: github)
    git = Mock()
    monkeypatch.setattr(controller, "git", lambda _: git)
    controller.acquire()
    controller.reconcile()
    controller.reconcile()
    github.ensure_pr.assert_called_once()
    git.push.assert_called_once()
    with factory() as session:
        assert session.get(Task, "task").state == "awaiting_review"


def test_pr_merge_and_rejection(settings, factory):
    add_task(factory, "merged", state="awaiting_review", pr_number=1)
    add_task(factory, "rejected", state="awaiting_review", pr_number=2)
    github = Mock()
    github.pull.side_effect = [{"merged_at": "now", "state": "closed"}, {"merged_at": None, "state": "closed"}]
    github.feedback.return_value = []
    controller = Controller(settings, factory, github_factory=lambda *_: github)
    controller.acquire()
    controller.poll_prs()
    with factory() as session:
        assert session.get(Task, "merged").state == "merged"
        assert session.get(Task, "rejected").state == "closed_unmerged"


def test_finished_worker_cleanup_is_retried(settings, factory, active, monkeypatch):
    cleanup = Mock()
    monkeypatch.setattr("autocoder.controller.cleanup_attempt", cleanup)
    with factory.begin() as session:
        attempt = session.get(Attempt, active)
        attempt.finished_at, attempt.container_id = time.time(), "orphan-worker"
        attempt.outcome = "blocked"
        session.get(Task, "task").state = "blocked"
    controller = Controller(settings, factory)
    controller.acquire()
    controller.reconcile()
    cleanup.assert_called_once_with(active)
    with factory() as session:
        assert session.get(Attempt, active).container_id is None
