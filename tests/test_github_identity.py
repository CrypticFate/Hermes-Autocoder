from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from typer.testing import CliRunner

from autocoder.cli import app
from autocoder.github import GitHubClient, GitHubError, for_owner
from autocoder.gitops import Git


def api(settings, handler):
    return GitHubClient("unit-test-pat", config=settings.github,
                        client=httpx.Client(base_url="https://api.github.com",
                                            transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("login,allowed", [("owner-bot", True), ("OWNER-BOT", True), ("owner", False)])
def test_bot_pat_identity(settings, login, allowed):
    def handler(request):
        assert request.url.path == "/user"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["Authorization"] == "Bearer unit-test-pat"
        return httpx.Response(200, json={"login": login})
    client = api(settings, handler)
    if allowed:
        assert client.verify_identity() == login
    else:
        with pytest.raises(GitHubError, match="does not match"):
            client.verify_identity()


def test_owner_scope_and_no_generic_public_api(settings):
    with pytest.raises(GitHubError, match="not configured"):
        for_owner(settings, "someone-else")
    client = for_owner(settings, "owner")
    assert not hasattr(client, "request")
    assert not hasattr(client, "pages")
    assert not hasattr(client, "merge")
    assert not hasattr(client, "client")


def test_bot_discovers_collaborator_repository(settings):
    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "owner-bot"})
        assert "collaborator" in request.url.params["affiliation"]
        return httpx.Response(200, json=[{"owner": {"login": "owner"}, "size": 1}])
    assert len(api(settings, handler).repositories("owner")) == 1


def test_app_auth_refresh_and_identity(settings, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keyfile = tmp_path / "app.pem"
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    settings.github.mode = "app"
    settings.github.bot_login = "example-app[bot]"
    settings.github.app_id, settings.github.installation_id = 123, 456
    settings.github.private_key_secret = keyfile
    now = [int(datetime.now(timezone.utc).timestamp())]
    exchanges = []

    def handler(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if request.url.path.startswith("/app"):
            claims = jwt.decode(token, key.public_key(), algorithms=["RS256"],
                                options={"verify_iat": False})
            assert claims == {"iat": now[0] - 60, "exp": now[0] + 540, "iss": "123"}
            if request.url.path == "/app":
                return httpx.Response(200, json={"id": 123, "slug": "example-app"})
            assert request.method == "POST"
            assert request.url.path == "/app/installations/456/access_tokens"
            exchanges.append(token)
            return httpx.Response(201, json={"token": f"unit-installation-token-{len(exchanges)}",
                "expires_at": datetime.fromtimestamp(now[0] + 3600, timezone.utc).isoformat()})
        assert token == f"unit-installation-token-{len(exchanges)}"
        return httpx.Response(200, json={"full_name": "owner/repo"})

    client = GitHubClient(config=settings.github, clock=lambda: now[0], client=httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    assert client.verify_identity() == "example-app[bot]"
    client.repository("owner/repo")
    client.repository("owner/repo")
    assert len(exchanges) == 1
    now[0] += 3550
    client.repository("owner/repo")
    assert len(exchanges) == 2


@pytest.mark.parametrize("login,exit_code", [("owner-bot", 0), ("owner", 1)])
def test_doctor_verifies_bot_without_model_call(settings, tmp_path, monkeypatch, login, exit_code):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings.model_dump(mode="json")))
    client = api(settings, lambda _: httpx.Response(200, json={"login": login}))
    monkeypatch.setattr("autocoder.cli.for_owner", lambda *_: client)
    monkeypatch.setattr("docker.from_env", Mock(return_value=Mock()))
    runner = CliRunner()
    assert runner.invoke(app, ["--config", str(path), "init"]).exit_code == 0
    result = runner.invoke(app, ["--config", str(path), "doctor"])
    assert result.exit_code == exit_code, result.output
    assert ("identity verified" if exit_code == 0 else "does not match") in result.output
    assert "unit-test-pat" not in result.output


def test_commit_uses_configured_identity(settings, tmp_path):
    git = Git(commit_name=settings.github.commit_name, commit_email=settings.github.commit_email)
    git.run(tmp_path, "init")
    (tmp_path / "README.md").write_text("Fixture\n")
    git.commit(tmp_path, "Fixture commit")
    identity = git.run(tmp_path, "log", "-1", "--format=%an|%ae|%cn|%ce").strip()
    assert identity == "|".join([settings.github.commit_name, settings.github.commit_email] * 2)
