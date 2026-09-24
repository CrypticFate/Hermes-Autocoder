from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from autocoder.budget import BudgetError, issue_capability, reserve, revoke, settle
from autocoder.db import locked
from autocoder.models import Charge, Task
from autocoder.proxy import create_app


def body():
    return {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 128}


def test_free_model_accounting_and_request_limit(settings, factory, active):
    from autocoder.config import Settings
    settings = Settings.model_validate({**settings.model_dump(),
        "model": {**settings.model.model_dump(), "input_usd_per_mtok": 0, "output_usd_per_mtok": 0},
        "budgets": {**settings.budgets.model_dump(), "pools": {"builder": {"requests_per_attempt": 1}}}})
    assert settings.paid_errors() == []
    token = issue_capability(factory, active, 60)
    charge_id = reserve(factory, settings, token, body())
    settle(factory, settings, charge_id, {"prompt_tokens": 20, "completion_tokens": 10})
    with factory() as session:
        assert session.get(Charge, charge_id).micro_usd == 0
    with pytest.raises(BudgetError):
        reserve(factory, settings, token, body())


def test_atomic_budget_across_concurrent_requests(settings, factory, active):
    token = issue_capability(factory, active, 60)
    with locked(factory) as (_, control):
        control.daily_micro = 5000
    def request(_):
        try:
            return reserve(factory, settings, token, body())
        except BudgetError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(8)))
    assert sum(value is not None for value in results) == 1


def test_unknown_outcome_conservatively_charged(settings, factory, active):
    token = issue_capability(factory, active, 60)
    charge_id = reserve(factory, settings, token, body())
    settle(factory, settings, charge_id)
    with factory() as session:
        charge = session.get(Charge, charge_id)
        assert charge.micro_usd == charge.reserved_micro
        assert charge.state == "estimated"


def test_usage_and_idempotent_settlement(settings, factory, active):
    token = issue_capability(factory, active, 60)
    charge_id = reserve(factory, settings, token, body())
    settle(factory, settings, charge_id, {"prompt_tokens": 20, "completion_tokens": 10})
    settle(factory, settings, charge_id, {"prompt_tokens": 400, "completion_tokens": 300})
    with factory() as session:
        assert session.get(Charge, charge_id).micro_usd == 40


@pytest.mark.parametrize("action", ["cancel", "pause", "revoke", "expire", "unconfigured"])
def test_capability_restrictions(settings, factory, active, action):
    token = issue_capability(factory, active, -1 if action == "expire" else 60)
    if action == "revoke":
        revoke(factory, active)
    with locked(factory) as (session, control):
        if action == "cancel":
            session.get(Task, "task").state = "cancelled"
        if action == "pause":
            control.paused = True
        if action == "unconfigured":
            control.monthly_micro = None
    with pytest.raises(BudgetError):
        reserve(factory, settings, token, body())


def test_proxy_never_forwards_run_token(settings, factory, active):
    token = issue_capability(factory, active, 60)
    received = []
    def handler(request):
        received.append(request)
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    client = TestClient(create_app(settings, factory, httpx.MockTransport(handler)))
    response = client.post("/v1/chat/completions", json=body(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert received[0].headers["authorization"] == "Bearer test-provider-credential"
    assert token not in received[0].content.decode()
    with factory() as session:
        assert session.scalar(select(Charge)).micro_usd == 11


def test_proxy_rejects_bypasses_and_no_provider_call(settings, factory, active):
    token = issue_capability(factory, active, 60)
    client = TestClient(create_app(settings, factory, httpx.MockTransport(lambda _: pytest.fail("Must not call"))))
    for extra in ({"stream": "true"}, {"provider": "other"}, {"model": "unpriced-model"}, {"max_tokens": 999999}):
        response = client.post("/v1/chat/completions", json={**body(), **extra},
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code in (400, 403)


def test_provider_failure_does_not_expose_response(settings, factory, active):
    token = issue_capability(factory, active, 60)
    client = TestClient(create_app(settings, factory, httpx.MockTransport(
        lambda _: httpx.Response(500, text="secret-provider-details"))))
    response = client.post("/v1/chat/completions", json=body(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 502
    assert "secret-provider-details" not in response.text
    with factory() as session:
        charge = session.scalar(select(Charge))
        assert charge.micro_usd > 0


def test_hermes_nonstreaming_metadata_is_normalized(settings, factory, active):
    import json
    token = issue_capability(factory, active, 60)
    def handler(request):
        payload = json.loads(request.content)
        assert "stream_options" not in payload
        assert payload["reasoning_effort"] == "medium"
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    client = TestClient(create_app(settings, factory, httpx.MockTransport(handler)))
    response = client.post("/v1/chat/completions", json={**body(), "stream": False,
        "stream_options": {"include_usage": True}, "reasoning_effort": "medium"},
        headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_streaming_is_buffered_and_accounted(settings, factory, active):
    import json
    token = issue_capability(factory, active, 60)
    def handler(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json={"id": "fixture", "model": "test-model", "created": 1,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    client = TestClient(create_app(settings, factory, httpx.MockTransport(handler)))
    response = client.post("/v1/chat/completions", json={**body(), "stream": True},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert chunks[1]["choices"][0]["finish_reason"] == "tool_calls"
    with factory() as session:
        assert session.scalar(select(Charge)).state == "measured"


@pytest.mark.parametrize("cancel", [False, True])
def test_rate_limit_wait_respects_cancellation(settings, factory, active, cancel):
    token = issue_capability(factory, active, 60)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    async def sleep(_):
        if cancel:
            revoke(factory, active)
    client = TestClient(create_app(settings, factory, httpx.MockTransport(handler), sleep=sleep))
    response = client.post("/v1/chat/completions", json=body(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == (403 if cancel else 200)
    assert len(calls) == (1 if cancel else 2)
    with factory() as session:
        assert len(session.scalars(select(Charge)).all()) == 1
        assert session.scalar(select(Charge)).micro_usd == (0 if cancel else 11)


def concierge_settings(settings, builder_per_day=500, concierge_per_day=300):
    from autocoder.config import Settings
    return Settings.model_validate({**settings.model_dump(), "budgets": {
        **settings.budgets.model_dump(), "pools": {"builder": {"requests_per_day": builder_per_day},
                                                   "concierge": {"requests_per_day": concierge_per_day}}}})


def test_concierge_token_works_while_builder_pool_paused(settings, factory, active):
    from autocoder.budget import register_concierge_token
    concierge = "c" * 43
    register_concierge_token(factory, concierge)
    attempt = issue_capability(factory, active, 60)
    with locked(factory) as (_, control):
        control.paused = True
    with pytest.raises(BudgetError, match="paused"):
        reserve(factory, settings, attempt, body())
    charge = reserve(factory, settings, concierge, body())
    with factory() as session:
        assert session.get(Charge, charge).pool == "concierge"
        assert session.get(Charge, charge).attempt_id is None


def test_pool_limits_are_independent(settings, factory, active):
    from autocoder.budget import register_concierge_token
    settings = concierge_settings(settings, builder_per_day=1, concierge_per_day=2)
    concierge = "d" * 43
    register_concierge_token(factory, concierge)
    attempt = issue_capability(factory, active, 60)
    reserve(factory, settings, attempt, body())
    with pytest.raises(BudgetError, match="Builder pool"):
        reserve(factory, settings, attempt, body())
    reserve(factory, settings, concierge, body())
    reserve(factory, settings, concierge, body())
    with pytest.raises(BudgetError, match="Concierge pool"):
        reserve(factory, settings, concierge, body())


def test_rotation_revokes_previous_concierge_token(settings, factory):
    from autocoder.budget import register_concierge_token
    old, new = "e" * 43, "f" * 43
    register_concierge_token(factory, old)
    register_concierge_token(factory, new)
    with pytest.raises(BudgetError, match="Invalid"):
        reserve(factory, settings, old, body())
    reserve(factory, settings, new, body())


@pytest.mark.parametrize("extra,status", [
    ({"response_format": {"type": "json_object"}}, 200),
    ({"response_format": {"type": "json_schema", "json_schema": {"name": "facts", "schema": {}}}}, 200),
    ({"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}, 200),
    ({"tools": [{"type": "function", "function": {"name": "get_status", "parameters": {}}}],
      "tool_choice": "auto"}, 200),
    ({"response_format": {"type": "bogus"}}, 400),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}, 400),
])
def test_concierge_and_mem0_request_shapes(settings, factory, extra, status):
    from autocoder.budget import register_concierge_token
    token = "g" * 43
    register_concierge_token(factory, token)
    client = TestClient(create_app(settings, factory, httpx.MockTransport(lambda _: httpx.Response(
        200, json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))))
    response = client.post("/v1/chat/completions", json={**body(), **extra},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == status
