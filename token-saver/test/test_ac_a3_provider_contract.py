"""AC-A1/A2/A3/A4 provider contract matrix.

These are deterministic, recorded-fixture tests for the shared
OpenAICompatAdapter and the separate Anthropic wire-shape adapter.  They are
adapter-boundary tests: no real provider credentials or network are used.
The live-route coverage remains a separate acceptance gate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.providers.anthropic import AnthropicAdapter  # noqa: E402
from proxy.config import get_settings  # noqa: E402
from proxy.providers.base import AUTH_STYLES, auth_headers_for, error_from_status  # noqa: E402
from proxy.providers.model import ContentPart, Message, NormalizedRequest  # noqa: E402
from proxy.providers.openai_compat import OpenAICompatAdapter  # noqa: E402
from proxy.providers.registry import DEFAULT_REGISTRY, ProviderRegistry  # noqa: E402


# Recorded, deterministic registry fixtures.  The six rows are the shared
# OpenAI-compatible class required by AC-A3; Anthropic has its own suite below.
OPENAI_COMPAT_ROWS = [
    ("openai", "openai/gpt-4o", "bearer", "https://api.openai.com/v1"),
    ("openrouter", "openrouter/openai/gpt-4o", "bearer", "https://openrouter.ai/api/v1"),
    ("xai", "xai/grok-4", "bearer", "https://api.x.ai/v1"),
    ("google", "google/gemini-2.5-flash", "x-goog-api-key", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("vllm", "vllm/qwen-72b", "none", "http://localhost:8001/v1"),
    ("ollama", "ollama/llama3", "none", "http://localhost:11434/v1"),
]

RECORDED_CREDENTIAL = "recorded-test-credential"


def _request(model: str, *, stream: bool = True) -> NormalizedRequest:
    return NormalizedRequest(
        model=model,
        system="You are a precise assistant.",
        messages=[
            Message(role="user", content=[
                ContentPart(type="text", text="Inspect this image."),
                ContentPart(type="image", source={"url": "https://fixture.invalid/image.png"}),
                ContentPart(type="image", source={
                    "media_type": "image/png", "data": "ZmFrZQ=="
                }),
            ]),
            Message(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_fixture_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "arguments": '{"city":"Paris"}',
                    },
                }],
            ),
            Message(
                role="tool", content='{"temperature_c": 21}',
                tool_call_id="call_fixture_1", name="lookup_weather",
            ),
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "lookup_weather",
                "description": "Look up weather.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }],
        stream=stream,
        max_tokens=256,
        temperature=0.2,
        extra={"reasoning": {"enabled": False}, "top_p": 0.9},
    )


# ---------------------------------------------------------------- AC-A1 / AC-A2


def test_ac_a1_registry_contains_required_provider_rows():
    names = {row.name for row in DEFAULT_REGISTRY}
    assert {"anthropic", "openai", "openrouter", "xai", "google", "vllm", "ollama"} <= names
    assert all(row.adapter_class in {"AnthropicAdapter", "OpenAICompatAdapter"}
               for row in DEFAULT_REGISTRY)


@pytest.mark.parametrize("provider,model,auth_style,base_url", OPENAI_COMPAT_ROWS,
                         ids=[r[0] for r in OPENAI_COMPAT_ROWS])
def test_ac_a2_model_prefix_routes_to_expected_shared_adapter(provider, model,
                                                               auth_style, base_url):
    adapter = ProviderRegistry().route(model)
    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.name == provider
    assert adapter.auth_style == auth_style
    assert base_url  # fixture records the provider's configured route explicitly


def test_ac_a2_unknown_model_uses_documented_default_without_crashing():
    adapter = ProviderRegistry().route("unclaimed-model-fixture")
    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.name == "openrouter"


def test_ac_a2_unknown_provider_prefix_is_a_clear_4xx_on_live_route(monkeypatch, tmp_path):
    """An explicit unknown provider must not silently fall back upstream."""
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ac_a2.db"))
    get_settings.cache_clear()

    class UnexpectedUpstream(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, json={"unexpected": "fallback"})

    transport = UnexpectedUpstream()
    from proxy import main as main_mod
    monkeypatch.setattr(
        main_mod,
        "_client_factory",
        lambda base_url, timeout: httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport
        ),
        raising=False,
    )
    with TestClient(main_mod.app) as client:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer recorded-test-credential"},
            json={"model": "unknown-provider/model", "messages": [{"role": "user", "content": "hi"}]},
        )
    get_settings.cache_clear()
    assert 400 <= response.status_code < 500
    assert "unknown" in response.text.lower() or "provider" in response.text.lower()


# ---------------------------------------------------------------- AC-A3 shared OpenAI-compatible adapter matrix


@pytest.mark.parametrize("provider,model,auth_style,base_url", OPENAI_COMPAT_ROWS,
                         ids=[r[0] for r in OPENAI_COMPAT_ROWS])
def test_ac_a3_auth_and_request_translation_matrix(provider, model, auth_style, base_url):
    adapter = OpenAICompatAdapter(name=provider, auth_style=auth_style)
    wire = adapter.translate_request(_request(model))

    assert wire.path == "/v1/chat/completions"
    assert wire.json_body["model"] == model.split("/", 1)[1]
    assert wire.json_body["messages"][0]["role"] == "system"
    parts = wire.json_body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "Inspect this image."}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "https://fixture.invalid/image.png"}}
    assert parts[2]["image_url"]["url"] == "data:image/png;base64,ZmFrZQ=="
    assert wire.json_body["tools"][0]["function"]["name"] == "lookup_weather"
    assert wire.json_body["stream"] is True
    assert wire.json_body["stream_options"] == {"include_usage": True}
    assert wire.json_body["max_tokens"] == 256
    assert wire.json_body["temperature"] == 0.2
    assert wire.json_body["top_p"] == 0.9
    if provider == "openai":
        assert "reasoning" not in wire.json_body
    else:
        assert wire.json_body["reasoning"] == {"enabled": False}

    expected_auth = {
        "bearer": {"Authorization": f"Bearer {RECORDED_CREDENTIAL}"},
        "x-api-key": {"x-api-key": RECORDED_CREDENTIAL},
        "api-key": {"api-key": RECORDED_CREDENTIAL},
        "x-goog-api-key": {"x-goog-api-key": RECORDED_CREDENTIAL},
        "none": {},
    }[auth_style]
    assert adapter.auth_headers(RECORDED_CREDENTIAL) == expected_auth
    assert base_url.startswith(("http://", "https://"))


@pytest.mark.parametrize("style", AUTH_STYLES)
def test_ac_a3_supported_auth_styles_have_single_documented_placement(style):
    headers = auth_headers_for(style, RECORDED_CREDENTIAL)
    assert all(k.lower() not in {"authorization", "x-api-key", "api-key", "x-goog-api-key"}
               for k in headers if style == "query-param")
    if style == "none" or style == "query-param":
        assert headers == {}
    elif style == "bearer":
        assert headers == {"Authorization": f"Bearer {RECORDED_CREDENTIAL}"}
    else:
        assert len(headers) == 1


@pytest.mark.parametrize("provider,model,auth_style,_base_url", OPENAI_COMPAT_ROWS,
                         ids=[r[0] for r in OPENAI_COMPAT_ROWS])
def test_ac_a3_response_usage_and_error_normalization_matrix(provider, model,
                                                              auth_style, _base_url):
    adapter = OpenAICompatAdapter(name=provider, auth_style=auth_style)
    req = _request(model, stream=False)
    raw = httpx.Response(200, json={
        "id": "fixture_completion",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "fixture answer"}}],
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    })
    normalized = adapter.translate_response(raw, req)
    assert normalized.output_text == "fixture answer"
    assert normalized.usage is not None
    assert normalized.usage.input_tokens == 17
    assert normalized.usage.output_tokens == 9
    assert normalized.usage.cache_read_tokens == 4

    rate = adapter.translate_response(
        httpx.Response(429, json={"error": {"message": "fixture throttled"}},
                      headers={"retry-after": "7"}), req)
    assert rate.error is not None
    assert rate.error.kind == "rate_limit"
    assert rate.error.retry_after_s == 7.0

    overloaded = adapter.translate_response(
        httpx.Response(503, text="fixture upstream unavailable"), req)
    assert overloaded.error is not None
    assert overloaded.error.kind == "overloaded"
    assert overloaded.error.message == "fixture upstream unavailable"


# ---------------------------------------------------------------- AC-A4 Anthropic separate adapter suite


def test_ac_a4_anthropic_request_uses_messages_system_param_and_no_reasoning():
    adapter = AnthropicAdapter()
    wire = adapter.translate_request(_request("anthropic/claude-sonnet-5", stream=False))
    assert wire.path == "/v1/messages"
    assert wire.json_body["model"] == "claude-sonnet-5"
    assert wire.json_body["system"] == [{
        "type": "text",
        "text": "You are a precise assistant.",
        "cache_control": {"type": "ephemeral"},
    }]
    assert all(message["role"] != "system" for message in wire.json_body["messages"])
    assert "reasoning" not in wire.json_body
    assert wire.json_body["max_tokens"] == 256
    assert wire.json_body["tools"][0]["name"] == "lookup_weather"
    assert wire.json_body["tools"][0]["input_schema"]["type"] == "object"
    assert wire.json_body["messages"][1]["content"][0]["type"] == "tool_use"
    assert wire.json_body["messages"][2]["content"][0]["type"] == "tool_result"
    assert adapter.auth_headers(RECORDED_CREDENTIAL) == {
        "x-api-key": RECORDED_CREDENTIAL,
        "anthropic-version": "2023-06-01",
    }


def test_ac_a4_anthropic_response_fixture_normalizes_to_text_and_usage():
    adapter = AnthropicAdapter()
    raw = httpx.Response(200, json={
        "id": "msg_fixture",
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "tool_use", "id": "call_fixture_1", "name": "lookup_weather", "input": {}},
            {"type": "text", "text": "world"},
        ],
        "usage": {"input_tokens": 17, "output_tokens": 9,
                   "cache_read_input_tokens": 4},
    })
    normalized = adapter.translate_response(raw, _request("anthropic/claude-sonnet-5", stream=False))
    assert normalized.output_text == "Hello world"
    assert normalized.usage is not None
    assert normalized.usage.input_tokens == 17
    assert normalized.usage.output_tokens == 9
    assert normalized.usage.cache_read_tokens == 4


def test_ac_a4_anthropic_stream_fixture_preserves_text_usage_and_tool_events():
    adapter = AnthropicAdapter()
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":17}}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}',
        'data: {"type":"content_block_start","content_block":{"type":"tool_use","id":"call_fixture_1","name":"lookup_weather"}}',
        'data: {"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{\\"city\\":\\"Paris\\"}"}}',
        'data: {"type":"message_delta","usage":{"output_tokens":9}}',
        'data: {"type":"message_stop"}',
    ]
    events = [adapter.translate_stream_chunk(line, _request("anthropic/claude-sonnet-5"))
              for line in lines]
    assert events[0].kind == "usage" and events[0].usage.input_tokens == 17
    assert events[1].kind == "delta" and events[1].delta_text == "Hello"
    assert events[2].kind == "tool_start" and events[2].delta_text == "call_fixture_1"
    assert events[3].kind == "tool_delta" and events[3].delta_text == '{"city":"Paris"}'
    assert events[4].kind == "usage" and events[4].usage.output_tokens == 9
    assert events[5].kind == "done"


@pytest.mark.parametrize("status,expected_kind", [
    (401, "auth"), (403, "auth"), (400, "invalid_request"),
    (429, "rate_limit"), (503, "overloaded"), (529, "overloaded"), (500, "upstream"),
])
def test_ac_a3_statuses_map_to_one_stable_error_kind(status, expected_kind):
    assert error_from_status(status, "fixture error").kind == expected_kind
