import secrets
import time

from docker.errors import NotFound
from sqlalchemy import select

from autocoder.models import Sidecar
from autocoder.networks import remove_network, require_managed
from autocoder.redaction import register_secret


class SidecarManager:
    def __init__(self, settings, factory, client, network):
        self.settings, self.factory, self.client, self.network = settings, factory, client, network

    def start_services(self, attempt, names):
        if set(names) - self.settings.services_allowlist.keys():
            raise ValueError("Unknown service in plan")
        result = {}
        for name in names:
            spec = self.settings.services_allowlist[name]
            password = secrets.token_urlsafe(32)
            register_secret(password)
            def render(value):
                return value.replace("{{password}}", password).replace("{{host}}", name)
            env = {k: render(v) for k, v in spec.env.items()}
            exposed = {k: render(v) for k, v in spec.expose_env.items()}
            if result.keys() & exposed.keys():
                raise ValueError("Declared services expose conflicting environment variable names")
            result.update(exposed)
            with self.factory.begin() as session:
                row = Sidecar(attempt_id=attempt, service_name=name, image=spec.image,
                              network_id=self.network.id, status="starting")
                session.add(row)
                session.flush()
                row_id = row.id
            container = self.client.containers.create(spec.image,
                command=[render(v) for v in spec.command] or None,
                name=f"hermes-sidecar-{attempt}-{name}", environment=env,
                labels={"autocoder.managed": "true", "autocoder.attempt": attempt, "autocoder.sidecar": name},
                network=self.network.name, runtime=self.settings.builder.runtime,
                mem_limit=spec.memory, nano_cpus=int(spec.cpus * 1_000_000_000), pids_limit=256,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                tmpfs={spec.tmpfs: "rw,nosuid,size=1g"} if spec.tmpfs else {},
                log_config={"type": "none"})
            with self.factory.begin() as session:
                session.get(Sidecar, row_id).container_id = container.id
            require_managed(container)
            self.network.disconnect(container)
            self.network.connect(container, aliases=[name])
            container.start()
            deadline = time.monotonic() + 60
            healthy = False
            while time.monotonic() < deadline:
                check = container.exec_run(["timeout", "5", *[render(v) for v in spec.healthcheck]])
                if check.exit_code == 0:
                    healthy = True
                    break
                time.sleep(1)
            with self.factory.begin() as session:
                session.get(Sidecar, row_id).status = "healthy" if healthy else "failed"
            if not healthy:
                raise RuntimeError(f"service_unhealthy: {name}")
        return result

    def stop_services(self, attempt):
        with self.factory() as session:
            rows = session.scalars(select(Sidecar).where(Sidecar.attempt_id == attempt,
                                                         Sidecar.status != "removed")).all()
        for row in rows:
            if row.container_id:
                try:
                    container = self.client.containers.get(row.container_id)
                    require_managed(container)
                    container.remove(force=True)
                except NotFound:
                    pass
            with self.factory.begin() as session:
                current = session.get(Sidecar, row.id)
                current.status, current.removed_at = "removed", time.time()
        remove_network(self.network)
