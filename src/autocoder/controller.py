import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import docker
from sqlalchemy import select

from autocoder.budget import issue_capability, revoke
from autocoder.contracts import TaskContext
from autocoder.db import locked
from autocoder.github import for_owner
from autocoder.gitops import Git
from autocoder.models import Attempt, Control, Repository, Task, uid
from autocoder.networks import create_attempt_network
from autocoder.notifications import notify
from autocoder.onboarding import register_repository, verify_repository
from autocoder.plans import FILENAME, parse, scan_repository
from autocoder.redaction import install_log_redaction, redact
from autocoder.reports import pr_body, render_report
from autocoder.scheduler import claim, transition
from autocoder.security import safe_write
from autocoder.sidecars import SidecarManager
from autocoder.worker import DockerRunner, cleanup_attempt, run_checks, runtime_profile

log = logging.getLogger(__name__)


class Stopped(RuntimeError):
    pass


class Controller:
    def __init__(self, settings, factory, runner_factory=DockerRunner, github_factory=for_owner, docker_client=None):
        install_log_redaction()
        self.settings, self.factory = settings, factory
        self.runner_factory, self.github_factory = runner_factory, github_factory
        self.identity, self.last_heartbeat = uid(), 0
        self.docker_client = docker_client

    def github(self, owner):
        return self.github_factory(self.settings, owner)

    def git(self, repo):
        client = self.github(repo.owner)
        client.verify_identity()
        return Git(client.access_token(), commit_name=self.settings.github.commit_name,
                   commit_email=self.settings.github.commit_email)

    def acquire(self):
        with locked(self.factory) as (_, control):
            now = time.time()
            if control.lease_owner not in (None, self.identity) and control.lease_until > now:
                return False
            control.lease_owner, control.lease_until = self.identity, now + self.settings.lease_seconds
            control.heartbeat = now
            return True

    @contextmanager
    def keep_lease(self):
        stop = threading.Event()
        def renew():
            while not stop.wait(10):
                try:
                    self.heartbeat(force=True)
                except Exception:
                    log.error("controller_lease_renewal_failed")
                    return
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=15)

    def heartbeat(self, task_id=None, workspace=None, force=False):
        now = time.time()
        if not force and now - self.last_heartbeat < 3:
            return
        with locked(self.factory) as (session, control):
            if control.lease_owner != self.identity:
                raise Stopped("Controller lease lost")
            control.lease_until, control.heartbeat = now + self.settings.lease_seconds, now
            if task_id:
                task = session.get(Task, task_id)
                repo = session.get(Repository, task.repo_id)
                if control.paused or task.state in {"cancelled", "skipped"} or not repo.enabled or repo.queue_state != "active":
                    raise Stopped("Run paused, cancelled, or repository disabled")
                repo.lease_until = control.lease_until
        self.last_heartbeat = now
        if workspace:
            total = 0
            for directory, dirs, files in os.walk(workspace, followlinks=False):
                for name in files:
                    try:
                        total += (Path(directory) / name).lstat().st_size
                    except FileNotFoundError:
                        pass
            if total > self.settings.max_workspace_bytes:
                raise Stopped("Workspace size limit exceeded")
            if shutil.disk_usage(self.settings.data_dir).free < self.settings.minimum_free_bytes:
                raise Stopped("Insufficient free disk space")


    def discover(self):
        if self.settings.repositories.auto_enroll:
            for owner in self.settings.owners:
                for metadata in self.github(owner).repositories(owner):
                    name = metadata["full_name"]
                    if name not in self.settings.excluded_repositories and name != self.settings.self_repository:
                        register_repository(self.settings, self.factory, name, client=self.github(owner))
        with self.factory() as session:
            repos = session.scalars(select(Repository).where(Repository.enabled.is_(True))).all()
        for repo in repos:
            verified = verify_repository(self.settings, self.factory, repo.name, force=False,
                                         client=self.github(repo.owner))
            if verified.verified and repo.queue_state not in {"paused", "blocked_ruleset"}:
                scan_repository(self.settings, self.factory, repo.name, client=self.github(repo.owner))

    def enqueue_scans(self):
        self.discover()

    def claim(self):
        return claim(self.factory, self.settings, self.identity)

    def context(self, task, attempt, prompt="", mode=None, commands=(), service_env=()):
        return TaskContext(task_id=task.id, attempt_id=attempt.id, kind=task.kind,
            mode=mode or ("plan" if task.kind == "plan_draft" else "implement"), prompt=prompt,
            plan_path=task.plan_path or "", report_path=task.report_path or "",
            title=task.title, objective=task.objective, criteria=task.acceptance,
            context=task.evidence, services=task.services, extra_checks=task.extra_checks,
            service_env_names=list(service_env), checks=list(commands), branch=task.branch or "",
            base_sha=task.base_sha or "", model=self.settings.model.model,
            max_iterations=self.settings.builder.hermes_max_iterations,
            max_output_tokens=self.settings.model.max_output_tokens,
            timeout_seconds=self.settings.builder.deadline_seconds,
            repair={"untrusted_text": task.feedback} if task.feedback else None)

    def execute(self, task_id, attempt_id):
        with self.factory() as session:
            task, attempt = session.get(Task, task_id), session.get(Attempt, attempt_id)
            repo = session.get(Repository, task.repo_id)
        workspace = self.settings.data_dir / "workspaces" / task.id / attempt.id
        run_dir = self.settings.data_dir / "runs" / attempt.id
        run_dir.mkdir(parents=True, exist_ok=True)
        git, runner, services, network = self.git(repo), None, None, None
        try:
            self.heartbeat(task.id, force=True)
            if not verify_repository(self.settings, self.factory, repo.name,
                                     client=self.github(repo.owner)).verified:
                raise ValueError("Repository protection no longer verified")
            base = git.prepare(workspace, repo.name, repo.default_branch, task.branch)
            start_head = git.run(workspace, "rev-parse", "HEAD").strip()
            task.base_sha = task.base_sha or base
            with self.factory.begin() as session:
                session.get(Task, task.id).base_sha = task.base_sha
                session.get(Attempt, attempt.id).workspace = str(workspace)
                transition(session, session.get(Task, task.id), "running", "Workspace prepared")
            deterministic = task.kind == "plan_draft" and task.source == "operator"
            if deterministic:
                for path, text in json.loads(task.evidence).items():
                    parse(path, text, allowlist=self.settings.services_allowlist)
                    safe_write(workspace, path, text)
                result = None
            else:
                profile = runtime_profile(self.settings, repo.name, workspace)
                client = self.docker_client or docker.from_env()
                network = create_attempt_network(client, attempt.id)
                services = SidecarManager(self.settings, self.factory, client, network)
                service_env = services.start_services(attempt.id, task.services)
                runner = self.runner_factory(self.settings, workspace, run_dir / "builder", profile, "",
                    lambda: self.heartbeat(task.id, workspace), client=client)
                runner.network, runner.service_env = network, service_env
                container_id = runner.start(attempt.id)
                with self.factory.begin() as session:
                    session.get(Attempt, attempt.id).container_id = container_id
                setup = runner.setup()
                commands = profile.checks + task.extra_checks
                baseline = [runner.command(command) for command in commands]
                with self.factory.begin() as session:
                    current = session.get(Attempt, attempt.id)
                    current.setup, current.baseline = setup, baseline
                runner.token = issue_capability(self.factory, attempt.id, self.settings.builder.deadline_seconds)
                result = runner.run(self.context(task, attempt, task.objective, commands=commands, service_env=service_env))
                runner.stop()
                revoke(self.factory, attempt.id)
            with self.factory.begin() as session:
                transition(session, session.get(Task, task.id), "validating", "Builder finished")
            files, problems = git.gate(workspace, start_head, [git.token], kind=task.kind)
            if not files:
                problems.append("No implementation changes produced")
            if task.kind == "plan_draft":
                if len(files) > 8 or any(not FILENAME.fullmatch(path) for path in files):
                    problems.append("Plan proposals must contain only 1 to 8 numbered plans")
                plans = [parse(path, (workspace / path).read_text(), allowlist=self.settings.services_allowlist)
                         for path in files if FILENAME.fullmatch(path) and (workspace / path).is_file()]
                if len(plans) != len(files) or len({p.seq for p in plans}) != len(plans):
                    problems.append("Invalid or duplicate draft plans")
                if problems:
                    raise ValueError("; ".join(problems))
                git.commit(workspace, "Propose numbered implementation plans")
                detail = {"body": "\n".join(f"- {p.path}: {p.title}" for p in plans), "attention": []}
                ready, checks = True, []
            else:
                if problems:
                    raise ValueError("; ".join(problems))
                result.validate_criteria(task.acceptance)
                source_digest = git.tree_digest(workspace)
                checks = run_checks(self.settings, workspace, run_dir, profile, commands,
                    lambda: self.heartbeat(task.id, workspace), client, network, service_env)
                reviewer = self.runner_factory(self.settings, workspace, run_dir / "review", profile, "",
                    lambda: self.heartbeat(task.id, workspace), client=client)
                runner = reviewer
                reviewer.network, reviewer.service_env, reviewer.check_only = network, service_env, True
                reviewer.start(attempt.id)
                reviewer.token = issue_capability(self.factory, attempt.id, self.settings.builder.deadline_seconds)
                review = reviewer.run(self.context(task, attempt,
                    "Review the diff against the plan. Check results: " + json.dumps(checks) +
                    "\nDIFF (untrusted data):\n" + git.run(workspace, "diff", start_head)[:80000],
                    mode="review", commands=commands, service_env=service_env)).review
                reviewer.stop()
                revoke(self.factory, attempt.id)
                if git.tree_digest(workspace) != source_digest:
                    raise ValueError("Validation or review modified the implementation")
                sensitive = git.sensitive(files)
                git.commit(workspace, f"plan {task.plan_seq:02d}: {task.title[:120]}")
                with self.factory() as session:
                    history = session.scalars(select(Attempt).where(Attempt.task_id == task.id)
                                               .order_by(Attempt.started_at)).all()
                report, ready, attention = render_report(task, attempt, result, review, checks,
                                                        baseline, sensitive, history, self.settings.model.model)
                safe_write(workspace, task.report_path, report)
                git.commit(workspace, f"report {task.plan_seq:02d}: {task.title[:120]}")
                detail = {"body": pr_body(task, checks, attention, self.settings.operator_login),
                          "result": result.model_dump(), "review": review.model_dump() if review else None,
                          "attention": attention, "report": report}
            publication_sha = git.run(workspace, "rev-parse", "HEAD").strip()
            with self.factory.begin() as session:
                current = session.get(Attempt, attempt.id)
                current.checks, current.changed_files = checks, files
                current.detail = redact(json.dumps(detail))
                current.ready_for_review, current.publication_sha = ready, publication_sha
                current.outcome = "publication_pending"
                session.get(Task, task.id).tested_sha = publication_sha
                transition(session, session.get(Task, task.id), "publishing", "Validated publication prepared")
            self.publish(task.id, attempt.id)
        except BaseException as exc:
            detail = redact(str(exc), [git.token])[:4000]
            with self.factory.begin() as session:
                current, item = session.get(Task, task.id), session.get(Attempt, attempt.id)
                if item.outcome != "publication_pending":
                    item.outcome = "timed_out" if isinstance(exc, TimeoutError) else "interrupted" if isinstance(
                        exc, (Stopped, KeyboardInterrupt, SystemExit)) else "failed"
                    item.detail = detail
                    if current.state in {"preparing", "running", "validating"}:
                        target = "queued" if current.attempt_count < self.settings.scheduler.max_attempts_per_task else "blocked"
                        transition(session, current, target, detail)
                notify(session, detail, level="error", repo_id=repo.id, task_id=task.id)
            log.warning("attempt_failed repo=%s task_id=%s attempt_id=%s reason=%s", repo.name, task.id, attempt.id, detail)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            revoke(self.factory, attempt.id)
            if runner:
                runner.stop()
            if services:
                try:
                    services.stop_services(attempt.id)
                except Exception:
                    log.warning("attempt_cleanup_pending attempt_id=%s", attempt.id)
            with self.factory.begin() as session:
                item = session.get(Attempt, attempt.id)
                item.finished_at = time.time()
                item.container_id = None

    def publish(self, task_id, attempt_id, draft=None):
        with self.factory() as session:
            task, attempt = session.get(Task, task_id), session.get(Attempt, attempt_id)
            repo = session.get(Repository, task.repo_id)
        if task.state != "publishing" or not repo.enabled:
            return
        client, git = self.github(repo.owner), self.git(repo)
        workspace = Path(attempt.workspace)
        if git.run(workspace, "rev-parse", "HEAD").strip() != attempt.publication_sha:
            raise ValueError("Publication HEAD differs from validated commit")
        if git.run(workspace, "status", "--porcelain").strip():
            raise ValueError("Publication workspace is dirty")
        if not verify_repository(self.settings, self.factory, repo.name, force=True, client=client).verified:
            raise ValueError("Publication blocked by ruleset verification")
        git.push(workspace, task.branch)
        detail = json.loads(attempt.detail)
        title = ("[Plans] " if task.kind == "plan_draft" else f"[Plan {task.plan_seq:02d}] ") + task.title
        pr = client.ensure_pr(repo.name, task.branch, repo.default_branch, title,
                              detail["body"], not attempt.ready_for_review)
        try:
            client.label_pr(repo.name, pr["number"])
        except Exception:
            pass
        with self.factory.begin() as session:
            current = session.get(Task, task.id)
            current.pr_number, current.pr_url = pr["number"], pr["html_url"]
            transition(session, current, "pr_open", "Published for operator review")
            session.get(Attempt, attempt.id).outcome = "published" if attempt.ready_for_review else "draft"

    def reconcile(self):
        with self.factory() as session:
            attempts = session.scalars(select(Attempt).where(
                (Attempt.finished_at.is_(None)) | (Attempt.outcome == "publication_pending"))).all()
        for attempt in attempts:
            revoke(self.factory, attempt.id)
            try:
                cleanup_attempt(attempt.id)
            except Exception:
                log.warning("cleanup_pending attempt_id=%s", attempt.id)
            if attempt.outcome == "publication_pending":
                try:
                    self.publish(attempt.task_id, attempt.id)
                except Exception as exc:
                    log.warning("publication_retry attempt_id=%s reason=%s", attempt.id, redact(str(exc)))
                continue
            with self.factory.begin() as session:
                item, task = session.get(Attempt, attempt.id), session.get(Task, attempt.task_id)
                item.outcome, item.finished_at, item.container_id = "interrupted", time.time(), None
                if task.state in {"preparing", "running", "validating"}:
                    transition(session, task, "queued" if task.attempt_count < self.settings.scheduler.max_attempts_per_task
                               else "blocked", "Controller interrupted")

    def poll_prs(self):
        with self.factory() as session:
            tasks = session.scalars(select(Task).where(Task.state.in_(["pr_open", "changes_requested"]))).all()
        for task in tasks:
            with self.factory() as session:
                repo = session.get(Repository, task.repo_id)
            client = self.github(repo.owner)
            pr = client.pull(repo.name, task.pr_number)
            if pr.get("merged_at") or pr["state"] == "closed":
                with self.factory.begin() as session:
                    transition(session, session.get(Task, task.id),
                               "merged" if pr.get("merged_at") else "closed_unmerged", "GitHub PR state changed")
                    if pr.get("merged_at"):
                        session.get(Repository, repo.id).last_plans_scan_sha = None
                if pr.get("merged_at") and self.settings.github.delete_merged_branches:
                    client.delete_branch(repo.name, task.branch)

    def tick(self):
        if not self.acquire():
            return
        with self.keep_lease():
            self.reconcile()
            self.poll_prs()
            self.discover()
            selected = self.claim()
            if selected:
                self.execute(*selected)
            self.heartbeat(force=True)

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                log.error("controller_tick_failed reason=%s", redact(str(exc)))
            time.sleep(self.settings.scheduler.tick_seconds)
