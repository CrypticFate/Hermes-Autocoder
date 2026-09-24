from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from autocoder.config import Settings, load_settings, secret


def test_appendix_a_example_round_trip():
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["builder"]["image"] = "sha256:" + "a" * 64
    for service in data["services_allowlist"].values():
        service["image"] = service["image"].replace("<digest>", "b" * 64)
    settings = Settings.model_validate(data)
    assert settings.operator_login == "CrypticFate"
    assert settings.scheduler.sequential is True
    assert set(settings.services_allowlist) == {"postgres", "mysql", "redis", "mongo"}
    assert settings.profiles["python"].checks == ["ruff check .", "pytest -q"]
    assert Settings.model_validate(settings.model_dump()) == settings


@pytest.mark.parametrize("path,value,match", [
    (("operator_login",), "", "operator_login"),
    (("github", "bot_login"), "", "bot_login"),
    (("github", "bot_login"), "OWNER", "must differ"),
    (("builder", "image"), "worker:latest", "pinned"),
    (("builder", "image"), "worker@sha256:placeholder", "pinned"),
    (("scheduler", "sequential"), False, "parallel plans are not supported in v2"),
    (("scheduler", "sequential"), "false", "parallel plans are not supported in v2"),
    (("scheduler", "sequential"), 1, "parallel plans are not supported in v2"),
    (("model", "provider_url"), "http://provider.invalid", "HTTPS"),
    (("model", "input_usd_per_mtok"), "NaN", "finite"),
    (("budgets", "daily_usd"), "Infinity", "finite"),
    (("github", "mode"), "app", "App mode requires"),
    (("github", "commit_email"), "not-an-email", "email address"),
    (("github", "commit_name"), "name\nheader", "control characters"),
])
def test_invalid_configuration(settings, path, value, match):
    data = deepcopy(settings.model_dump())
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match=match):
        Settings.model_validate(data)


@pytest.mark.parametrize("field", ["operator_login", "github"])
def test_required_identity(settings, field):
    data = settings.model_dump()
    del data[field]
    with pytest.raises(ValidationError, match="Field required"):
        Settings.model_validate(data)


def test_bot_login_required(settings):
    data = settings.model_dump()
    del data["github"]["bot_login"]
    with pytest.raises(ValidationError, match="Field required"):
        Settings.model_validate(data)


@pytest.mark.parametrize("image", ["postgres:16", "sha256:" + "a" * 64, "postgres@sha256:bad"])
def test_service_requires_full_named_digest(settings, image):
    data = settings.model_dump()
    data["services_allowlist"] = {"postgres": {"image": image, "healthcheck": ["pg_isready"]}}
    with pytest.raises(ValidationError, match="sha256|pinned"):
        Settings.model_validate(data)


def test_legacy_config_rejected_without_mutating_file(tmp_path):
    path = tmp_path / "config.yaml"
    original = "owners: []\nmodel: old-model\n"
    path.write_text(original)
    with pytest.raises(ValidationError):
        load_settings(path)
    assert path.read_text() == original


@pytest.mark.parametrize("value", ["", "<GITHUB_BOT_TOKEN>", "PLACEHOLDER", "first\nsecond"])
def test_placeholder_or_invalid_secret_is_rejected(tmp_path, value):
    path = tmp_path / "github_bot"
    path.write_text(value)
    with pytest.raises(ValueError, match="placeholder"):
        secret(path)
