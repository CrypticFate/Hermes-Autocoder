"""operator_pat mode: the operator's own token instead of a bot account (opt-in)."""
import pytest
from helpers import PLAN, FakeDocker, FixtureRunner, LocalGit, github_double, make_remote
from pydantic import ValidationError
from sqlalchemy import select

from autocoder.config import Settings
from autocoder.controller import Controller
from autocoder.github import SYSTEM_MARKER
from autocoder.models import Feedback, Task
from autocoder.onboarding import register_repository


def operator_settings(settings, **ruleset):
    data = settings.model_dump()
    data["github"].update(mode="operator_pat", bot_login="owner")
    data["github"]["require_ruleset"].update(ruleset)
    return Settings.model_validate(data)


def test_config_rules(settings):
    assert operator_settings(settings, min_approvals=0, require_last_push_approval=False).github.mode == "operator_pat"
    data = settings.model_dump()
    data["github"]["mode"] = "operator_pat"  # bot_login still "owner-bot"
    with pytest.raises(ValidationError, match="must equal operator_login"):
        Settings.model_validate(data)
    data = settings.model_dump()
    data["github"]["require_ruleset"]["min_approvals"] = 0
    with pytest.raises(ValidationError, match="only in operator_pat"):
        Settings.model_validate(data)
    data = settings.model_dump()
    data["github"]["bot_login"] = "owner"
    with pytest.raises(ValidationError, match="must differ"):
        Settings.model_validate(data)


@pytest.mark.parametrize("permission,accepted", [("admin", True), ("maintain", True), ("write", True),
                                                 ("read", False)])
def test_owner_permissions_accepted_but_pull_request_rule_still_required(settings, factory, permission,
                                                                          accepted):
    settings = operator_settings(settings, min_approvals=0, require_last_push_approval=False)
    client = github_double()
    client.permission.return_value = {"permission": permission}
    client.branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        {"type": "deletion"}, {"type": "non_fast_forward"}]
    assert register_repository(settings, factory, "owner/repo", client=client).accepted is accepted
    client.permission.return_value = {"permission": "admin"}
    client.branch_rules.return_value = []
    assert not register_repository(settings, factory, "owner/repo", client=client).accepted


def test_full_flow_with_operator_token(settings, factory, tmp_path):
    settings = operator_settings(settings, min_approvals=0, require_last_push_approval=False)
    settings.profiles["python"].checks = ["test -f README.md"]
    settings.repository_profiles = {"owner/repo": "python"}
    settings.scheduler.feedback_debounce_seconds = 0
    remote, _, _ = make_remote(tmp_path, {"README.md": "x\n", "report/.gitkeep": "",
        "plans/01-first-step.md": PLAN.format(title="First step", objective="Build", criterion="Works")})
    github = github_double()
    github.permission.return_value = {"permission": "admin"}
    github.branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        {"type": "deletion"}, {"type": "non_fast_forward"}]
    pushes = []
    FixtureRunner.contexts, FixtureRunner.behaviour = [], None
    ctl = Controller(settings, factory, FixtureRunner, lambda *_: github, docker_client=FakeDocker(),
                     git_factory=lambda _repo: LocalGit(remote, pushes))
    assert ctl.acquire()
    ctl.discover()
    ctl.execute(*ctl.claim())
    assert pushes == ["agent/01-first-step"]
    old = "2020-01-01T00:00:00Z"
    github.pull.return_value = {"number": 1, "state": "open", "head": {"sha": "h"}}
    github.issue_comments.return_value = [
        # Your own comment (same account as the token) steers the repair...
        {"id": 1, "user": {"login": "owner", "type": "User"}, "created_at": old, "body": "Rename it please"},
        # ...but the gatekeeper's own comments, also written with your token, are never feedback.
        {"id": 2, "user": {"login": "owner", "type": "User"}, "created_at": old,
         "body": "Superseded by #9.\n\n" + SYSTEM_MARKER}]
    ctl.poll_prs(force=True)
    with factory() as session:
        assert [f.github_id for f in session.scalars(select(Feedback))] == ["1"]
        assert session.scalar(select(Task)).state == "queued"
    ctl.execute(*ctl.claim())
    assert "Rename it please" in FixtureRunner.contexts[-2].model_dump_json()
    github.pull.return_value = {"number": 1, "state": "closed", "merged_at": old}
    ctl.poll_prs(force=True)
    with factory() as session:
        assert session.scalar(select(Task)).state == "merged"
    assert not any("merge" in call[0] for call in github.method_calls)


def test_gatekeeper_comments_carry_marker():
    import json

    import httpx

    from autocoder.github import GitHubClient
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={})
    client = GitHubClient("unit-test-operator-token-value", client=httpx.Client(base_url="https://api.github.com",
                                                   transport=httpx.MockTransport(handler)))
    client.comment_pr("owner/repo", 1, "Superseded by #2")
    assert sent[0]["body"].endswith(SYSTEM_MARKER)
