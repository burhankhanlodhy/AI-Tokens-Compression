"""T1 routed-provider wire coverage for selective tool compression.

The provider matrix must not silently bypass TOOL_SCHEMA_COMPRESSION_ENABLED:
all six supported providers use the real chat route and a capture transport,
without requiring a live Postgres acceptance database.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import get_settings  # noqa: E402


class _CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )


@pytest.mark.parametrize("model,provider", [
    ("openai/gpt-4o", "openai"),
    ("anthropic/claude-sonnet-5", "anthropic"),
    ("openrouter/openai/gpt-4o", "openrouter"),
    ("xai/grok-4", "xai"),
    ("google/gemini-2.5-flash", "google"),
    ("vllm/qwen-72b", "vllm"),
])
@pytest.mark.parametrize("schema_compression_enabled", [True, False])
@pytest.mark.parametrize("content_mode", ["null", "omitted"])
def test_tool_schema_setting_controls_all_routed_provider_wires(
    tmp_path, monkeypatch, model, provider, schema_compression_enabled, content_mode,
):
    """AC-T4: translated provider wire respects the schema setting exactly."""
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv(
        "TOOL_SCHEMA_COMPRESSION_ENABLED",
        "true" if schema_compression_enabled else "false",
    )
    get_settings.cache_clear()

    from proxy import main as main_mod

    transport = _CaptureTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=transport,
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup_weather",
            "description": "Look up weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        },
    }]
    assistant_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup_weather", "arguments": '{ "city": "Paris" }'},
    }
    try:
        with TestClient(main_mod.app) as client:
            main_mod.app.state.http_clients = {}
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer tool-test"},
                json={
                    "model": model,
                    "messages": [
                        ({"role": "assistant", **({"content": None} if content_mode == "null" else {}),
                          "tool_calls": [assistant_call]}),
                        {"role": "tool", "tool_call_id": "call_1",
                         "content": '{\n  "forecast": "sunny"\n}'},
                    ],
                    "tools": tools,
                    "tool_choice": "auto",
                },
            )
    finally:
        main_mod._client_factory = None
        get_settings.cache_clear()

    assert response.status_code == 200, (provider, response.text[:200])
    assert len(transport.requests) == 1
    raw = transport.requests[0].content
    wire = json.loads(raw)
    assert wire["tool_choice"] == "auto"
    assert wire["tools"]
    if provider == "anthropic":
        assert wire["tools"][0]["name"] == "lookup_weather"
        assert wire["messages"][0]["content"][0]["id"] == "call_1"
    else:
        assert wire["messages"][0]["content"] == ""
        assert wire["tools"] == tools
        assert wire["messages"][0]["tool_calls"] == [assistant_call]

    if schema_compression_enabled:
        assert b'"tools":[' in raw
    else:
        assert b'"tools": [' in raw
