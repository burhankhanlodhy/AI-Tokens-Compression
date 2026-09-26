"""Release-A V2.3 quick-win regressions.

These tests exercise the user-approved plan's three application-side changes:
pricing aliases, cache exclusion for tool-bearing requests, and content-derived
TOCP identifiers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import config  # noqa: E402
from proxy.config import get_settings  # noqa: E402
from proxy.tocp import ContinuationStore  # noqa: E402


class _CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )


def test_price_lookup_normalizes_openrouter_prefix_and_vendorless_alias():
    """V2.3 quick win #1: provider-returned model aliases get the shipped rate."""
    config._pricing_cache = None
    config._pricing_warned.clear()
    expected = config.estimate_cost("z-ai/glm-5.3-flash", 1_000_000, 0)
    assert expected != 0.50  # the known fallback that made ledger savings wrong
    assert config.estimate_cost("openrouter/z-ai/glm-5.3-flash", 1_000_000, 0) == expected
    assert config.estimate_cost("glm-5.3-flash", 1_000_000, 0) == expected


def test_tool_bearing_request_bypasses_exact_and_semantic_response_caches(tmp_path, monkeypatch):
    """V2.3 quick win #2: tools never trigger response-cache work or replay."""
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_CACHE_MAX_COSINE_DISTANCE", "0.1")
    get_settings.cache_clear()

    from proxy import main as main_mod

    calls = {"exact_lookup": 0, "exact_record": 0, "embedding": 0, "semantic_lookup": 0}

    def exact_lookup(*_args, **_kwargs):
        calls["exact_lookup"] += 1
        return None

    def exact_record(*_args, **_kwargs):
        calls["exact_record"] += 1

    async def embedding(*_args, **_kwargs):
        calls["embedding"] += 1
        return [1.0]

    def semantic_lookup(*_args, **_kwargs):
        calls["semantic_lookup"] += 1
        return main_mod.semantic_cache.SemanticLookupResult.not_attempted()

    # Exercise both response-cache branches even though the production semantic
    # cache is normally release-gated off.
    monkeypatch.setattr(main_mod.caching, "lookup", exact_lookup)
    monkeypatch.setattr(main_mod.caching, "record", exact_record)
    monkeypatch.setattr(main_mod, "acquire_embedding", embedding)
    monkeypatch.setattr(main_mod.semantic_cache, "lookup_result", semantic_lookup)
    monkeypatch.setattr(
        main_mod,
        "get_settings_store",
        lambda: SimpleNamespace(snapshot=lambda: {
            "l1_enabled": False,
            "tool_schema_minify": False,
            "tool_schema_cache_enabled": True,
            "tool_result_optimization": False,
            "tool_result_compression_enabled": False,
            "output_conciseness_enabled": False,
            "semantic_cache_enabled": True,
        }),
    )

    transport = _CaptureTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=transport,
    )
    try:
        with TestClient(main_mod.app) as client:
            main_mod.app.state.http_clients = {}
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer tool-test"},
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "Use the supplied tool."}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "lookup_weather",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }],
                },
            )
    finally:
        main_mod._client_factory = None
        get_settings.cache_clear()

    assert response.status_code == 200, response.text
    assert len(transport.requests) == 1
    assert calls == {name: 0 for name in calls}


def test_tocp_id_is_stable_for_identical_scoped_content_and_changes_with_scope_or_content():
    """V2.3 quick win #3: stable IDs permit deterministic continuation replay."""
    store = ContinuationStore(ttl_seconds=60, max_entries=10, segment_chars=8)
    first = store.save("tenant-a", "session-a", "compiler output\nline 2")
    identical = store.save("tenant-a", "session-a", "compiler output\nline 2")
    changed_content = store.save("tenant-a", "session-a", "compiler output\nline 3")
    changed_scope = store.save("tenant-a", "session-b", "compiler output\nline 2")

    assert identical.continuation_id == first.continuation_id
    assert changed_content.continuation_id != first.continuation_id
    assert changed_scope.continuation_id != first.continuation_id
    assert store.size == 3
    assert store.get_segment(first.continuation_id, "tenant-a", "session-a", 0) == "compiler"
