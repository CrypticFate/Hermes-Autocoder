import re
from datetime import timezone

from pydantic import BaseModel, Field
from sqlalchemy import select

from autocoder.github import for_owner
from autocoder.models import Repository, utcnow
from autocoder.notifications import notify
from autocoder.redaction import redact


class RulesetStatus(BaseModel):
    verified: bool
    repo: str
    problems: list[str] = Field(default_factory=list)
    rules: list[dict] = Field(default_factory=list)
    permission: str = "unknown"


class OnboardingResult(BaseModel):
    accepted: bool
    repo: str
    problems: list[str] = Field(default_factory=list)


def normalize_repository(url, owners):
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)?"
                         r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_][A-Za-z0-9_.-]*?)(?:\.git)?", url)
    if not match or match[2] in {".", ".."}:
        raise ValueError("Use OWNER/REPO or a github.com HTTPS/SSH repository URL")
    if match[1].lower() not in {o.lower() for o in owners}:
        raise ValueError("Repository owner is not configured")
    return "/".join(match.groups())


def inspect_repository(settings, client, repo):
    problems, rules, permission, metadata = [], [], "unknown", None
    try:
        client.verify_identity()
        metadata = client.repository(repo)
        if metadata.get("archived"):
            problems.append("Repository is archived; unarchive it first")
        permission = client.permission(repo, settings.github.bot_login).get("permission", "unknown")
        if permission != "write":
            problems.append("Give the bot exactly Write access, not read, maintain or admin")
        branch = metadata.get("default_branch", "main")
        client.branch(repo, branch)
        rules = client.branch_rules(repo, branch)
        required = settings.github.require_ruleset
        pr_rules = [r.get("parameters", {}) for r in rules if r.get("type") == "pull_request"]
        if not any(r.get("required_approving_review_count", 0) >= required.min_approvals
                   and (not required.require_last_push_approval or r.get("require_last_push_approval") is True)
                   for r in pr_rules):
            problems.append("Create an active default-branch repository ruleset requiring approvals and "
                            "last-push approval; classic branch protection is not accepted")
        types = {r.get("type") for r in rules}
        for enabled, rule in [(required.block_force_push, "non_fast_forward"),
                              (required.block_deletion, "deletion")]:
            if enabled and rule not in types:
                problems.append(f"Enable {rule} in the active repository ruleset")
    except Exception as exc:
        problems.append("Could not verify access, initial default-branch commit and ruleset: " + redact(str(exc)))
    return metadata, RulesetStatus(verified=not problems, repo=repo, problems=problems,
                                   rules=rules, permission=permission)


def verify_repository(settings, factory, repo, *, force=True, client=None):
    name = normalize_repository(repo, settings.owners)
    with factory() as session:
        row = session.scalar(select(Repository).where(Repository.name == name))
        if row is None:
            raise ValueError("Unknown managed repository")
        if not force and row.ruleset_verified_at and row.ruleset_report:
            age = (utcnow() - row.ruleset_verified_at.replace(tzinfo=timezone.utc)).total_seconds()
            if age < settings.github.ruleset_recheck_seconds and row.ruleset_report.get("verified"):
                return RulesetStatus.model_validate(row.ruleset_report)
    client = client or for_owner(settings, name.split("/")[0])
    _, result = inspect_repository(settings, client, name)
    with factory.begin() as session:
        row = session.scalar(select(Repository).where(Repository.name == name))
        row.ruleset_report, row.ruleset_verified_at = result.model_dump(), utcnow()
        if not result.verified:
            row.queue_state = "blocked_ruleset"
            notify(session, "; ".join(result.problems), level="action_required", repo_id=row.id,
                   key=f"ruleset:{row.id}:{';'.join(result.problems)}")
    return result


def register_repository(settings, factory, url, *, client=None):
    name = normalize_repository(url, settings.owners)
    client = client or for_owner(settings, name.split("/")[0])
    metadata, result = inspect_repository(settings, client, name)
    with factory.begin() as session:
        if result.verified:
            row = session.get(Repository, metadata["id"])
            if row is None:
                row = Repository(id=metadata["id"], name=name, owner=name.split("/")[0])
                session.add(row)
            row.default_branch = metadata["default_branch"]
            row.enabled, row.accessible, row.queue_state = True, True, "active"
            row.onboarded_at = row.ruleset_verified_at = utcnow()
            row.ruleset_report = result.model_dump()
        else:
            row = session.scalar(select(Repository).where(Repository.name == name))
            if row:
                row.enabled, row.queue_state = False, "blocked_ruleset"
            notify(session, name + ": " + "; ".join(result.problems), level="action_required")
    return OnboardingResult(accepted=result.verified, repo=name, problems=result.problems)
