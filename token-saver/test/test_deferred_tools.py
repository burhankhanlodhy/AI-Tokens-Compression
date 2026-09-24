from __future__ import annotations

from proxy.deferred_tools import (
    DeferredToolSelector,
    provider_deferred_tool_support,
)


def tools():
    return [
        {"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {"name": "run_tests", "description": "Run the test suite", "parameters": {"type": "object", "properties": {"target": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "search_code", "description": "Search source code", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    ]


def test_catalog_is_compact_stable_and_preserves_tool_order():
    selector = DeferredToolSelector(tools())
    assert selector.catalog() == (
        '{"tools":[{"name":"read_file","description":"Read a file"},'
        '{"name":"run_tests","description":"Run the test suite"},'
        '{"name":"search_code","description":"Search source code"}]}'
    )
    assert selector.catalog() == selector.catalog()


def test_search_returns_exact_ranked_schemas_in_original_order():
    source = tools()
    selected, result = DeferredToolSelector(source).search("search source")
    assert [tool["function"]["name"] for tool in selected] == ["search_code"]
    assert selected[0] is source[2]
    assert result.status == "hit"
    assert result.miss_count == 0
    assert result.match_count == 1
    assert result.telemetry()["match_count"] == 1


def test_search_miss_falls_back_to_eager_schema_set():
    source = tools()
    selected, result = DeferredToolSelector(source).search("deploy production")
    assert selected == source
    assert result.status == "miss_fallback"
    assert result.miss_count == 1


def test_provider_compatibility_is_explicitly_conservative():
    for provider in ("openai", "anthropic", "openrouter", "xai", "google", "vllm", "ollama", "unknown"):
        assert provider_deferred_tool_support(provider) is False


def test_disabled_or_unsupported_selection_is_exact_eager_fallback():
    source = tools()
    selected, result = DeferredToolSelector(source).select(
        "search source", enabled=True, provider="openai"
    )
    assert selected is source
    assert result.status == "unsupported_fallback"
    selected, result = DeferredToolSelector(source).select(
        "search source", enabled=False, provider="future"
    )
    assert selected is source
    assert result.status == "disabled_fallback"


def test_search_errors_and_retries_are_counted_and_fail_open():
    source = tools()
    selector = DeferredToolSelector(source)
    selected, result = selector.search("", retries=2)
    assert selected is source
    assert result.status == "error_fallback"
    assert result.error_count == 1
    assert result.retry_count == 2
    assert result.telemetry()["error_count"] == 1
    assert result.telemetry()["retry_count"] == 2


def test_registered_adapter_compatibility_contracts_remain_eager():
    from proxy.providers.anthropic import AnthropicAdapter
    from proxy.providers.openai_compat import OpenAICompatAdapter
    from proxy.providers.model import NormalizedRequest

    source = tools()
    openai_req = NormalizedRequest(model="openai/gpt-4o", messages=[], tools=source)
    assert OpenAICompatAdapter(name="openai").translate_request(openai_req).json_body["tools"] is source

    anthropic_req = NormalizedRequest(model="anthropic/claude-sonnet-4", messages=[], tools=source)
    translated = AnthropicAdapter().translate_request(anthropic_req).json_body["tools"]
    assert [t["name"] for t in translated] == ["read_file", "run_tests", "search_code"]
    assert translated[0]["input_schema"] == source[0]["function"]["parameters"]
    assert translated[0]["input_schema"]["required"] == ["path"]


def test_client_header_cannot_turn_on_server_deferred_tool_flag():
    from proxy.config import Settings

    settings = Settings(_env_file=None)
    source = tools()
    selector = DeferredToolSelector(source)
    headers = {"X-Token-Saver-Deferred-Tools": "true"}
    selected, result = selector.select(
        "search source", enabled=settings.v21_deferred_tools_enabled, provider="openai"
    )
    assert headers  # adversarial header is present but ignored
    assert selected is source
    assert result.status == "disabled_fallback"
