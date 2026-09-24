"""Invariant tests T-I1..T-I14 (plan section 2). T-I14 redaction basics live in test_redaction.py."""
import asyncio
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from sqlalchemy import select

from autocoder.gitops import Git, GitError, assert_agent_branch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "autocoder"
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text())


def sources():
    return {p.name: p.read_text() for p in SRC.glob("*.py")}


# --------------------------------------------------------------------------- I1
def test_i1_no_merge_or_auto_merge_code_path():
    pattern = re.compile(r"/merge\b|merge_pull|mergePullRequest|enablePullRequestAutoMerge|auto_merge|"
                         r"automerge|\bmerge\s*\(", re.I)
    offenders = [f"{name}:{i}" for name, text in sources().items()
                 for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    assert offenders == []
    github = (SRC / "github.py").read_text()
    words = set(re.findall(r"\w*merge\w*", github, re.I))
    assert words <= {"mergeable", "merged", "merged_at"}, words


@pytest.mark.parametrize("branch", ["main", "master", "refs/heads/main", "agent/../main", "agent/x:main",
                                    "agent/", "Agent/01-x", "agent/01-x/../../main", "+agent/01-x", ""])
def test_i1_pre_push_assertion_rejects_non_agent_refs(branch, tmp_path):
    with pytest.raises(GitError):
        assert_agent_branch(branch)
    with pytest.raises(GitError):
        Git().push(tmp_path, branch)


@pytest.mark.parametrize("branch", ["agent/01-setup", "agent/01-setup-r1", "agent/12-x-rb2", "agent/plans-ab12"])
def test_i1_agent_branches_are_allowed(branch):
    assert assert_agent_branch(branch) == "refs/heads/" + branch


def test_i1_push_never_forces():
    text = (SRC / "gitops.py").read_text()
    assert "--force" not in text and "+HEAD" not in text


# --------------------------------------------------------------------------- I2
@pytest.mark.parametrize("permission", ["admin", "maintain", "read", "triage"])
def test_i2_bot_permission_must_be_exactly_write(settings, factory, permission):
    from helpers import github_double

    from autocoder.onboarding import register_repository
    client = github_double()
    client.permission.return_value = {"permission": permission}
    assert not register_repository(settings, factory, "owner/repo", client=client).accepted


def test_i2_publish_always_reverifies_with_force():
    text = (SRC / "controller.py").read_text()
    publish = text[text.index("    def publish("):text.index("    # ------------------------------------------------------------ PR")]
    assert publish.index("verify_repository(") < publish.index("git.push(")
    assert "force=True" in publish[publish.index("verify_repository("):publish.index("git.push(")]


# --------------------------------------------------------------------------- I3/I4
def service(name):
    return COMPOSE["services"][name]


def test_i3_i4_secret_mounts():
    holders = {name: set(svc.get("secrets", [])) for name, svc in COMPOSE["services"].items()}
    assert [n for n, s in holders.items() if "github_bot" in s] == ["controller"]
    assert [n for n, s in holders.items() if "model_provider" in s] == ["model-proxy"]
    assert holders["concierge"] == {"concierge_model_token", "mcp_concierge_token", "db_mem0_password"}
    for name, svc in COMPOSE["services"].items():
        for volume in svc.get("volumes", []):
            if "docker.sock" in str(volume):
                assert name == "docker-proxy"
            assert "secrets" not in str(volume).split(":")[0]


def test_i4_provider_key_read_only_by_proxy():
    readers = [name for name, text in sources().items() if "provider_key_file" in text and name != "config.py"]
    assert readers == ["proxy.py"]


def test_i3_builder_environment_has_no_credentials(settings, tmp_path):
    from autocoder.worker import DockerRunner
    client = Mock()
    client.networks.get.return_value.attrs = {"Internal": True}
    container = client.containers.run.return_value
    container.labels = {"autocoder.managed": "true"}
    runner = DockerRunner(settings, tmp_path / "ws", tmp_path / "run", Mock(setup=[]), "", lambda: None, client)
    runner.network = Mock(labels={"autocoder.managed": "true"})
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    runner.start("attempt")
    kwargs = client.containers.run.call_args.kwargs
    env = json.dumps(kwargs["environment"])
    assert "test-github-credential" not in env and "test-provider-credential" not in env
    assert not any(k.startswith(("GITHUB", "GH_", "OPENROUTER", "AUTOCODER_MODEL_TOKEN")) for k in kwargs["environment"])
    # I5 hardening on the same container.
    assert kwargs["read_only"] is True and kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert kwargs["user"] != "0:0" and not kwargs["user"].startswith("0:")
    assert kwargs["volumes"][str(tmp_path / "ws" / ".git")] == {"bind": "/workspace/.git", "mode": "ro"}
    assert not any("docker.sock" in path for path in kwargs["volumes"])
    assert kwargs["network"] == settings.worker_network


def test_i3_mcp_registry_has_no_git_or_github_write_tools(settings, factory):
    from autocoder.mcp_server import Operations, build_server
    names = {t.name for t in asyncio.run(build_server(Operations(settings, factory)).list_tools())}
    assert not {n for n in names if re.search(r"merge|git|push|branch|commit|exec|run_|secret|config|budget", n)}


# --------------------------------------------------------------------------- I5
def test_i5_network_topology():
    networks = COMPOSE["networks"]
    assert networks["control"]["internal"] is True and networks["workers"]["internal"] is True
    assert set(service("concierge")["networks"]) == {"control"}
    assert set(service("database")["networks"]) == {"control"}
    assert set(service("controller")["networks"]) == {"control", "github-egress"}
    assert set(service("model-proxy")["networks"]) == {"control", "workers", "provider-egress"}
    assert service("controller")["environment"]["DOCKER_HOST"] == "tcp://docker-proxy:2375"


def test_i5_setup_egress_is_detached_before_isolation_probe():
    from autocoder.worker import DockerRunner
    runner = DockerRunner.__new__(DockerRunner)
    order = []
    runner.profile = SimpleNamespace(setup=["pip install x"])
    runner.settings = SimpleNamespace(builder=SimpleNamespace(setup_egress=True))
    runner.check_only, runner.setup_network = True, None
    runner.container = Mock(labels={"autocoder.managed": "true"})
    runner.client = Mock()
    network = runner.client.networks.get.return_value
    network.connect.side_effect = lambda *a, **k: order.append("attach")
    network.disconnect.side_effect = lambda *a, **k: order.append("detach")

    def command(argv, timeout=None, environment=None):
        order.append("probe" if "socket" in str(argv) else "setup")
        return {"exit_code": 0, "output": ""}
    runner.command = command
    runner.setup()
    assert order == ["attach", "setup", "detach", "probe"]


def test_i5_attempt_networks_are_internal():
    from autocoder.networks import create_attempt_network
    client = Mock()
    create_attempt_network(client, "a1")
    assert client.networks.create.call_args.kwargs["internal"] is True
    assert client.networks.create.call_args.kwargs["labels"]["autocoder.managed"] == "true"


def test_i5_refuses_unlabeled_docker_objects(settings, factory):
    from autocoder.networks import require_managed
    from autocoder.sidecars import SidecarManager
    from autocoder.worker import cleanup_attempt
    unlabeled = Mock(labels={}, attrs={})
    with pytest.raises(ValueError, match="unlabeled"):
        require_managed(unlabeled)
    client = Mock()
    client.containers.list.return_value = [unlabeled]
    with pytest.raises(ValueError, match="unlabeled"):
        cleanup_attempt(client, "attempt")
    unlabeled.remove.assert_not_called()
    manager = SidecarManager(settings, factory, client, Mock(labels={}, attrs={}))
    with pytest.raises(ValueError, match="unlabeled"):
        manager.stop_services("none")


# --------------------------------------------------------------------------- I6
def concierge_setup():
    spec = importlib.util.spec_from_file_location("concierge_setup", ROOT / "docker/concierge/concierge_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rendered(tmp_path, template_edit=None):
    module = concierge_setup()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("concierge_model_token", "mcp_concierge_token", "db_mem0_password"):
        (secrets / name).write_text(f"{name}-value-" + "x" * 32)
    if template_edit:
        original = (module.HERE / "config.yaml.tmpl").read_text()
        module.HERE = tmp_path
        (tmp_path / "config.yaml.tmpl").write_text(template_edit(original))
        (tmp_path / "SOUL.md").write_text((ROOT / "docker/concierge/SOUL.md").read_text())
    home = module.render({"CONCIERGE_MODEL": "test/model"}, secrets, tmp_path / "home")
    return module, yaml.safe_load((home / "config.yaml").read_text()), home


def fake_resolver(config):
    """Mirrors Hermes: platform lists resolve to toolsets; an unknown platform falls back to everything."""
    universe = {"terminal": {"terminal", "process"}, "file": {"read_file", "write_file", "patch"},
                "memory": {"memory"}, "session_search": {"session_search"}, "clarify": {"clarify"},
                "todo": {"todo"}, "code_execution": {"execute_code"}, "web": {"web_search"}}
    toolsets = set()
    for platform in ("cli", "telegram"):
        toolsets |= set((config.get("platform_toolsets") or {}).get(platform) or universe)
    return toolsets, set().union(*(universe.get(t, set()) for t in toolsets))


def test_i6_rendered_concierge_config_is_safe(tmp_path):
    module, config, home = rendered(tmp_path)
    assert module.selfcheck(config, fake_resolver, env={}) == []
    assert config["mcp_servers"]["autocoder"]["url"] == "http://controller:8765/mcp"
    assert config["memory"]["provider"] == "mem0" and config["kanban"]["dispatch_in_gateway"] is False
    assert (home / "config.yaml").stat().st_mode & 0o077 == 0
    mem0 = json.loads((home / "mem0.json").read_text())
    assert mem0["mode"] == "oss" and mem0["oss"]["vector_store"]["provider"] == "pgvector"
    assert mem0["oss"]["embedder"]["provider"] == "fastembed"
    assert mem0["oss"]["llm"]["config"]["openai_base_url"] == "http://model-proxy:8080/v1"


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("  cli: ${TOOLSETS}", '  cli: ["memory", "terminal"]'),
    lambda t: t.replace("toolsets: ${TOOLSETS}\n", 'toolsets: ["memory", "file"]\n', 1),
    lambda t: t.replace("  telegram: ${TOOLSETS}\n", ""),
    lambda t: t.replace("  provider: mem0", "  provider: \"\""),
    lambda t: t + "\n" if False else t.replace("mcp_servers:\n", "mcp_servers:\n  shell:\n    command: sh\n"),
    lambda t: t.replace('    enabled: "off"\n', ""),
    lambda t: t.replace('    enabled: "off"', '    enabled: auto'),
])
def test_i6_selfcheck_fails_when_template_enables_forbidden_tools(tmp_path, edit):
    module, config, _ = rendered(tmp_path, edit)
    assert module.selfcheck(config, fake_resolver, env={})


def test_i6_selfcheck_fails_on_tool_search_bridge_tools(tmp_path):
    module, config, _ = rendered(tmp_path)
    def bridge_resolver(config):
        toolsets, tools = fake_resolver(config)
        return toolsets, tools | {"tool_search", "tool_describe", "tool_call"}
    assert any("tool_call" in p for p in module.selfcheck(config, bridge_resolver, env={}))
    assert module.tool_search_off({"tools": {"tool_search": False}})
    assert not module.tool_search_off({"tools": {"tool_search": {"enabled": False}}})


def test_i6_kanban_only_allowed_when_gated_off(tmp_path):
    module, config, _ = rendered(tmp_path)

    def resolver(cfg):
        return {"memory", "kanban"}, {"memory", "kanban_create"}
    assert module.selfcheck(config, resolver, env={}) == []
    assert module.selfcheck(config, resolver, env={"HERMES_KANBAN_TASK": "t1"})


def test_i6_concierge_entrypoint_runs_selfcheck_before_start():
    text = (ROOT / "docker/concierge/entrypoint.sh").read_text()
    assert "set -eu" in text and text.index("selfcheck") < text.index("exec hermes gateway run")


# --------------------------------------------------------------------------- I7
def test_i7_builder_memory_on_is_rejected(settings, tmp_path):
    from autocoder.contracts import TaskContext
    from autocoder.worker import DockerRunner
    runner = DockerRunner.__new__(DockerRunner)
    runner.run_dir, runner.token = tmp_path, "tok"
    (tmp_path / "input").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "result.json").write_text('{"summary": "x"}')
    effective = {}

    def command(*args, **kwargs):  # Stands in for Hermes writing its effective configuration.
        (tmp_path / "output" / "hermes_effective_config.json").write_text(json.dumps(effective))
        (tmp_path / "output" / "result.json").write_text('{"summary": "x"}')
        return {"exit_code": 0, "output": ""}
    runner.command = command
    context = TaskContext(task_id="t", attempt_id="a", mode="plan", prompt="", base_sha="s", model="m",
                          max_iterations=1, max_output_tokens=128, timeout_seconds=30)
    for config, error in [({"memory_enabled": True, "user_profile_enabled": False, "forbidden_tools": []}, "memory"),
                          ({"memory_enabled": False, "user_profile_enabled": False,
                            "forbidden_tools": ["web_search"]}, "Forbidden")]:
        effective.clear()
        effective.update(config)
        with pytest.raises(ValueError, match=error):
            runner.run(context)


def test_i7_builder_hermes_config_disables_memory():
    text = (SRC / "hermes_runner.py").read_text()
    assert '"memory_enabled": False' in text and '"user_profile_enabled": False' in text
    assert "skip_memory=True" in text and '"mcp_servers": {}' in text
    assert 'enabled_toolsets=["terminal", "file"]' in text


# --------------------------------------------------------------------------- I8/I9
@pytest.mark.parametrize("path", ["plans/01-x.md", "plans/new.md", "report/01-x.md", "report/.gitkeep"])
def test_i8_i9_builder_cannot_touch_plans_or_reports(tmp_path, path):
    git = Git()
    git.run(tmp_path, "init")
    (tmp_path / "README.md").write_text("x")
    base = git.commit(tmp_path, "base")
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("builder edit")
    _, problems = git.gate(tmp_path, base, kind="implementation")
    assert any("operator-owned" in p for p in problems)


def test_i9_report_written_only_after_gates():
    text = (SRC / "controller.py").read_text()
    implement = text[text.index("    def _implement("):text.index("    def _checks(")]
    assert implement.index("git.gate(") < implement.index("safe_write(workspace, task.report_path")


# --------------------------------------------------------------------------- I10
def test_i10_one_active_implementation_per_repo(factory):
    from autocoder.models import Repository, Task
    from autocoder.scheduler import ACTIVE, next_task
    with factory.begin() as session:
        for seq, state in ((1, "pr_open"), (2, "pending")):
            session.add(Task(id=f"p{seq}", repo_id=1, plan_path=f"plans/0{seq}-x.md", plan_seq=seq,
                             plan_slug="x", title="x", objective="x", fingerprint=f"p{seq}", state=state))
    with factory() as session:
        assert next_task(session, session.get(Repository, 1)) is None
        assert "pr_open" in ACTIVE and "changes_requested" in ACTIVE


# --------------------------------------------------------------------------- I11
def test_i11_non_operator_feedback_never_reaches_repair_context(settings, factory):
    from autocoder import feedback
    from autocoder.models import Task
    with factory.begin() as session:
        session.add(Task(id="t", repo_id=1, title="x", objective="x", fingerprint="t", state="pr_open",
                         pr_number=1, repair_count=1))
    client = Mock()
    old = "2020-01-01T00:00:00Z"
    client.issue_comments.return_value = [
        {"id": 1, "user": {"login": "attacker"}, "created_at": old, "body": "EXFILTRATE"},
        {"id": 2, "user": {"login": "Owner"}, "created_at": old, "body": "fine change"}]
    client.review_comments.return_value = client.reviews.return_value = client.check_runs.return_value = []
    feedback.collect(settings, factory, "t", client, "owner/repo", {"number": 1, "head": {"sha": "s"}})
    with factory() as session:
        repair, ids = feedback.repair_context(session, session.get(Task, "t"))
    assert "EXFILTRATE" not in json.dumps(repair) and "fine change" in json.dumps(repair)
    assert len(ids) == 1


# --------------------------------------------------------------------------- I12
def test_i12_services_only_from_digest_pinned_allowlist(settings, factory):
    from pydantic import ValidationError

    from autocoder.config import ServiceConfig
    from autocoder.plans import parse
    from autocoder.sidecars import SidecarManager
    with pytest.raises(ValidationError):
        ServiceConfig(image="postgres:16", healthcheck=["true"])
    plan = "---\nservices: [postgres]\n---\n# X\n\n## Objective\nx\n\n## Acceptance criteria\n- y\n"
    with pytest.raises(ValueError, match="Unknown services"):
        parse("plans/01-x.md", plan, allowlist={})
    with pytest.raises(ValueError, match="Unknown"):
        SidecarManager(settings, factory, Mock(), Mock()).start_services("a", ["postgres"])
    for name, spec in yaml.safe_load((ROOT / "config.example.yaml").read_text())["services_allowlist"].items():
        assert re.search(r"@sha256:[0-9a-f]{64}$", spec["image"]), name


# --------------------------------------------------------------------------- I13
def test_i13_state_and_projections_come_from_database(settings, factory):
    from autocoder.mcp_server import Operations
    from autocoder.models import Repository, Task, utcnow
    from autocoder.reports import pr_body
    with factory.begin() as session:
        session.get(Repository, 1).onboarded_at = utcnow()
        session.add(Task(id="t", repo_id=1, plan_path="plans/01-x.md", plan_seq=1, plan_slug="x", title="DB title",
                         objective="x", fingerprint="t", state="pr_open", acceptance=["a"],
                         report_path="report/01-x.md"))
    with factory() as session:
        body = pr_body(session.get(Task, "t"), None, None, [], [], "owner")
    assert "DB title" in body and "report/01-x.md" in body
    status = Operations(settings, factory).get_status()
    assert status["repositories"][0]["current_task"]["state"] == "pr_open"
    assert not list((settings.data_dir).glob("**/*.md"))


def test_i13_task_state_assigned_only_by_scheduler():
    offenders = [f"{name}:{i}" for name, text in sources().items() if name != "scheduler.py"
                 for i, line in enumerate(text.splitlines(), 1)
                 if re.search(r"\b(task|current|row|item)\.state\s*=[^=]", line)]
    assert offenders == []


# --------------------------------------------------------------------------- I14
def test_i14_outbound_text_is_redacted(settings, factory):
    from autocoder.mcp_server import audit, cap_output
    from autocoder.models import Event, Notification
    from autocoder.notifications import notify
    from autocoder.redaction import register_secret
    from autocoder.reports import pr_body
    secret = "unit-test-outbound-secret-123"
    register_secret(secret)
    token = "ghp_" + "a" * 36
    assert secret not in json.dumps(cap_output({"x": secret, "y": [token]}))
    with factory.begin() as session:
        notify(session, f"leak {secret} {token}")
    audit(factory, "get_status", {"repo": secret}, "ok")
    with factory() as session:
        stored = [n.message for n in session.scalars(select(Notification))] + \
                 [e.detail for e in session.scalars(select(Event))]
    assert stored and not any(secret in s or token in s for s in stored)
    task = SimpleNamespace(plan_path="plans/01-x.md", title=secret, report_path="report/01-x.md", acceptance=[])
    assert secret not in pr_body(task, None, None, [], [token], "owner")


def test_i14_log_handlers_redact_after_configuration(capsys, monkeypatch):
    import logging

    from autocoder.cli import configure_logging
    from autocoder.redaction import register_secret
    secret = "unit-test-log-secret-456"
    register_secret(secret)
    monkeypatch.setenv("AUTOCODER_LOG_FORMAT", "json")
    configure_logging()
    logging.getLogger("autocoder.test").warning("attempt_failed repo=o/r task_id=t1 reason=%s", secret)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["repo"] == "o/r" and payload["task_id"] == "t1" and secret not in line
