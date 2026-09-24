import asyncio
import json
import math
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from autocoder.budget import BudgetError, capability_active, digest, reserve, settle
from autocoder.config import secret
from autocoder.models import Attempt, Capability, Control, Event
from autocoder.redaction import install_log_redaction
from autocoder.streaming import completion_sse


def create_app(settings, factory, transport=None, sleep=asyncio.sleep):
    install_log_redaction()

    @asynccontextmanager
    async def lifespan(app):
        install_log_redaction()
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1_000_000:
                raise HTTPException(413, "Request too large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise HTTPException(400, "messages required")
        # Text/tool Chat Completions (Hermes) and JSON mode (mem0); no media, files or provider overrides.
        allowed = {"model", "messages", "tools", "tool_choice", "temperature", "top_p",
                   "max_tokens", "max_completion_tokens", "stream", "stream_options",
                   "parallel_tool_calls", "reasoning_effort", "response_format", "stop"}
        if set(body) - allowed or not isinstance(body.get("stream", False), bool):
            raise HTTPException(400, "Unsupported request option")
        streaming = body.get("stream", False)
        body["stream"] = False
        body.pop("stream_options", None)
        if body.get("reasoning_effort") not in (None, "none", "minimal", "low", "medium", "high", "xhigh"):
            raise HTTPException(400, "Unsupported reasoning effort")
        response_format = body.get("response_format")
        if response_format is not None and (not isinstance(response_format, dict) or response_format.get(
                "type") not in {"text", "json_object", "json_schema"}):
            raise HTTPException(400, "Unsupported response_format")
        for message in body["messages"]:
            content = message.get("content") if isinstance(message, dict) else False
            if isinstance(content, list) and all(isinstance(part, dict) and part.get("type") == "text"
                                                 and isinstance(part.get("text"), str) for part in content):
                continue
            if content is not None and not isinstance(content, str):
                raise HTTPException(400, "Only text content is supported")
        if "max_tokens" not in body and "max_completion_tokens" not in body:
            body["max_tokens"] = settings.model.max_output_tokens
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            charge_id = reserve(factory, settings, token, body)
        except BudgetError as exc:
            if "ceiling" in str(exc):
                with factory.begin() as session:
                    cap = session.get(Capability, digest(token))
                    if cap:
                        attempt = session.get(Attempt, cap.attempt_id) if cap.attempt_id else None
                        session.add(Event(task_id=attempt.task_id if attempt else None, kind="budget_exhausted",
                                          detail=f"{cap.pool}: {exc}"))
            raise HTTPException(403, str(exc)) from None
        try:
            async with httpx.AsyncClient(timeout=120, transport=transport) as client:
                response = await client.post(settings.model.provider_url.rstrip("/") + "/chat/completions",
                                             json=body, headers={"Authorization": "Bearer " + secret(
                                                 settings.provider_key_file)})
                if response.status_code == 429:
                    try:
                        delay = float(response.headers.get("retry-after", "60"))
                    except ValueError:
                        delay = 60
                    if math.isfinite(delay) and 0 <= delay <= 65:
                        # One bounded retry, with pause/cancellation checks during the wait.
                        for _ in range(max(1, math.ceil(delay))):
                            await sleep(1)
                            with factory() as session:
                                cap = session.get(Capability, digest(token))
                                inactive = not capability_active(session, cap, session.get(Control, 1))
                            if inactive:
                                settle(factory, settings, charge_id, {"prompt_tokens": 0, "completion_tokens": 0})
                                raise HTTPException(403, "Run paused or inactive")
                        response = await client.post(settings.model.provider_url.rstrip("/") + "/chat/completions",
                                                     json=body, headers={"Authorization": "Bearer " + secret(
                                                         settings.provider_key_file)})
                if response.status_code == 429:
                    settle(factory, settings, charge_id, {"prompt_tokens": 0, "completion_tokens": 0})
                    raise HTTPException(429, "Provider rate limit; retry later", headers={"Retry-After": "60"})
            if response.is_error:
                settle(factory, settings, charge_id)
                raise HTTPException(502, f"Provider returned HTTP {response.status_code}")
            result = response.json()
            settle(factory, settings, charge_id, result.get("usage"))
            if streaming:
                return Response(completion_sse(result), media_type="text/event-stream")
            return result
        except (httpx.TransportError, ValueError, OSError):
            settle(factory, settings, charge_id)
            raise HTTPException(502, "Provider request failed; reservation retained") from None

    return app
