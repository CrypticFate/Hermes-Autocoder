import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Protocol

from docker.errors import NotFound

import docker
from autocoder.config import Profile, Settings
from autocoder.contracts import TaskContext as TaskContext
from autocoder.domain import RunResult
from autocoder.networks import (
    attach,
    create_attempt_network,
    detach,
    isolation_probe,
    remove_network,
    require_managed,
)
from autocoder.security import redact


class AgentRunner(Protocol):
    def run(self, context: TaskContext) -> RunResult: ...


def runtime_profile(settings: Settings, repository: str, workspace: Path) -> Profile:
    if repository in settings.repository_profiles:
        return settings.profiles[settings.repository_profiles[repository]]
    if (workspace / "package.json").exists():
        if (workspace / "package-lock.json").exists() and "node" in settings.profiles:
            package = json.loads((workspace / "package.json").read_text())
            scripts = package.get("scripts", {})
            checks = [f"npm run {name}" for name in ("lint", "typecheck", "test", "build") if name in scripts]
            return settings.profiles["node"].model_copy(update={"checks": checks})
    if (workspace / "uv.lock").exists() and "python" in settings.profiles:
        return settings.profiles["python"]
    raise ValueError("Unsupported runtime: configure repository_profiles and a pinned image/check commands")


class DockerRunner:
    def __init__(self, settings, workspace, run_dir, profile, token, heartbeat, client=None):
        self.settings, self.workspace, self.run_dir = settings, workspace, run_dir
        self.profile, self.token, self.heartbeat = profile, token, heartbeat
        self.client = client or docker.from_env()
        self.container = None
        self.network = None
        self.service_env = {}
        self.setup_network = None
        self.check_only = False
        self.deadline = time.monotonic() + settings.builder.deadline_seconds

    def start(self, attempt_id):
        if not self.settings.builder.image.startswith("sha256:") and "@sha256:" not in self.settings.builder.image:
            raise ValueError("Worker image must be pinned by digest or image ID")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        output = self.run_dir / "output"
        output.mkdir(exist_ok=True)
        (self.run_dir / "input").mkdir(exist_ok=True)
        runtime = self.run_dir.parent / "runtime"
        runtime.mkdir(exist_ok=True)
        # Paths are identical on the host and in the controller Compose service.
        for root in (self.workspace, output, runtime):
            if os.geteuid() == 0:
                for directory, dirs, files in os.walk(root, followlinks=False):
                    os.chown(directory, self.settings.worker_uid, self.settings.worker_gid)
                    for name in dirs + files:
                        os.chown(Path(directory) / name, self.settings.worker_uid,
                                 self.settings.worker_gid, follow_symlinks=False)
        self.network = self.network or create_attempt_network(self.client, attempt_id)
        workers = self.client.networks.get(self.settings.worker_network)
        if not workers.attrs.get("Internal"):
            raise ValueError("workers network must be internal")
        self.container = self.client.containers.run(
            self.settings.builder.image, ["sleep", "infinity"], detach=True, init=True,
            name=f"hermes-task-{attempt_id}", labels={"autocoder.managed": "true", "autocoder.attempt": attempt_id},
            user=f"{self.settings.worker_uid}:{self.settings.worker_gid}", working_dir="/workspace",
            read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            pids_limit=self.settings.builder.pids, mem_limit=self.settings.builder.memory,
            runtime=self.settings.builder.runtime,
            nano_cpus=int(self.settings.builder.cpus * 1_000_000_000),
            network=self.settings.worker_network,
            tmpfs={"/tmp": f"rw,nosuid,size={self.settings.worker_tmp_bytes},mode=1777",
                   "/home/worker": f"rw,nosuid,size={self.settings.worker_tmp_bytes},mode=1777"},
            volumes={str(self.workspace): {"bind": "/workspace", "mode": "ro" if self.check_only else "rw"},
                     str(self.workspace / ".git"): {"bind": "/workspace/.git", "mode": "ro"},
                     str(self.run_dir / "input"): {"bind": "/input", "mode": "ro"},
                     str(runtime): {"bind": "/venv", "mode": "ro" if self.check_only else "rw"},
                     str(output): {"bind": "/output", "mode": "rw"}},
            environment={**self.service_env, "HOME": "/home/worker", "HERMES_HOME": "/home/worker/hermes",
                         "TERMINAL_ENV": "local", "TERMINAL_CWD": "/workspace",
                         "AUTOCODER_PROXY_URL": self.settings.proxy_url,
                         "VIRTUAL_ENV": "/venv", "PATH": "/venv/bin:/usr/local/bin:/usr/bin:/bin",
                         "UV_CACHE_DIR": "/tmp/uv-cache", "CI": "true", "PYTHONDONTWRITEBYTECODE": "1",
                         "NPM_CONFIG_ENGINE_STRICT": "true"},
            log_config=docker.types.LogConfig(type="json-file", config={"max-size": "10m", "max-file": "2"}))
        attach(self.network, self.container)
        if self.check_only:
            # Check and review containers never get setup egress; prove it before anything runs.
            isolation_probe(self)
        return self.container.id

    def setup(self):
        if self.profile.setup and self.settings.builder.setup_egress:
            self.setup_network = self.client.networks.get("hermes-setup-egress")
            attach(self.setup_network, self.container)
        try:
            if not self.check_only:
                created = self.command(["python3", "-m", "venv", "/venv"])
                if created["exit_code"]:
                    raise ValueError("Could not create attempt Python environment")
            results = [self.command(command) for command in self.profile.setup]
            if any(result["exit_code"] for result in results):
                raise ValueError("Dependency setup failed")
        finally:
            if self.setup_network:
                detach(self.setup_network, self.container)
                self.setup_network = None
        isolation_probe(self)
        return results

    def command(self, argv, timeout=None, environment=None):
        import threading
        require_managed(self.container)
        original = argv
        if isinstance(argv, str):
            argv = ["sh", "-lc", argv]
        result, error = [], []
        remaining = int(self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Task deadline reached")
        seconds = min(timeout or self.profile.command_seconds, remaining)

        def execute():
            try:
                execution = self.client.api.exec_create(self.container.id,
                    ["timeout", "--signal=KILL", str(seconds), *argv], workdir="/workspace",
                    environment=environment or {})
                chunks, size = deque(), 0
                for chunk in self.client.api.exec_start(execution["Id"], stream=True):
                    chunks.append(chunk[-30000:])
                    size += len(chunks[-1])
                    while size > 60000 and len(chunks) > 1:
                        size -= len(chunks.popleft())
                info = self.client.api.exec_inspect(execution["Id"])
                result.append((info["ExitCode"], b"".join(chunks)[-30000:]))
            except Exception as exc:
                error.append(exc)
        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(timeout=1)
            self.heartbeat()
            if time.monotonic() >= self.deadline:
                self.stop()
                thread.join(timeout=5)
                raise TimeoutError("Task deadline reached")
        if error:
            raise error[0]
        code, output = result[0]
        return {"command": original, "exit_code": code,
                "output": redact(output.decode(errors="replace")[-30000:], [self.token])}

    def run(self, context: TaskContext) -> RunResult:
        # Controller writes input; the running worker sees the bind-mounted directory.
        (self.run_dir / "input" / "context.json").write_text(context.model_dump_json())
        result_path = self.run_dir / "output" / "result.json"
        for stale in (result_path, self.run_dir / "output" / "hermes_effective_config.json"):
            if stale.exists() or stale.is_symlink():
                stale.unlink()
        execution = self.command(["/opt/hermes/.venv/bin/python", "-m", "autocoder.hermes_runner"],
                                 timeout=context.timeout_seconds,
                                 environment={"AUTOCODER_MODEL_TOKEN": self.token})
        (self.run_dir / f"{context.mode}.log").write_text(execution["output"])
        # I7 and tool policy are checked before any builder output is trusted.
        effective = self.run_dir / "output" / "hermes_effective_config.json"
        if effective.is_symlink() or not effective.is_file() or effective.stat().st_size > 100_000:
            raise ValueError("Missing Hermes effective configuration")
        config = json.loads(effective.read_text())
        if config.get("memory_enabled") is not False or config.get("user_profile_enabled") is not False:
            raise ValueError("Builder memory must be disabled")
        if config.get("forbidden_tools") != []:
            raise ValueError("Forbidden builder toolset")
        if execution["exit_code"] != 0:
            raise RuntimeError(f"Hermes exited with code {execution['exit_code']}; see redacted worker log")
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > 200_000:
            raise ValueError("Missing or invalid Hermes result")
        result = RunResult.model_validate_json(redact(result_path.read_text(), [self.token]))
        if context.mode == "implement":
            result.validate_criteria(context.criteria)
        return result

    def stop(self):
        if self.container:
            require_managed(self.container)
            try:
                self.container.remove(force=True)
            except NotFound:
                pass
            except Exception as exc:
                logging.getLogger(__name__).warning("worker_cleanup_pending: %s", type(exc).__name__)
                return
            self.container = None


def cleanup_attempt(client, attempt_id):
    for container in client.containers.list(all=True, filters={"label": f"autocoder.attempt={attempt_id}"}):
        require_managed(container)
        container.remove(force=True)
    for network in client.networks.list(filters={"label": f"autocoder.attempt={attempt_id}"}):
        remove_network(network)


def run_checks(settings, workspace, run_dir, profile, commands, heartbeat, client, network, service_env):
    runner = DockerRunner(settings, workspace, run_dir / "checks", profile, "", heartbeat, client)
    runner.network, runner.service_env, runner.check_only = network, service_env, True
    try:
        runner.start(network.labels["autocoder.attempt"])
        isolation_probe(runner)
        results = [runner.command(command) for command in commands]
        for result in results:
            result["output"] = redact(result["output"][-4096:])
        return results
    finally:
        runner.stop()
