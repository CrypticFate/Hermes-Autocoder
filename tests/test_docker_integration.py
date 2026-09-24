"""Docker acceptance (Phases 6, 7, 8, 14). Run with RUN_DOCKER_TESTS=1 and WORKER_IMAGE=<builder image id>."""
import os
import uuid

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="Set RUN_DOCKER_TESTS=1")]
POSTGRES = "postgres:16@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6"


@pytest.fixture
def client():
    import docker
    return docker.from_env()


def test_postgres_sidecar_reachable_then_fully_removed(settings, factory, active, client):
    """E2E-5 building block: a sidecar answers `select 1` on att-<id>, and nothing labeled remains."""
    from autocoder.config import ServiceConfig
    from autocoder.networks import create_attempt_network
    from autocoder.sidecars import SidecarManager
    settings.services_allowlist["postgres"] = ServiceConfig(
        image=POSTGRES, tmpfs="/var/lib/postgresql/data",
        env={"POSTGRES_USER": "app", "POSTGRES_DB": "app", "POSTGRES_PASSWORD": "{{password}}"},
        healthcheck=["pg_isready", "-U", "app", "-d", "app"],
        expose_env={"DATABASE_URL": "postgresql://app:{{password}}@{{host}}:5432/app"})
    network = create_attempt_network(client, active)
    manager = SidecarManager(settings, factory, client, network)
    try:
        env = manager.start_services(active, ["postgres"])
        probe = client.containers.run(POSTGRES, ["psql", env["DATABASE_URL"], "-c", "select 1"], network=network.name,
                                      remove=True, labels={"autocoder.managed": "true", "autocoder.attempt": active})
        assert b"1 row" in probe
    finally:
        manager.stop_services(active)
    assert client.containers.list(all=True, filters={"label": f"autocoder.attempt={active}"}) == []
    assert client.networks.list(filters={"label": f"autocoder.attempt={active}"}) == []


@pytest.mark.skipif(not os.environ.get("WORKER_IMAGE"), reason="Set WORKER_IMAGE to the built builder image")
def test_builder_on_internal_networks_has_no_egress(settings, tmp_path, client):
    """T-I5: during Hermes execution a builder cannot reach github.com or openrouter.ai."""
    from autocoder.config import Profile
    from autocoder.worker import DockerRunner
    workers = client.networks.create(f"autocoder-test-{uuid.uuid4().hex[:8]}", internal=True)
    settings.worker_network, settings.builder.image = workers.name, os.environ["WORKER_IMAGE"]
    settings.worker_uid = settings.worker_gid = 1000
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    runner = DockerRunner(settings, tmp_path / "ws", tmp_path / "run", Profile(), "", lambda: None, client)
    runner.check_only = True
    try:
        runner.start(uuid.uuid4().hex)  # check_only start runs the isolation probe and raises on egress.
        assert runner.command(["sh", "-c", "test ! -S /var/run/docker.sock && test ! -w /workspace/.git"])[
            "exit_code"] == 0
    finally:
        runner.stop()
        if runner.network:
            runner.network.remove()
        workers.remove()


def test_unlabeled_container_is_never_removed(client):
    from autocoder.worker import cleanup_attempt
    attempt = uuid.uuid4().hex
    container = client.containers.create(POSTGRES, labels={"autocoder.attempt": attempt})
    try:
        with pytest.raises(ValueError, match="unlabeled"):
            cleanup_attempt(client, attempt)
        container.reload()
    finally:
        container.remove(force=True)
