from unittest.mock import Mock

import pytest
from sqlalchemy import select

from autocoder.config import ServiceConfig
from autocoder.models import Sidecar
from autocoder.sidecars import SidecarManager


def test_service_lifecycle_and_password_stays_out_of_state(settings, factory, active):
    settings.services_allowlist["postgres"] = ServiceConfig(image="postgres@sha256:" + "a" * 64,
        env={"POSTGRES_PASSWORD": "{{password}}"}, healthcheck=["pg_isready"],
        expose_env={"DATABASE_URL": "postgresql://app:{{password}}@{{host}}:5432/app"})
    client, network, container = Mock(), Mock(id="net", labels={"autocoder.managed": "true"}), Mock()
    network.name = "att-attempt"
    container.id, container.labels = "container", {"autocoder.managed": "true"}
    container.exec_run.return_value.exit_code = 0
    client.containers.create.return_value = client.containers.get.return_value = container
    manager = SidecarManager(settings, factory, client, network)
    env = manager.start_services(active, ["postgres"])
    password = client.containers.create.call_args.kwargs["environment"]["POSTGRES_PASSWORD"]
    assert password in env["DATABASE_URL"] and "@postgres:" in env["DATABASE_URL"]
    with factory() as session:
        row = session.scalar(select(Sidecar))
        assert row.status == "healthy" and password not in str(vars(row))
    manager.stop_services(active)
    container.remove.assert_called_once_with(force=True)
    network.remove.assert_called_once()
    with factory() as session:
        assert session.scalar(select(Sidecar)).status == "removed"


def test_unknown_service(settings, factory):
    manager = SidecarManager(settings, factory, Mock(), Mock())
    with pytest.raises(ValueError, match="Unknown"):
        manager.start_services("attempt", ["unapproved"])
