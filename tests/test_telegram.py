"""Telegram chat for the concierge: operator-only access, safe override, model-free notifications."""
import importlib.util
import io
import json
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAFE = {"unauthorized_dm_behavior": "ignore"}


@pytest.mark.parametrize("env,ok", [
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "123456"}, True),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "123,456"}, True),
    ({}, True),
    ({"TELEGRAM_BOT_TOKEN": "x"}, False),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "@myname"}, False),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "*"}, False),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1", "TELEGRAM_ALLOW_ALL_USERS": "true"}, False),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1", "GATEWAY_ALLOW_ALL_USERS": "1"}, False),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1", "TELEGRAM_GROUP_ALLOWED_CHATS": "-100"}, False),
    ({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1", "TELEGRAM_ALLOW_ALL_USERS": "false"}, True),
])
def test_telegram_is_operator_only(env, ok):
    setup = load("concierge_setup", "docker/concierge/concierge_setup.py")
    assert (setup.telegram_problems(SAFE, env) == []) is ok


def test_pairing_for_unknown_users_is_disabled():
    setup = load("concierge_setup", "docker/concierge/concierge_setup.py")
    env = {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"}
    assert setup.telegram_problems({}, env)
    assert "unauthorized_dm_behavior: ignore" in (ROOT / "docker/concierge/config.yaml.tmpl").read_text()


def test_telegram_override_adds_only_egress_and_bot_token():
    base = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["concierge"]
    override = yaml.safe_load((ROOT / "compose.telegram.yaml").read_text())
    concierge = override["services"]["concierge"]
    assert set(concierge["secrets"]) - set(base["secrets"]) == {"telegram_bot_token"}
    assert not {"github_bot", "model_provider"} & set(concierge["secrets"])
    assert set(concierge["networks"]) == {"control", "concierge-egress"}
    assert "internal" not in override["networks"]["concierge-egress"]
    assert list(override["services"]) == ["concierge"]


def test_image_installs_telegram_adapter_and_entrypoint_checks_first():
    assert "--extra messaging" in (ROOT / "docker/concierge.Dockerfile").read_text()
    entry = (ROOT / "docker/concierge/entrypoint.sh").read_text()
    assert entry.index("concierge_setup.py selfcheck") < entry.index("exec hermes gateway run")
    assert entry.index("TELEGRAM_BOT_TOKEN=") < entry.index("concierge_setup.py selfcheck")


def test_notification_job_is_model_free_and_idempotent(tmp_path):
    setup = load("concierge_setup", "docker/concierge/concierge_setup.py")
    created = []

    class Jobs:
        def list_jobs(self, include_disabled=False):
            return created

        def remove_job(self, job_id):
            created[:] = [j for j in created if j["id"] != job_id]

        def create_job(self, **kwargs):
            created.append({"id": str(len(created)), **kwargs})
            return created[-1]
    for _ in range(2):
        setup.seed_notifications(tmp_path, "2m", Jobs())
    assert len(created) == 1
    job = created[0]
    assert job["no_agent"] is True and job["deliver"] == "telegram" and job["prompt"] is None
    assert (tmp_path / "scripts" / "autocoder_notify.py").is_file()


def test_notifier_prints_each_notification_once(settings, factory, tmp_path, monkeypatch, capsys):
    from autocoder.mcp_server import create_app
    notify = load("autocoder_notify", "docker/concierge/autocoder_notify.py")
    notify.STATE = tmp_path / "state.json"
    secret = tmp_path / "token"
    secret.write_text(TOKEN)
    real_path = Path
    monkeypatch.setattr(notify, "Path", lambda p: secret if p == "/run/secrets/mcp_concierge_token" else real_path(p))

    class Opened(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    with TestClient(create_app(settings, factory, TOKEN)) as client:
        def urlopen(request, timeout):
            response = client.post("/mcp", content=request.data, headers=dict(request.header_items()))
            assert response.status_code == 200
            return Opened(response.content)
        monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
        run_notifier(factory, notify, capsys)


def run_notifier(factory, notify, capsys):
    from autocoder.models import Notification
    with factory.begin() as session:
        session.add(Notification(level="action_required", message="PR #1 for plan 01 was closed."))
    notify.main()
    first = capsys.readouterr().out
    assert "[Action required] PR #1 for plan 01 was closed." in first
    notify.main()
    assert capsys.readouterr().out == ""
    assert json.loads(notify.STATE.read_text())["delivered"]
