from unittest.mock import Mock

import pytest

from autocoder.contracts import RunResult, TaskContext
from autocoder.hermes_runner import build_prompt
from autocoder.networks import create_attempt_network, isolation_probe, require_managed


def test_result_criteria_must_match_order_and_no_extra_fields():
    result = RunResult(criteria=[{"criterion": "one", "status": "met", "evidence": "file"}])
    assert result.validate_criteria(["one"]) is result
    with pytest.raises(ValueError, match="1:1"):
        result.validate_criteria(["two"])
    with pytest.raises(ValueError):
        RunResult(summary="x" * 2001)
    with pytest.raises(ValueError):
        RunResult(invented=True)


def test_networks_internal_and_unmanaged_operations_refused():
    client = Mock()
    create_attempt_network(client, "attempt")
    assert client.networks.create.call_args.kwargs["internal"] is True
    with pytest.raises(ValueError, match="unlabeled"):
        require_managed(Mock(labels={}, attrs={}))
    runner = Mock()
    runner.command.return_value = {"exit_code": 1}
    with pytest.raises(RuntimeError, match="isolation"):
        isolation_probe(runner)


def test_prompt_has_plan_and_only_service_environment_names():
    context = TaskContext(task_id="t", attempt_id="a", mode="implement", prompt="", base_sha="sha",
        model="test", max_iterations=2, max_output_tokens=128, timeout_seconds=30,
        plan_path="plans/01-first.md", title="First", objective="Build", criteria=["Works"],
        service_env_names=["DATABASE_URL"], services=["postgres"])
    prompt = build_prompt(context)
    assert "plans/01-first.md" in prompt and "DATABASE_URL" in prompt
