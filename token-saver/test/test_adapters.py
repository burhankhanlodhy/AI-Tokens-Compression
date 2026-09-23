"""PA-1 adapter unit tests: routing, auth placement, request translation,
response/stream translation, error normalization. Fixtures are inline here;
QA's contract-matrix suite (C1-C10) extends these per provider."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.providers import (  # noqa: E402
    Message,
    NormalizedRequest,
    ContentPart,
    ProviderRegistry,
)
from proxy.providers.anthropic import AnthropicAdapter  # noqa: E402
from proxy.providers.base import auth_headers_for, error_from_status  # noqa: E402
from proxy.providers.openai_compat import OpenAICompatAdapter  # noqa: E402
from proxy.providers.registry import (  # noqa: E402
    DEFAULT_REGISTRY,
    ProviderRegistry,
    ProviderRow,
)


# ---------------------------------------------------------------- C1 routing

@pytest.mark.parametrize("model,expected_name", [
    ("anthropic/claude-sonnet-5", "anthropic"),
    ("claude-sonnet-5", "anthropic"),
    ("openai/gpt-4o", "openai"),
    ("gpt-4o-mini", "openai"),
    ("openrouter/z-ai/glm-5.3-flash", "openrouter"),
    ("xai/grok-4", "xai"),
    ("grok-3", "xai"),
    ("google/gemini-2.5-flash", "google"),
    ("gemini-2.5-flash", "google"),
    ("ollama/llama3", "ollama"),
    ("vllm/qwen-72b", "vllm"),
    ("some-unknown-model", "openrouter"),  # default provider
])
def test_route_by_model_prefix(model, expected_name):
    reg = ProviderRegistry()
    assert reg.route(model).name == expected_name


def test_route_override_wins():
    reg = ProviderRegistry()
    assert reg.route("gpt-4o", override="openrouter").name == "openrouter"


def test_route_disabled_provider_does_not_silently_reroute():
    """AC-A2: a known-but-disabled provider returns None instead of silently
    rerouting its models to the default provider; an explicit unknown
    provider slug also returns None (clear 4xx upstream of dispatch)."""
    rows = [r for r in DEFAULT_REGISTRY if r.name != "anthropic"]
    reg = ProviderRegistry(rows=rows)
    assert reg.route("claude-3-5-sonnet") is None
    assert reg.route("anthropic/claude-sonnet-5") is None
    assert reg.route("unknown-provider/model") is None
    # bare, unclaimed model names still use the documented default
    assert reg.route("some-unknown-model").name == "openrouter"


# ------------------------------------- B-24 config-row providers (AC-A1 x AC-A2)

def test_config_row_provider_routes_to_its_own_adapter():
    """A config-added (row-only) provider routes to its own adapter, not the
    default provider — the AC-A1 'no code change' path must actually work."""
    row = ProviderRow("myconfigprovider", "https://myhost.example/v1",
                      "OpenAICompatAdapter", "bearer")
    reg = ProviderRegistry(rows=DEFAULT_REGISTRY + [row])
    adapter = reg.route("myconfigprovider/mistral-small")
    assert adapter is not None and adapter.name == "myconfigprovider"
    assert reg.base_url_for("myconfigprovider") == "https://myhost.example/v1"


def test_disabled_config_row_does_not_silently_reroute():
    """B-24: disabling a config-added row returns None from route() — never
    a silent reroute to the default provider (AC-A2 in both directions)."""
    row = ProviderRow("myconfigprovider", "https://myhost.example/v1",
                      "OpenAICompatAdapter", "bearer", enabled=False)
    reg = ProviderRegistry(rows=DEFAULT_REGISTRY + [row])
    assert reg.route("myconfigprovider/mistral-small") is None


def test_anthropic_class_config_row_keeps_row_name():
    """B-24: AnthropicAdapter no longer hardcodes the built-in name — a row
    named 'corp-anthropic' routes and attributes under its own name."""
    row = ProviderRow("corp-anthropic", "https://corp-gw.example",
                      "AnthropicAdapter", "x-api-key")
    reg = ProviderRegistry(rows=DEFAULT_REGISTRY + [row])
    adapter = reg.route("corp-anthropic/claude-sonnet-5")
    assert adapter is not None and adapter.name == "corp-anthropic"
    assert adapter.messages_path == "/v1/messages"


# ---------------------------------------------------------------- C2 auth

@pytest.mark.parametrize("style,expected", [
    ("bearer", {"Authorization": "Bearer sk-test"}),
    ("x-api-key", {"x-api-key": "sk-test"}),
    ("api-key", {"api-key": "sk-test"}),
    ("x-goog-api-key", {"x-goog-api-key": "sk-test"}),
    ("none", {}),
])
def test_auth_header_placement(style, expected):
    assert auth_headers_for(style, "sk-test") == expected


def test_anthropic_auth_has_version_header():
    h = AnthropicAdapter().auth_headers("sk-ant-test")
    assert h["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in h


# ---------------------------------------------------------------- C3 request translation

def _norm(stream=False, tools=None, extra=None, content=None):
    return NormalizedRequest(
        model="anthropic/claude-sonnet-5" if stream or True else "x",
        messages=[Message(role="user", content=content or "hello")],
        system="be brief",
        tools=tools,
        stream=stream,
        max_tokens=512,
        temperature=0.5,
        extra=extra or {},
    )


def test_openai_translation_shape():
    a = OpenAICompatAdapter(name="openai")
    w = a.translate_request(_norm())
    assert w.path == "/v1/chat/completions"
    assert w.json_body["model"] == "claude-sonnet-5"
    assert w.json_body["messages"][0] == {"role": "system", "content": "be brief"}
    assert w.json_body["max_tokens"] == 512
    assert "stream" not in w.json_body


def test_openai_stream_options_included():
    a = OpenAICompatAdapter(name="openai")
    w = a.translate_request(_norm(stream=True))
    assert w.json_body["stream"] is True
    assert w.json_body["stream_options"] == {"include_usage": True}


def test_openai_multimodal_base64_and_url():
    a = OpenAICompatAdapter(name="openai")
    req = NormalizedRequest(model="gpt-4o", messages=[Message(role="user", content=[
        ContentPart(type="text", text="what is this"),
        ContentPart(type="image", source={"url": "https://x/img.png"}),
        ContentPart(type="image", source={"media_type": "image/jpeg", "data": "abc123"}),
    ])])
    w = a.translate_request(req)
    parts = w.json_body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    assert parts[1]["image_url"]["url"] == "https://x/img.png"
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,abc123")


def test_anthropic_system_as_param_and_max_tokens():
    a = AnthropicAdapter()
    w = a.translate_request(_norm())
    assert w.path == "/v1/messages"
    assert w.json_body["system"] == "be brief"
    assert w.json_body["max_tokens"] == 512
    # system must NOT appear inside messages
    assert all(m["role"] != "system" for m in w.json_body["messages"])


def test_anthropic_default_max_tokens_when_none():
    a = AnthropicAdapter()
    req = _norm()
    req.max_tokens = None
    w = a.translate_request(req)
    assert w.json_body["max_tokens"] == 4096


def test_anthropic_tool_call_arguments_are_object():
    req = _norm()
    req.messages = [Message(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "call_weather",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": '{"city":"Paris","units":"celsius"}',
            },
        }],
    )]

    wire = AnthropicAdapter().translate_request(req)

    tool_use = wire.json_body["messages"][0]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["input"] == {"city": "Paris", "units": "celsius"}
    assert isinstance(tool_use["input"], dict)


def test_anthropic_tool_call_malformed_arguments_fall_back_to_empty_object():
    req = _norm()
    req.messages = [Message(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "call_weather",
            "type": "function",
            "function": {"name": "get_weather", "arguments": "not-json"},
        }],
    )]

    wire = AnthropicAdapter().translate_request(req)

    assert wire.json_body["messages"][0]["content"][0]["input"] == {}


def test_anthropic_tool_translation():
    a = AnthropicAdapter()
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "w", "parameters": {"type": "object"}}}]
    w = a.translate_request(_norm(tools=tools))
    # AC-A4: OpenAI `parameters` must be renamed to Anthropic `input_schema`
    assert w.json_body["tools"] == [{"name": "get_weather", "description": "w",
                                     "input_schema": {"type": "object"}}]


def test_anthropic_tool_translation_is_idempotent():
    """AC-A4: already-Anthropic tool definitions pass through unchanged, and
    OpenAI-only keys (strict) are dropped from wrapped definitions."""
    a = AnthropicAdapter()
    anthropic_tool = {"name": "get_weather", "description": "w",
                      "input_schema": {"type": "object"}}
    w = a.translate_request(_norm(tools=[anthropic_tool]))
    assert w.json_body["tools"] == [anthropic_tool]
    wrapped = [{"type": "function", "function": {
        "name": "get_weather", "description": "w", "strict": True,
        "parameters": {"type": "object"}}}]
    w2 = a.translate_request(_norm(tools=wrapped))
    assert w2.json_body["tools"] == [{"name": "get_weather", "description": "w",
                                      "input_schema": {"type": "object"}}]


# ---------------------------------------------------------------- C4 response translation

def test_openai_response_usage_and_text():
    a = OpenAICompatAdapter(name="openai")
    raw = httpx.Response(200, json={"choices": [{"message": {"content": "hi there"}}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                              "prompt_tokens_details": {"cached_tokens": 4}}})
    n = a.translate_response(raw, _norm())
    assert n.output_text == "hi there"
    assert n.usage.input_tokens == 10
    assert n.usage.output_tokens == 5
    assert n.usage.cache_read_tokens == 4


def test_anthropic_response_usage_with_cache_fields():
    a = AnthropicAdapter()
    raw = httpx.Response(200, json={"content": [{"type": "text", "text": "hey"}],
                                    "usage": {"input_tokens": 20, "output_tokens": 8,
                                              "cache_read_input_tokens": 15,
                                              "cache_creation_input_tokens": 2}})
    n = a.translate_response(raw, _norm())
    assert n.output_text == "hey"
    assert n.usage.input_tokens == 20
    assert n.usage.cache_read_tokens == 15
    assert n.usage.cache_write_tokens == 2


# ---------------------------------------------------------------- C8 error normalization

@pytest.mark.parametrize("status,kind", [
    (401, "auth"), (403, "auth"), (429, "rate_limit"),
    (503, "overloaded"), (400, "invalid_request"), (500, "upstream"),
])
def test_error_kind_mapping(status, kind):
    assert error_from_status(status, "m").kind == kind


def test_openai_error_extraction():
    a = OpenAICompatAdapter(name="openai")
    raw = httpx.Response(429, json={"error": {"message": "slow down"}},
                         headers={"retry-after": "12"})
    n = a.translate_response(raw, _norm())
    assert n.error.kind == "rate_limit"
    assert n.error.message == "slow down"
    assert n.error.retry_after_s == 12.0


def test_anthropic_error_extraction():
    a = AnthropicAdapter()
    raw = httpx.Response(529, json={"error": {"type": "overloaded_error",
                                              "message": "Overloaded"}})
    n = a.translate_response(raw, _norm())
    assert n.error.kind == "overloaded"
    assert n.error.message == "Overloaded"


# ---------------------------------------------------------------- C4/C5 streaming

def test_openai_stream_deltas_and_usage():
    a = OpenAICompatAdapter(name="openai")
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    events = [a.translate_stream_chunk(l, _norm(stream=True)) for l in lines]
    text = "".join(e.delta_text for e in events if e.kind == "delta")
    assert text == "Hello"
    assert events[2].kind == "usage" and events[2].usage.output_tokens == 2
    assert events[3].kind == "done"


def test_openai_stream_ollama_usage_only_chunk():
    """Ollama quirk: usage chunk with no choices must not crash or duplicate text."""
    a = OpenAICompatAdapter(name="ollama", auth_style="none")
    e = a.translate_stream_chunk('data: {"usage":{"prompt_tokens":3,"completion_tokens":1}}',
                                 _norm(stream=True))
    assert e.kind == "usage"
    assert e.usage.input_tokens == 3


def test_anthropic_stream_events():
    a = AnthropicAdapter()
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":25,"output_tokens":0}}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"message_delta","usage":{"output_tokens":9}}',
        'data: {"type":"message_stop"}',
    ]
    events = [a.translate_stream_chunk(l, _norm(stream=True)) for l in lines]
    assert events[0].kind == "usage" and events[0].usage.input_tokens == 25
    assert events[1].delta_text == "Hi"
    assert events[2].usage.output_tokens == 9
    assert events[3].kind == "done"


def test_anthropic_stream_error_event():
    a = AnthropicAdapter()
    e = a.translate_stream_chunk('data: {"type":"error","error":{"type":"overloaded_error","message":"x"}}',
                                 _norm(stream=True))
    assert e.kind == "error" and e.error.kind == "overloaded_error"
