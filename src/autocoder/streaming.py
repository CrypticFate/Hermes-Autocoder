"""Adapt a fully accounted completion to the SSE shape expected by Hermes."""
import json


def completion_sse(result):
    base = {"id": result.get("id", "completion"), "object": "chat.completion.chunk",
            "created": result.get("created", 0), "model": result.get("model", "")}
    choices, finishes = [], []
    for choice in result.get("choices", []):
        delta = dict(choice.get("message") or {})
        if delta.get("tool_calls"):
            delta["tool_calls"] = [{**call, "index": index} for index, call in enumerate(delta["tool_calls"])]
        choices.append({"index": choice["index"], "delta": delta, "finish_reason": None})
        finishes.append({"index": choice["index"], "delta": {}, "finish_reason": choice.get("finish_reason", "stop")})
    chunks = [{**base, "choices": choices}, {**base, "choices": finishes}]
    if result.get("usage"):
        chunks.append({**base, "choices": [], "usage": result["usage"]})
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
