import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

from autocoder import feedback
from autocoder.budget import issue_capability, revoke
from autocoder.config import Profile
from autocoder.contracts import TaskContext
from autocoder.db import locked
from autocoder.github import for_owner
from autocoder.gitops import Git
from autocoder.models import Attempt, Repository, Sidecar, Task, uid
from autocoder.networks import create_attempt_network, require_managed
from autocoder.notifications import notify
from autocoder.onboarding import register_repository, verify_repository
from autocoder.plans import FILENAME, parse, scan_repository
from autocoder.redaction import install_log_redaction, redact
from autocoder.reports import pr_body, render_report
from autocoder.scheduler import claim, transition
from autocoder.security import safe_write
from autocoder.sidecars import SidecarManager
from autocoder.worker import DockerRunner, cleanup_attempt, runtime_profile

log = logging.getLogger(__name__)
PLAN_TEMPLATE = """---
title: <short imperative title>
services: []          # optional, allowlisted names only
checks: []            # optional extra shell checks
---

# <title>

## Objective
<one paragraph>

## Acceptance criteria
- [ ] <observable, testable criterion>

## Context
<details the builder needs>

## Notes
<optional>
"""


class Stopped(RuntimeError):
    pass


class Controller:
    def __init__(self, settings, factory, runner_factory=DockerRunner, github_factory=for_owner,
                 docker_client=None, git_factory=None, clock=time.time):
        install_log_redaction()
        self.settings, self.factory = settings, factory
        self.runner_factory, self.github_factory = runner_factory, github_factory
        self.git_factory, self.clock = git_factory, clock
        self.identity, self.last_heartbeat = uid(), 0
        self.docker_client = docker_client

    # ----------------------------------------------------------------- plumbing
    def github(self, owner):
        return self.github_factory(self.settings, owner)

    def git(self, repo):
        if self.git_factory:
            return self.git_factory(repo)
        client = self.github(repo.owner)
        client.verify_identity()
        return Git(client.access_token(), commit_name=self.settings.github.commit_name,
                   commit_email=self.settings.github.commit_email)

    def docker(self):
        if self.docker_client is None:
            import docker
            self.docker_client = docker.from_env()
        return self.docker_client

    def acquire(self):
        with locked(self.factory) as (_, control):
            now = time.time()
            if control.lease_owner not in (None, self.identity) and control.lease_until > now:
                return False
            control.lease_owner, control.lease_until = self.identity, now + self.settings.lease_seconds
            control.heartbeat = now
            return True

    def release(self):
        with locked(self.factory) as (_, control):
            if control.lease_owner == self.identity:
                control.lease_owner, control.lease_until = None, 0

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
                if (control.paused or task.state in {"cancelled", "skipped"} or not repo.enabled
                        or repo.queue_state != "active"):
                    raise Stopped("Run paused, cancelled, or repository disabled")
                repo.lease_until = control.lease_until
        self.last_heartbeat = now
        if workspace:
            total = 0
            for directory, _, files in os.walk(workspace, followlinks=False):
                for name in files:
                    try:
                        total += (Path(directory) / name).lstat().st_size
                    except FileNotFoundError:
                        pass
            if total > self.settings.max_workspace_bytes:
                raise Stopped("Workspace size limit exceeded")
            if shutil.disk_usage(self.settings.data_dir).free < self.settings.minimum_free_bytes:
                raise Stopped("Insufficient free disk space")

    # ---------------------------------------------------------------- discovery
    def discover(self):
        if self.settings.repositories.auto_enroll:
            for owner in self.settings.owners:
                for metadata in self.github(owner).repositories(owner):
                    name = metadata["full_name"]
                    if (name in self.settings.excluded_repositories or name == self.settings.self_repository
                            or metadata.get("archived") or (metadata.get("fork") and not self.settings.include_forks)):
                        continue
                    with self.factory() as session:
                        known = session.get(Repository, metadata["id"])
                    if known is None:
                        register_repository(self.settings, self.factory, name, client=self.github(owner))
        with self.factory() as session:
            repos = session.scalars(select(Repository).where(Repository.enabled.is_(True))).all()
        for repo in repos:
            try:
                verified = verify_repository(self.settings, self.factory, repo.name, force=False,
                                             client=self.github(repo.owner))
                if verified.verified and repo.queue_state not in {"paused", "blocked_ruleset"}:
                    scan_repository(self.settings, self.factory, repo.name, client=self.github(repo.owner),
                                    git=self.git(repo) if self.git_factory else None)
            except Exception as exc:
                log.warning("repository_refresh_failed repo=%s reason=%s", repo.name, redact(str(exc)))

    def claim(self):
        return claim(self.factory, self.settings, self.identity)

    def context(self, task, attempt, prompt="", mode=None, commands=(), service_env=(), repair=None):
        return TaskContext(task_id=task.id, attempt_id=attempt.id, kind=task.kind,
            mode=mode or ("plan" if task.kind == "plan_draft" else "implement"), prompt=prompt,
            plan_path=task.plan_path or "", report_path=task.report_path or "",
            title=task.title, objective=task.objective, criteria=task.acceptance,
            context=task.evidence if task.kind == "implementation" else "",
            services=task.services, extra_checks=task.extra_checks,
            service_env_names=sorted(service_env), checks=list(commands), branch=task.branch or "",
            base_sha=task.base_sha or "", model=self.settings.model.model,
            max_iterations=self.settings.builder.hermes_max_iterations,
            max_output_tokens=self.settings.model.max_output_tokens,
            timeout_seconds=self.settings.builder.deadline_seconds, repair=repair,
            operator_login=self.settings.operator_login)

    # ---------------------------------------------------------------- execution
    def execute(self, task_id, attempt_id):
        with self.factory() as session:
            task, attempt = session.get(Task, task_id), session.get(Attempt, attempt_id)
            repo = session.get(Repository, task.repo_id)
        workspace = self.settings.data_dir / "workspaces" / task.id / attempt.id
        run_dir = self.settings.data_dir / "runs" / attempt.id
        run_dir.mkdir(parents=True, exist_ok=True)
        git, cleanup = None, []
        try:
            git = self.git(repo)
            self.heartbeat(task.id, force=True)
            if not verify_repository(self.settings, self.factory, repo.name,
                                     client=self.github(repo.owner)).verified:
                raise Stopped("Repository protection no longer verified")
            base = git.prepare(workspace, repo.name, repo.default_branch, task.branch)
            start_head = git.run(workspace, "rev-parse", "HEAD").strip()
            task.base_sha = task.base_sha or base
            with self.factory.begin() as session:
                session.get(Task, task.id).base_sha = task.base_sha
                session.get(Attempt, attempt.id).workspace = str(workspace)
                transition(session, session.get(Task, task.id), "running", "Workspace prepared")
            if task.kind == "plan_draft" and task.source == "operator":
                detail, ready, checks, files = self._operator_plan(task, workspace, git, start_head)
            elif task.kind == "plan_draft":
                detail, ready, checks, files = self._draft_plans(task, attempt, repo, workspace, run_dir,
                                                                 git, start_head, cleanup)
            else:
                detail, ready, checks, files = self._implement(task, attempt, repo, workspace, run_dir,
                                                               git, start_head, cleanup)
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
            self._fail(task, attempt, repo, exc, git)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            revoke(self.factory, attempt.id)
            for action in reversed(cleanup):
                try:
                    action()
                except Exception:
                    log.warning("attempt_cleanup_pending attempt_id=%s", attempt.id)
            with self.factory.begin() as session:
                item = session.get(Attempt, attempt.id)
                item.finished_at = item.finished_at or time.time()
                item.container_id = None

    def _fail(self, task, attempt, repo, exc, git):
        detail = redact(str(exc) or type(exc).__name__, [git.token] if git else [])[:4000]
        with self.factory.begin() as session:
            current, item = session.get(Task, task.id), session.get(Attempt, attempt.id)
            if item.outcome == "publication_pending":
                notify(session, f"Publication for {current.title} is pending: {detail}", level="error",
                       repo_id=repo.id, task_id=task.id, key=f"publication:{attempt.id}")
            else:
                item.outcome = ("timed_out" if isinstance(exc, TimeoutError) else "interrupted" if isinstance(
                    exc, (Stopped, KeyboardInterrupt, SystemExit)) else "failed")
                item.detail = detail
                if current.state in {"preparing", "running", "validating"}:
                    exhausted = current.attempt_count >= self.settings.scheduler.max_attempts_per_task
                    transition(session, current, "blocked" if exhausted else "queued", detail)
                    if exhausted:
                        notify(session, f"Plan {current.plan_seq or ''} ({current.title}) is blocked after "
                               f"{current.attempt_count} attempts: {detail}. Say 'retry plan' after fixing it.",
                               level="action_required", repo_id=repo.id, task_id=task.id,
                               key=f"blocked:{task.id}:{attempt.id}")
                notify(session, f"Attempt failed for {current.title}: {detail}", level="error",
                       repo_id=repo.id, task_id=task.id)
        log.warning("attempt_failed repo=%s task_id=%s attempt_id=%s plan=%s reason=%s", repo.name, task.id,
                    attempt.id, task.plan_path, detail)

    def _operator_plan(self, task, workspace, git, start_head):
        """add_plan: the gatekeeper renders the plan itself; no builder runs."""
        files = json.loads(task.evidence)
        plans = [parse(path, text, allowlist=self.settings.services_allowlist) for path, text in files.items()]
        for path, text in files.items():
            safe_write(workspace, path, text)
        if not (workspace / "report").exists():
            safe_write(workspace, "report/.gitkeep", "")
        changed, problems = git.gate(workspace, start_head, [git.token], kind="plan_draft")
        if problems or not set(files) <= set(changed) or set(changed) - set(files) - {"report/.gitkeep"}:
            raise ValueError("; ".join(problems) or "Plan publication contains unexpected files")
        git.commit(workspace, "plans: add " + ", ".join(sorted(p.path.removeprefix("plans/") for p in plans)))
        return {"body": self._plan_body(plans, task), "attention": [], "plans": [p.path for p in plans]}, \
            True, [], changed

    def _plan_body(self, plans, task):
        from autocoder.reports import plan_pr_body
        return plan_pr_body(plans, self.settings.operator_login,
                            "" if task.source == "operator" else task.objective)

    def _draft_plans(self, task, attempt, repo, workspace, run_dir, git, start_head, cleanup):
        profile = Profile(command_seconds=self.settings.builder.deadline_seconds)
        client = self.docker()
        network = create_attempt_network(client, attempt.id)
        services = SidecarManager(self.settings, self.factory, client, network)
        cleanup.append(lambda: services.stop_services(attempt.id))
        runner = self._runner(workspace, run_dir / "builder", profile, task, network, {}, client)
        cleanup.append(runner.stop)
        self._record_container(attempt.id, runner.start(attempt.id))
        runner.setup()
        tree = git.run(workspace, "ls-files")[:6000]
        prompt = ("OPERATOR GOAL:\n" + (task.objective or "Propose focused next steps for this repository.")
                  + "\n\nREPOSITORY FILES (truncated):\n" + tree + "\n\nPLAN TEMPLATE (Appendix B):\n"
                  + PLAN_TEMPLATE)
        runner.token = issue_capability(self.factory, attempt.id, self.settings.builder.deadline_seconds)
        result = runner.run(self.context(task, attempt, prompt))
        runner.stop()
        revoke(self.factory, attempt.id)
        with self.factory.begin() as session:
            transition(session, session.get(Task, task.id), "validating", "Builder finished")
        files, problems = git.gate(workspace, start_head, [git.token], kind="plan_draft")
        if not files:
            problems.append("The builder did not write any plan files")
        if len(files) > 8 or any(not FILENAME.fullmatch(path) for path in files):
            problems.append("Plan drafts may only add 1 to 8 files named plans/NN-slug.md")
        plans = []
        for path in files:
            if FILENAME.fullmatch(path) and (workspace / path).is_file():
                try:
                    plans.append(parse(path, (workspace / path).read_text(),
                                       allowlist=self.settings.services_allowlist))
                except ValueError as exc:
                    problems.append(f"{path}: {exc}")
        if len({p.seq for p in plans}) != len(plans):
            problems.append("Draft plans reuse a sequence number")
        if problems:
            raise ValueError("; ".join(problems))
        git.commit(workspace, f"plans: propose {len(plans)} implementation plans")
        return {"body": self._plan_body(plans, task), "attention": [], "plans": [p.path for p in plans],
                "result": result.model_dump()}, True, [], files

    def _runner(self, workspace, run_dir, profile, task, network, service_env, client, check_only=False):
        runner = self.runner_factory(self.settings, workspace, run_dir, profile, "",
                                     lambda: self.heartbeat(task.id, workspace), client=client)
        runner.network, runner.service_env, runner.check_only = network, service_env, check_only
        return runner

    def _record_container(self, attempt_id, container_id):
        with self.factory.begin() as session:
            session.get(Attempt, attempt_id).container_id = container_id

    def _implement(self, task, attempt, repo, workspace, run_dir, git, start_head, cleanup):
        profile = runtime_profile(self.settings, repo.name, workspace)
        client = self.docker()
        network = create_attempt_network(client, attempt.id)
        services = SidecarManager(self.settings, self.factory, client, network)
        cleanup.append(lambda: services.stop_services(attempt.id))
        service_env = services.start_services(attempt.id, task.services)
        runner = self._runner(workspace, run_dir / "builder", profile, task, network, service_env, client)
        cleanup.append(runner.stop)
        self._record_container(attempt.id, runner.start(attempt.id))
        setup = runner.setup()
        commands = profile.checks + task.extra_checks
        baseline = [runner.command(command) for command in commands]
        for item in baseline:
            item["output"] = redact(item["output"])[-4096:]
        with self.factory.begin() as session:
            current = session.get(Attempt, attempt.id)
            current.setup, current.baseline = setup, baseline
            repair, feedback_ids = feedback.repair_context(session, session.get(Task, task.id))
            current.feedback_ids = feedback_ids
            if repair:
                current.mode = "repair"
        runner.token = issue_capability(self.factory, attempt.id, self.settings.builder.deadline_seconds)
        result = runner.run(self.context(task, attempt, commands=commands, service_env=service_env,
                                         repair=repair))
        runner.stop()
        revoke(self.factory, attempt.id)
        with self.factory.begin() as session:
            transition(session, session.get(Task, task.id), "validating", "Builder finished")
        files, problems = git.gate(workspace, start_head, [git.token], kind="implementation")
        if not files:
            problems.append("No implementation changes produced")
        if problems:
            raise ValueError("; ".join(problems))
        result.validate_criteria(task.acceptance)
        source_digest = git.tree_digest(workspace)
        checks = self._checks(task, attempt, workspace, run_dir, profile, commands, network, service_env, client,
                              cleanup)
        review = self._review(task, attempt, workspace, run_dir, profile, commands, network, service_env,
                              client, cleanup, git, start_head, checks)
        if git.tree_digest(workspace) != source_digest:
            raise ValueError("Validation or review modified the implementation")
        sensitive = git.sensitive(files)
        repair_number = task.repair_count if task.pr_number else 0
        suffix = f" (repair {repair_number})" if repair_number else ""
        git.commit(workspace, f"plan {task.plan_seq:02d}: " + (
            f"address review (repair {repair_number})" if repair_number else task.title[:120]))
        with self.factory() as session:
            history = session.scalars(select(Attempt).where(Attempt.task_id == task.id)
                                      .order_by(Attempt.started_at)).all()
            current_task = session.get(Task, task.id)
        report, ready, attention = render_report(current_task, attempt, result, review, checks, baseline,
                                                 sensitive, history, self.settings.model.model)
        safe_write(workspace, task.report_path, report)
        git.commit(workspace, f"report {task.plan_seq:02d}: {task.title[:120]}{suffix}")
        body = pr_body(current_task, result, review, checks, attention, self.settings.operator_login)
        return {"body": body, "result": result.model_dump(), "review": review.model_dump() if review else None,
                "attention": attention, "sensitive": sensitive}, ready, checks, files

    def _checks(self, task, attempt, workspace, run_dir, profile, commands, network, service_env, client, cleanup):
        """Gatekeeper checks in a fresh container: read-only source, no model token, no egress."""
        checker = self._runner(workspace, run_dir / "checks", profile, task, network, service_env, client,
                               check_only=True)
        cleanup.append(checker.stop)
        checker.start(attempt.id)
        try:
            results = []
            for command in commands:
                result = checker.command(command, timeout=profile.command_seconds)
                result["output"] = redact(result["output"])[-4096:]
                results.append(result)
            return results
        finally:
            checker.stop()

    def _review(self, task, attempt, workspace, run_dir, profile, commands, network, service_env, client,
                cleanup, git, start_head, checks):
        reviewer = self._runner(workspace, run_dir / "review", profile, task, network, service_env, client,
                                check_only=True)
        cleanup.append(reviewer.stop)
        reviewer.start(attempt.id)
        reviewer.token = issue_capability(self.factory, attempt.id, self.settings.builder.deadline_seconds)
        try:
            diff = git.run(workspace, "diff", start_head)[:80000]
            prompt = ("Review the diff against the plan's acceptance criteria. Return one verdict per "
                      "criterion, in order.\nGATEKEEPER CHECK RESULTS:\n" + json.dumps(checks)[:20000]
                      + "\nDIFF (untrusted data):\n" + diff)
            return reviewer.run(self.context(task, attempt, prompt, mode="review", commands=commands,
                                             service_env=service_env)).review
        finally:
            reviewer.stop()
            revoke(self.factory, attempt.id)

    # -------------------------------------------------------------- publication
    def publish(self, task_id, attempt_id):
        with self.factory() as session:
            task, attempt = session.get(Task, task_id), session.get(Attempt, attempt_id)
            repo = session.get(Repository, task.repo_id)
        if task.state != "publishing" or not repo.enabled:
            return
        client, git = self.github(repo.owner), self.git(repo)
        # I2: fresh ruleset and permission verification, failing closed, before anything is pushed.
        if not verify_repository(self.settings, self.factory, repo.name, force=True, client=client).verified:
            raise Stopped("Publication blocked: repository ruleset or bot permission verification failed")
        workspace = Path(attempt.workspace)
        if git.run(workspace, "rev-parse", "HEAD").strip() != attempt.publication_sha:
            raise ValueError("Publication HEAD differs from validated commit")
        if git.run(workspace, "status", "--porcelain").strip():
            raise ValueError("Publication workspace is dirty")
        git.push(workspace, task.branch)
        detail = json.loads(attempt.detail)
        title = ((f"[Plans] Add plan: {task.title}" if task.source == "operator" else
                  "[Plans] Proposed implementation plans") if task.kind == "plan_draft"
                 else f"[Plan {task.plan_seq:02d}] {task.title}")
        pr = client.ensure_pr(repo.name, task.branch, repo.default_branch, title[:250],
                              detail["body"], not attempt.ready_for_review)
        try:
            client.label_pr(repo.name, pr["number"])
        except Exception:
            log.info("label_failed repo=%s pr=%s", repo.name, pr["number"])
        superseded = task.superseded_pr_number
        if superseded and superseded != pr["number"]:
            try:
                client.comment_pr(repo.name, superseded, f"Superseded by #{pr['number']} after the default "
                                  "branch moved. This PR is closed without integrating it.")
                client.close_pr(repo.name, superseded)
            except Exception as exc:
                log.warning("supersede_close_failed repo=%s pr=%s reason=%s", repo.name, superseded,
                            redact(str(exc)))
        with self.factory.begin() as session:
            current = session.get(Task, task.id)
            current.pr_number, current.pr_url = pr["number"], pr["html_url"]
            current.superseded_pr_number, current.feedback = None, ""
            transition(session, current, "pr_open", "Published for operator review")
            item = session.get(Attempt, attempt.id)
            item.outcome = "published" if attempt.ready_for_review else "draft"
            feedback.mark_consumed(session, item.feedback_ids or [], item.id)
            state = "ready for review" if attempt.ready_for_review else "a draft that needs attention"
            notify(session, f"PR #{pr['number']} ({title}) is {state}: {pr['html_url']}", repo_id=repo.id,
                   task_id=task.id, key=f"published:{attempt.id}")

    # ------------------------------------------------------------ PR monitoring
    def poll_prs(self, force=False):
        with locked(self.factory) as (_, control):
            if not force and time.time() - control.polled_at < self.settings.scheduler.pr_poll_seconds:
                return
            control.polled_at = time.time()
        with self.factory() as session:
            tasks = session.scalars(select(Task).where(Task.state.in_(["pr_open", "changes_requested"]))).all()
        for task in tasks:
            try:
                self._poll_task(task)
            except Exception as exc:
                log.warning("pr_poll_failed task_id=%s reason=%s", task.id, redact(str(exc)))

    def _poll_task(self, task):
        with self.factory() as session:
            repo = session.get(Repository, task.repo_id)
        client = self.github(repo.owner)
        pr = client.pull(repo.name, task.pr_number)
        if pr.get("merged_at") or pr.get("merged"):
            with self.factory.begin() as session:
                transition(session, session.get(Task, task.id), "merged", "Operator merged the PR")
                session.get(Repository, repo.id).last_plans_scan_sha = None
                notify(session, f"PR #{task.pr_number} ({task.title}) was merged.", repo_id=repo.id,
                       task_id=task.id, key=f"merged:{task.id}:{task.pr_number}")
            if self.settings.github.delete_merged_branches and task.branch != repo.default_branch:
                try:
                    client.delete_branch(repo.name, task.branch)
                except Exception as exc:
                    log.info("branch_delete_failed repo=%s reason=%s", repo.name, redact(str(exc)))
            return
        if pr["state"] == "closed":
            with self.factory.begin() as session:
                transition(session, session.get(Task, task.id), "closed_unmerged", "Operator closed the PR")
            return
        if task.kind != "implementation":
            return
        feedback.collect(self.settings, self.factory, task.id, client, repo.name, pr)
        if pr.get("mergeable") is False and pr.get("mergeable_state") == "dirty":
            self._handle_conflict(task, repo, client, pr)
            return
        with self.factory.begin() as session:
            current = session.get(Task, task.id)
            rows = feedback.pending(session, current.id)
            if rows and current.state == "pr_open":
                transition(session, current, "changes_requested", f"{len(rows)} feedback items received")
            if current.state == "changes_requested" and feedback.debounce_elapsed(self.settings, rows,
                                                                                 self.clock()):
                self._queue_repair(session, current, "Operator feedback debounce elapsed")

    def _queue_repair(self, session, task, reason):
        if task.repair_count >= self.settings.scheduler.max_repairs_per_task:
            transition(session, task, "blocked", "Repair limit reached")
            notify(session, f"PR #{task.pr_number} for plan {task.plan_seq:02d} reached the repair limit "
                   f"({task.repair_count}). Review it yourself, or say 'retry plan {task.plan_seq}'.",
                   level="action_required", repo_id=task.repo_id, task_id=task.id,
                   key=f"repairs:{task.id}:{task.repair_count}")
            return
        task.repair_count += 1
        task.attempt_count = 0
        transition(session, task, "queued", reason)

    def _handle_conflict(self, task, repo, client, pr):
        """The default branch moved under an open PR. Force-push is blocked, so publish a new branch."""
        root = task.branch.split("-rb")[0] if "-rb" in task.branch else task.branch
        k = task.rebase_count + 1
        new_branch = f"{root}-rb{k}"
        git = self.git(repo)
        workspace = self.settings.data_dir / "rebase" / task.id / str(k)
        if workspace.exists():
            shutil.rmtree(workspace)
        ok, info = git.rebase(workspace, repo.name, repo.default_branch, task.branch)
        with self.factory.begin() as session:
            current = session.get(Task, task.id)
            current.rebase_count = k
            if not ok:
                current.superseded_pr_number, current.pr_number = current.pr_number, None
                current.pr_url, current.branch, current.base_sha = None, new_branch, None
                current.feedback = f"PR #{pr['number']} conflicts with {repo.default_branch}. {info}"
                if current.state == "pr_open":
                    transition(session, current, "changes_requested", "Merge conflict")
                self._queue_repair(session, current, "Merge conflict requires re-implementation")
                return
        if not verify_repository(self.settings, self.factory, repo.name, force=True, client=client).verified:
            raise Stopped("Publication blocked by repository protection")
        git.push(workspace, new_branch)
        title = f"[Plan {task.plan_seq:02d}] {task.title}"
        body = (pr.get("body") or "") + f"\n\nRebased onto `{repo.default_branch}`; supersedes #{pr['number']}."
        new = client.ensure_pr(repo.name, new_branch, repo.default_branch, title, body, bool(pr.get("draft")))
        try:
            client.label_pr(repo.name, new["number"])
        except Exception:
            pass
        client.comment_pr(repo.name, pr["number"], f"Superseded by #{new['number']} (rebased onto "
                          f"{repo.default_branch}). Closed without integrating.")
        client.close_pr(repo.name, pr["number"])
        with self.factory.begin() as session:
            current = session.get(Task, task.id)
            current.branch, current.pr_number, current.pr_url = new_branch, new["number"], new["html_url"]
            current.superseded_pr_number = None
            notify(session, f"PR #{pr['number']} was rebased onto {repo.default_branch} as PR #{new['number']}.",
                   repo_id=repo.id, task_id=task.id, key=f"rebased:{task.id}:{k}")

    # ----------------------------------------------------------------- recovery
    def reconcile(self):
        with self.factory() as session:
            attempts = session.scalars(select(Attempt).where(
                (Attempt.finished_at.is_(None)) | (Attempt.outcome == "publication_pending")
                | (Attempt.container_id.is_not(None)))).all()
        for attempt in attempts:
            revoke(self.factory, attempt.id)
            self._cleanup_attempt(attempt.id)
            if attempt.outcome == "publication_pending":
                try:
                    self.publish(attempt.task_id, attempt.id)
                except Exception as exc:
                    log.warning("publication_retry attempt_id=%s reason=%s", attempt.id, redact(str(exc)))
                continue
            with self.factory.begin() as session:
                item, task = session.get(Attempt, attempt.id), session.get(Task, attempt.task_id)
                if item.finished_at is None:
                    item.outcome, item.finished_at = "interrupted", time.time()
                    item.detail = item.detail or "Controller restarted during the attempt"
                item.container_id = None
                if task.state in {"preparing", "running", "validating"}:
                    exhausted = task.attempt_count >= self.settings.scheduler.max_attempts_per_task
                    transition(session, task, "blocked" if exhausted else "queued", "Controller interrupted")
        self._sweep_docker()

    def _cleanup_attempt(self, attempt_id):
        try:
            cleanup_attempt(self.docker(), attempt_id)
        except Exception:
            log.warning("cleanup_pending attempt_id=%s", attempt_id)
        with self.factory.begin() as session:
            for row in session.scalars(select(Sidecar).where(Sidecar.attempt_id == attempt_id,
                                                             Sidecar.status != "removed")):
                row.status, row.removed_at = "removed", time.time()

    def _sweep_docker(self):
        """Remove managed containers/networks whose attempt is finished or unknown."""
        try:
            client = self.docker()
            objects = [*client.containers.list(all=True, filters={"label": "autocoder.managed=true"}),
                       *client.networks.list(filters={"label": "autocoder.managed=true"})]
        except Exception:
            return
        with self.factory() as session:
            for obj in objects:
                attempt_id = (getattr(obj, "labels", None) or {}).get("autocoder.attempt")
                attempt = session.get(Attempt, attempt_id) if attempt_id else None
                if attempt is not None and attempt.finished_at is None:
                    continue
                try:
                    require_managed(obj)
                    obj.remove(force=True) if hasattr(obj, "stop") else obj.remove()
                except Exception:
                    log.warning("sweep_pending attempt_id=%s", attempt_id)

    # --------------------------------------------------------------------- loop
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
