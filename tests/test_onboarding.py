from unittest.mock import Mock

import pytest
from sqlalchemy import select

from autocoder.models import Repository
from autocoder.onboarding import normalize_repository, register_repository, verify_repository


def github_fixture():
    client = Mock()
    client.repository.return_value = {"id": 1, "default_branch": "main", "archived": False}
    client.permission.return_value = {"permission": "write"}
    client.branch.return_value = {"name": "main"}
    client.branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1,
                                                  "require_last_push_approval": True}},
        {"type": "deletion"}, {"type": "non_fast_forward"}]
    return client


@pytest.mark.parametrize("url", ["owner/repo", "https://github.com/owner/repo.git", "git@github.com:owner/repo.git"])
def test_repository_urls(url):
    assert normalize_repository(url, ["owner"]) == "owner/repo"


@pytest.mark.parametrize("url", ["http://github.com/owner/repo", "https://evil.invalid/owner/repo",
                                 "owner/../repo", "other/repo", "owner/repo?token=value"])
def test_bad_urls(url):
    with pytest.raises(ValueError):
        normalize_repository(url, ["owner"])


@pytest.mark.parametrize("failure", ["archived", "read", "admin", "maintain", "missing_branch",
                                     "no_rules", "no_approvals", "no_last_push", "no_deletion", "no_force"])
def test_onboarding_fails_closed(settings, factory, failure):
    client = github_fixture()
    if failure == "archived":
        client.repository.return_value["archived"] = True
    elif failure in {"read", "admin", "maintain"}:
        client.permission.return_value["permission"] = failure
    elif failure == "missing_branch":
        client.branch.side_effect = RuntimeError("HTTP 404")
    elif failure == "no_rules":
        client.branch_rules.return_value = []
    elif failure == "no_approvals":
        client.branch_rules.return_value[0]["parameters"]["required_approving_review_count"] = 0
    elif failure == "no_last_push":
        client.branch_rules.return_value[0]["parameters"]["require_last_push_approval"] = False
    elif failure == "no_deletion":
        client.branch_rules.return_value.pop(1)
    else:
        client.branch_rules.return_value.pop()
    assert not register_repository(settings, factory, "owner/repo", client=client).accepted
    with factory() as session:
        assert session.get(Repository, 1).enabled is False


def test_i2_recheck_detects_removed_rules(settings, factory):
    client = github_fixture()
    assert register_repository(settings, factory, "owner/repo", client=client).accepted
    client.branch_rules.return_value = []
    assert not verify_repository(settings, factory, "owner/repo", force=True, client=client).verified
    with factory() as session:
        assert session.scalar(select(Repository)).queue_state == "blocked_ruleset"
