import re
import uuid
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from autocoder.domain import fingerprint
from autocoder.github import for_owner
from autocoder.gitops import Git
from autocoder.models import Repository, Task
from autocoder.notifications import notify

FILENAME = re.compile(r"^plans/(?P<seq>\d{2,3})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$")


class PlanFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int
    slug: str
    path: str
    blob_sha: str
    title: str
    objective: str
    acceptance_criteria: list[str]
    context: str = ""
    services: list[str] = Field(default_factory=list)
    extra_checks: list[str] = Field(default_factory=list)


class InvalidPlan(BaseModel):
    path: str
    blob_sha: str = ""
    reason: str
    ignored: bool = False


def parse(path, text, blob_sha="", *, allowlist=()):
    match = FILENAME.fullmatch(path)
    if not match:
        raise ValueError("Filename must be plans/NN-lowercase-slug.md")
    if len(text.encode()) > 100_000:
        raise ValueError("Plan exceeds 100 KB")
    metadata = {}
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise ValueError("Frontmatter must end with ---")
        metadata, text = yaml.safe_load(parts[0][4:]) or {}, parts[1].lstrip("\r\n")
        if not isinstance(metadata, dict) or set(metadata) - {"title", "services", "checks"}:
            raise ValueError("Frontmatter allows title, services and checks only")
    heading = re.search(r"^# (.+)$", text, re.M)
    title = metadata.get("title", heading[1] if heading else "")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Add frontmatter title or a # heading")
    sections = {}
    for section in re.finditer(r"^## ([^\n]+)\n([\s\S]*?)(?=^## |\Z)", text, re.M):
        key = section[1].strip().lower()
        if key in sections:
            raise ValueError(f"Duplicate section: {key}")
        sections[key] = section[2].strip()
    objective = sections.get("objective", "")
    if not objective:
        raise ValueError("Add a non-empty ## Objective section")
    criteria = re.findall(r"^- (?:\[[ xX]\] )?(.+)$", sections.get("acceptance criteria", ""), re.M)
    criteria = [c.strip() for c in criteria if c.strip()]
    if not criteria:
        raise ValueError("Add ## Acceptance criteria with at least one non-empty list item")
    for key in ("services", "checks"):
        if not isinstance(metadata.get(key, []), list) or not all(
                isinstance(v, str) and v.strip() for v in metadata.get(key, [])):
            raise ValueError(f"{key} must be a list of non-empty strings")
    services = metadata.get("services", [])
    if set(services) - set(allowlist):
        raise ValueError("Unknown services: " + ", ".join(sorted(set(services) - set(allowlist))))
    context = "\n\n".join(sections[k] for k in ("context", "notes") if k in sections)
    return PlanFile(seq=int(match["seq"]), slug=match["slug"], path=path, blob_sha=blob_sha,
                    title=title.strip(), objective=objective, acceptance_criteria=criteria,
                    context=context, services=services, extra_checks=metadata.get("checks", []))


def discover(repo_checkout, head_sha, *, allowlist=()):
    git, results = Git(), []
    entries = git.run(repo_checkout, "ls-tree", "-r", "-z", head_sha, "--", "plans/")
    for entry in filter(None, entries.split("\0")):
        metadata, path = entry.split("\t", 1)
        mode, kind, sha = metadata.split()
        if not FILENAME.fullmatch(path):
            if Path(path).name != ".gitkeep":
                results.append(InvalidPlan(path=path, reason="Ignored non-plan filename", ignored=True))
            continue
        try:
            if mode != "100644" or kind != "blob":
                raise ValueError("Plans must be regular non-executable files")
            if int(git.run(repo_checkout, "cat-file", "-s", sha)) > 100_000:
                raise ValueError("Plan exceeds 100 KB")
            results.append(parse(path, git.run(repo_checkout, "cat-file", "blob", sha), sha, allowlist=allowlist))
        except (ValueError, yaml.YAMLError) as exc:
            results.append(InvalidPlan(path=path, blob_sha=sha, reason=str(exc)))
    numbers = [FILENAME.fullmatch(p.path)["seq"] for p in results if not isinstance(p, InvalidPlan) or not p.ignored]
    duplicates = {int(n) for n in numbers if sum(int(v) == int(n) for v in numbers) > 1}
    if duplicates:
        results = [InvalidPlan(path=p.path, blob_sha=p.blob_sha,
                    reason="Duplicate sequence numbers: " + str(sorted(duplicates)))
                   if FILENAME.fullmatch(p.path) and int(FILENAME.fullmatch(p.path)["seq"]) in duplicates else p
                   for p in results]
    return results


def reconcile(session, repo, plans):
    from autocoder.domain import transition
    existing = {t.plan_path: t for t in session.scalars(select(Task).where(
        Task.repo_id == repo.id, Task.kind == "implementation", Task.plan_path.is_not(None)))}
    paths, invalid = set(), False
    for plan in plans:
        if isinstance(plan, InvalidPlan) and plan.ignored:
            notify(session, f"Ignored {plan.path}", repo_id=repo.id, key=f"ignored:{repo.id}:{plan.path}")
            continue
        paths.add(plan.path)
        task = existing.get(plan.path)
        if task and (task.attempt_count > 0 or task.state not in {"pending", "invalid", "cancelled"}):
            if task.plan_blob_sha != plan.blob_sha:
                notify(session, f"{plan.path} changed after work started; add a new numbered plan instead",
                       repo_id=repo.id, key=f"changed:{task.id}:{plan.blob_sha}")
            continue
        if task is None:
            match = FILENAME.fullmatch(plan.path)
            task = Task(repo_id=repo.id, plan_path=plan.path, plan_seq=int(match["seq"]),
                        plan_slug=match["slug"], fingerprint=fingerprint(plan.path), source="plan",
                        state="pending", title=plan.path, objective="Invalid plan")
            session.add(task)
        task.plan_blob_sha = plan.blob_sha
        if isinstance(plan, InvalidPlan):
            invalid = True
            task.invalid_reason = plan.reason
            transition(session, task, "invalid", plan.reason)
            notify(session, f"{plan.path}: {plan.reason}", level="action_required", repo_id=repo.id,
                   key=f"invalid:{repo.id}:{plan.path}:{plan.blob_sha}")
        else:
            task.title = task.plan_title = plan.title
            task.objective, task.acceptance, task.evidence = plan.objective, plan.acceptance_criteria, plan.context
            task.services, task.extra_checks = plan.services, plan.extra_checks
            task.report_path = plan.path.replace("plans/", "report/", 1)
            task.invalid_reason = None
            if task.state in {"invalid", "cancelled"}:
                transition(session, task, "pending", "Plan corrected on default branch")
    for path, task in existing.items():
        if path not in paths and task.attempt_count == 0 and task.state in {"pending", "invalid"}:
            transition(session, task, "cancelled", "Plan removed before start")
    if invalid:
        repo.queue_state = "blocked_invalid_plans"
    elif repo.queue_state == "blocked_invalid_plans":
        repo.queue_state = "active"


def request_plan_draft(factory, repo, goal="", *, explicit=True):
    with factory.begin() as session:
        row = session.scalar(select(Repository).where(Repository.name == repo))
        if row is None:
            raise ValueError("Unknown managed repository")
        drafts = session.scalars(select(Task).where(Task.repo_id == row.id, Task.kind == "plan_draft")).all()
        for task in drafts:
            if task.state not in {"merged", "cancelled", "skipped", "closed_unmerged"}:
                return task.id
            if not explicit and task.state == "closed_unmerged":
                return None
        task = Task(repo_id=row.id, kind="plan_draft", state="pending", title="Draft implementation plans",
                    objective=goal or "Propose focused numbered plans based on repository needs",
                    fingerprint=fingerprint("draft:" + uuid.uuid4().hex), source="goal")
        session.add(task)
        session.flush()
        return task.id


def render_plan(seq, title, objective, criteria, services=(), context=""):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:70].rstrip("-") or "plan"
    path = f"plans/{seq:02d}-{slug}.md"
    text = "---\n" + yaml.safe_dump({"title": title, "services": list(services)}) + "---\n\n"
    text += "## Objective\n" + objective + "\n\n## Acceptance criteria\n"
    text += "\n".join("- [ ] " + item.replace("\n", " ") for item in criteria)
    text += "\n\n## Context\n" + context + "\n"
    return path, text


def scan_repository(settings, factory, repo, *, force=False, client=None, git=None):
    client = client or for_owner(settings, repo.split("/")[0])
    with factory() as session:
        row = session.scalar(select(Repository).where(Repository.name == repo))
        if row is None or not row.enabled:
            raise ValueError("Repository is not enabled")
        repository_id, base = row.id, row.default_branch
    checkout = settings.data_dir / "intake" / str(repository_id)
    git = git or Git(client.access_token())
    head = git.prepare(checkout, repo, base, "agent/plans-intake")
    with factory.begin() as session:
        row = session.get(Repository, repository_id)
        if not force and row.last_plans_scan_sha == head:
            return []
        plans = discover(checkout, head, allowlist=settings.services_allowlist)
        reconcile(session, row, plans)
        row.last_plans_scan_sha = head
    if not any(isinstance(p, PlanFile) or not p.ignored for p in plans):
        request_plan_draft(factory, repo, explicit=False)
    return plans


def add_plan(settings, factory, repo, title, objective, criteria, services=(), context=""):
    from autocoder.db import locked
    if not title.strip() or not objective.strip() or not criteria:
        raise ValueError("Title, objective and acceptance criteria are required")
    with locked(factory) as (session, _):
        row = session.scalar(select(Repository).where(Repository.name == repo))
        if row is None or not row.enabled:
            raise ValueError("Unknown or disabled managed repository")
        pending = session.scalar(select(Task).where(Task.repo_id == row.id, Task.kind == "plan_draft",
            Task.state.not_in(["merged", "closed_unmerged", "cancelled", "skipped"])))
        if pending:
            raise ValueError("Finish the open plan proposal before adding another plan")
        sequences = session.scalars(select(Task.plan_seq).where(Task.repo_id == row.id)).all()
        seq = max([n for n in sequences if n is not None] or [0]) + 1
        if seq > 999:
            raise ValueError("Plan sequence limit reached")
        path, text = render_plan(seq, title, objective, criteria, services, context)
        parse(path, text, allowlist=settings.services_allowlist)
        task = Task(repo_id=row.id, kind="plan_draft", state="pending", title=title, objective=objective,
                    fingerprint=fingerprint("add-plan:" + uuid.uuid4().hex), source="operator",
                    evidence=__import__("json").dumps({path: text}))
        session.add(task)
        session.flush()
        return task.id
