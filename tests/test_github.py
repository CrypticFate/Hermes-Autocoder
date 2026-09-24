import json

import httpx
import pytest

from autocoder.github import GitHub, GitHubError


def client(handler):
    return GitHub("fake", httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)),
                  sleep=lambda _: None)


def test_pagination_and_owner_filter(settings):
    pages = []
    def handle(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "owner"})
        page = int(request.url.params["page"])
        pages.append(page)
        rows = [{"id": i, "owner": {"login": "owner"}} for i in range(100)] if page == 1 else [
            {"id": 101, "owner": {"login": "someone-else"}}]
        return httpx.Response(200, json=rows)
    assert len(client(handle).repositories(settings.owners[0])) == 100
    assert pages == [1, 2]


def test_existing_pr_is_updated_not_duplicated():
    methods = []
    def handle(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[{"number": 7, "state": "open"}])
        assert json.loads(request.content)["body"] == "report"
        return httpx.Response(200, json={"number": 7, "state": "open"})
    pr = client(handle).ensure_pr("owner/repo", "agent/task", "main", "fix", "report", False)
    assert pr["number"] == 7
    assert methods == ["GET", "PATCH"]


def test_rate_limit_is_bounded():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "3600"})
    with pytest.raises(GitHubError, match="rate limited"):
        client(handle).authenticated_user()
    assert len(calls) == 1


def test_write_transport_failure_is_not_blindly_retried():
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")
    with pytest.raises(GitHubError, match="reconciliation"):
        client(handle).ready_pr("test-node")
    assert len(calls) == 1


def test_discovery_accepts_recent_commit_with_stale_size(settings):
    def handle(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "owner"})
        if request.url.path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "main"}])
        return httpx.Response(200, json=[{
            "owner": {"login": "owner"}, "full_name": "owner/pilot", "size": 0}])
    assert client(handle).repositories(settings.owners[0])[0]["size"] == 1
