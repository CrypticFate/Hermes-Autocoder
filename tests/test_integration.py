import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

import docker
from autocoder.budget import BudgetError, issue_capability, reserve
from autocoder.config import Profile
from autocoder.contracts import TaskContext
from autocoder.db import initialize, locked, session_factory
from autocoder.models import Attempt, Charge, Repository, Task
from autocoder.worker import DockerRunner

pytestmark = [pytest.mark.integration, pytest.mark.docker,
              pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="Set RUN_DOCKER_TESTS=1")]


def test_postgres_migration_and_concurrent_budget(settings):
    client = docker.from_env()
    container = client.containers.run("postgres:16-alpine", detach=True,
        environment={"POSTGRES_USER": "test", "POSTGRES_DB": "test", "POSTGRES_HOST_AUTH_METHOD": "trust"},
        ports={"5432/tcp": ("127.0.0.1", None)}, labels={"autocoder.test": "true"})
    factory = None
    try:
        container.reload()
        port = container.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        settings.database_url = f"postgresql+psycopg://test@127.0.0.1:{port}/test"
        factory = session_factory(settings)
        for _ in range(100):
            try:
                with factory().connection() as connection:
                    connection.exec_driver_sql("SELECT 1")
                break
            except Exception:
                time.sleep(0.1)
        configuration = Config("alembic.ini")
        configuration.attributes["database_url"] = settings.database_url
        command.upgrade(configuration, "head")
        initialize(factory, settings)
        with locked(factory) as (session, control):
            control.paused, control.daily_micro = False, 5000
            session.add(Repository(id=1, name="owner/repo", owner="owner", enabled=True))
            session.flush()
            session.add(Task(id="task", repo_id=1, title="Test", objective="Test", state="running", fingerprint="test"))
            session.flush()
            session.add(Attempt(id="attempt", task_id="task"))
        token = issue_capability(factory, "attempt", 60)
        def run(_):
            try:
                return reserve(factory, settings, token, {"model": "test-model", "messages": [], "max_tokens": 128})
            except BudgetError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(8)))
        assert sum(value is not None for value in results) == 1
        with factory() as session:
            assert session.scalar(select(Charge)).micro_usd <= 5000
    finally:
        if factory:
            factory.kw["bind"].dispose()
        container.remove(force=True, v=True)


@pytest.mark.skipif(not os.environ.get("WORKER_IMAGE"), reason="Set WORKER_IMAGE to built worker image ID")
def test_real_worker_isolation_import_and_timeout(settings, tmp_path):
    client = docker.from_env()
    network = client.networks.create(f"autocoder-test-{os.getpid()}", internal=True)
    settings.worker_network = network.name
    settings.proxy_url = "http://127.0.0.1:18765/v1"
    settings.worker_uid, settings.worker_gid = os.getuid(), os.getgid()
    if settings.worker_uid == 0:
        settings.worker_uid = settings.worker_gid = 1000
    workspace, run_dir = tmp_path / "workspace", tmp_path / "run"
    (workspace / ".git").mkdir(parents=True)
    (run_dir / "input").mkdir(parents=True)
    settings.builder.image = os.environ["WORKER_IMAGE"]
    profile = Profile(command_seconds=5)
    runner = DockerRunner(settings, workspace, run_dir, profile, "temporary-test-token", lambda: None)
    try:
        runner.start(f"test-{os.getpid()}")
        result = runner.command(["/opt/hermes/.venv/bin/python", "-c",
            "from run_agent import AIAgent; import autocoder.hermes_runner; print('imports-ok')"], timeout=30)
        assert result["exit_code"] == 0, result["output"]
        assert "imports-ok" in result["output"]
        context = TaskContext(task_id="fixture", attempt_id="fixture-attempt", mode="implement",
                              prompt="Create engine-proof.txt containing fixture completed, then reply fixture completed.",
                              base_sha="fixture", model="test-model", max_iterations=3,
                              max_output_tokens=128, timeout_seconds=60)
        (run_dir / "input" / "context.json").write_text(context.model_dump_json())
        stub = '''
import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_response(404); self.end_headers(); return
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        allowed = {'model', 'messages', 'tools', 'tool_choice', 'temperature', 'top_p',
                   'max_tokens', 'max_completion_tokens', 'stream', 'stream_options',
                   'parallel_tool_calls', 'reasoning_effort'}
        unexpected = set(payload) - allowed
        if unexpected:
            print('Unexpected request keys:', sorted(unexpected), flush=True)
            self.send_response(400); self.end_headers(); return
        body = {'id':'fixture', 'object':'chat.completion', 'created':1, 'model':'test-model',
                'choices':[{'index':0, 'message':{'role':'assistant','content':'fixture completed'},
                            'finish_reason':'stop'}],
                'usage':{'prompt_tokens':50, 'completion_tokens':3, 'total_tokens':53}}
        if not any(message.get('role') == 'tool' for message in payload['messages']):
            body['choices'][0] = {'index':0, 'finish_reason':'tool_calls', 'message': {
                'role':'assistant', 'content':None, 'tool_calls':[{'id':'fixture-call','type':'function',
                'function':{'name':'terminal','arguments':json.dumps({
                    'command': 'printf "fixture completed" > /workspace/engine-proof.txt'})}}]}}
        from autocoder.streaming import completion_sse
        data = (completion_sse(body) if payload.get('stream') else json.dumps(body)).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream' if payload.get('stream') else 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
server = HTTPServer(('127.0.0.1',18765), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
from autocoder.hermes_runner import main
main()
from pathlib import Path
result = json.loads(Path('/output/result.json').read_text())
assert result['completed'], result
assert 'fixture completed' in result['summary'], result
print('adapter-ok')
server.shutdown()
'''
        result = runner.command(["/opt/hermes/.venv/bin/python", "-c", stub], timeout=90)
        assert result["exit_code"] == 0, result["output"]
        assert "adapter-ok" in result["output"]
        assert (workspace / "engine-proof.txt").read_text() == "fixture completed"
        result = runner.command(["sh", "-c", "test ! -S /var/run/docker.sock && test ! -d /run/secrets && test ! -w /workspace/.git && test ! -w /opt/hermes"])
        assert result["exit_code"] == 0
        result = runner.command(["sleep", "5"], timeout=1)
        assert result["exit_code"] != 0
    finally:
        runner.stop()
        network.remove()
