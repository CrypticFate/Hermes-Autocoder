"""A seeded repository exercises real git/checks with deterministic external doubles."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy import select

from autocoder.controller import Controller
from autocoder.domain import PlanResult, ReviewResult, RunResult
from autocoder.gitops import Git
from autocoder.models import Attempt, Task


class LocalGit(Git):
    def __init__(self, seed):
        super().__init__()
        self.seed = seed
        self.pushes = []

    def prepare(self, directory, repo, base, branch):
        if not directory.exists():
            shutil.copytree(self.seed, directory)
            self.run(directory, "checkout", "-b", branch)
        return self.run(self.seed, "rev-parse", "HEAD").strip()

    def push(self, directory, branch):
        self.pushes.append((branch, self.run(directory, "rev-parse", "HEAD").strip()))


class FixtureRunner:
    def __init__(self, settings, workspace, run_dir, profile, token, heartbeat):
        self.workspace, self.token = workspace, token
        self.heartbeat = heartbeat

    def start(self, attempt_id):
        return f"fixture-{attempt_id}"

    def command(self, argv):
        if isinstance(argv, str):
            argv = ["sh", "-lc", argv]
        result = subprocess.run(argv, cwd=self.workspace, capture_output=True, text=True, timeout=10,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return {"command": argv, "exit_code": result.returncode, "output": result.stdout + result.stderr}

    def run(self, context):
        self.heartbeat()
        if context.mode == "plan":
            return RunResult(completed=True, plan=PlanResult.model_validate({"tasks": [{
                "key": "sum-fix", "title": "Fix addition", "objective": "Fix add returning subtraction",
                "evidence": "add(2, 3) returns -1 instead of the documented sum 5",
                "acceptance": ["Addition regression passes"], "category": "bug"}]}))
        if context.mode == "review":
            return RunResult(completed=True, review=ReviewResult(accepted=True, acceptance_met=[True]))
        (self.workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        return RunResult(completed=True, summary="Corrected addition; regression passes.")

    def stop(self):
        pass


def test_seeded_plan_code_report_pr_merge(settings, factory, tmp_path, monkeypatch):
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    (seed / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (seed / ".gitignore").write_text("__pycache__/\n")
    (seed / "check.py").write_text("from calc import add\nassert add(2, 3) == 5\n")
    (seed / "uv.lock").write_text("# Fixture uses an explicit independent check command\n")
    git = LocalGit(seed)
    git.commit(seed, "Seed a demonstrable defect")
    settings.profiles["python"].checks = [shlex.join([sys.executable, "check.py"])]
    github = Mock()
    github.repository.return_value = {"id": 1, "default_branch": "main", "archived": False}
    github.permission.return_value = {"permission": "write"}
    github.branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1,
                                                 "require_last_push_approval": True}},
        {"type": "deletion"}, {"type": "non_fast_forward"}]
    github.open_issues.return_value = []
    github.ensure_pr.return_value = {"number": 1, "html_url": "https://github.com/owner/repo/pull/1",
                                     "state": "open", "draft": False}
    controller = Controller(settings, factory, FixtureRunner, lambda *_: github)
    monkeypatch.setattr(controller, "git", lambda _: git)
    assert controller.acquire()
    controller.enqueue_scans()
    controller.execute(*controller.claim())
    with factory() as session:
        child = session.scalar(select(Task).where(Task.source == "maintenance"))
        assert child.state == "ready"
        child_id = child.id
    controller.execute(*controller.claim())
    with factory() as session:
        task = session.get(Task, child_id)
        attempt = session.scalar(select(Attempt).where(Attempt.task_id == child_id))
        assert task.state == "awaiting_review", task.reason
        assert attempt.baseline[0]["exit_code"] == 1
        assert attempt.checks[0]["exit_code"] == 0
        assert len(git.pushes) == 1
        report = Path(attempt.report_path).read_text()
        assert "exit: 1" in report and "exit: 0" in report
        assert "test-github-credential" not in report
        workspace = Path(attempt.workspace)
        assert (workspace / f"plan/{task.plan_id}/overview.md").is_file()
        assert (workspace / f"report/{task.id}/{attempt.id}.md").is_file()
        assert task.tested_sha in report
    github.pull.return_value = {"state": "closed", "merged_at": "now"}
    github.feedback.return_value = []
    controller.poll_prs()
    with factory() as session:
        assert session.get(Task, child_id).state == "merged"
