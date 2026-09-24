"""V1.2.1-T3 schema-minification, cache, and proxy-pipeline coverage."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import stats
from proxy.config import get_settings


VERBOSE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "lookup_account",
        "description": "Look up an account by its email address.",
        "parameters": {
            "type": "object",
            "description": "Inputs accepted by this function.",
            "properties": {
                "email": {
                    "type": "string",
                    "format": "email",
                    "description": "User email address",
                },
                "api_key": {
                    "type": "string",
                    "minLength": 12,
                    "description": "API credential for the Acme billing service.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["brief", "full"],
                    "description": "Controls how much account history is returned.",
                },
            },
            "required": ["email", "api_key"],
            "additionalProperties": False,
        },
    },
}]


def test_minify_tool_schema_removes_only_redundant_parameter_description():
    from proxy.tool_protocol import minify_tool_schema

    minified = minify_tool_schema(VERBOSE_TOOLS[0])
    properties = minified["function"]["parameters"]["properties"]

    assert "description" not in properties["email"]
    assert properties["api_key"]["description"] == (
        "API credential for the Acme billing service."
    )
    assert properties["mode"]["description"] == (
        "Controls how much account history is returned."
    )
    assert minified["function"]["description"] == VERBOSE_TOOLS[0]["function"]["description"]
    assert minified["function"]["parameters"]["required"] == ["email", "api_key"]
    assert properties["api_key"]["minLength"] == 12
    assert properties["mode"]["enum"] == ["brief", "full"]
    assert properties["email"]["format"] == "email"
    assert VERBOSE_TOOLS[0]["function"]["parameters"]["properties"]["email"]["description"] == (
        "User email address"
    )


def test_schema_cache_compresses_once_then_returns_isolated_cached_copies():
    from proxy.tool_protocol import SchemaCache

    cache = SchemaCache()
    for request_number in range(5):
        minified, cache_hit = cache.get_or_compress(VERBOSE_TOOLS)
        assert cache_hit is (request_number > 0)
        minified[0]["function"]["parameters"]["properties"]["email"]["type"] = "mutated"

    final, cache_hit = cache.get_or_compress(VERBOSE_TOOLS)
    assert cache_hit is True
    assert final[0]["function"]["parameters"]["properties"]["email"]["type"] == "string"
    assert cache.compression_count == 1
    assert cache.cache_hits == 5


def test_schema_cache_evicts_old_entries_and_skips_unchanged_schemas():
    from proxy.tool_protocol import SchemaCache

    cache = SchemaCache(max_entries=2, max_bytes=10_000)
    for name in ("lookup_one", "lookup_two", "lookup_three"):
        tools = [{
            "type": "function",
            "function": {
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Email address"},
                    },
                },
            },
        }]
        _, cache_hit = cache.get_or_compress(tools)
        assert cache_hit is False

    assert cache.cached_entries == 2
    assert cache.cached_bytes > 0
    _, evicted = cache.get_or_compress([{
        "type": "function",
        "function": {
            "name": "lookup_one",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Email address"},
                },
            },
        },
    }])
    assert evicted is False

    unchanged = [{
        "type": "function",
        "function": {
            "name": "already_compact",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
    }]
    _, cache_hit = cache.get_or_compress(unchanged)
    assert cache_hit is False
    assert cache.cached_entries == 2


def test_schema_cache_respects_lru_recency_and_byte_cap():
    from proxy.tool_protocol import SchemaCache

    def tools(name: str) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Email address"},
                    },
                },
            },
        }]

    cache = SchemaCache(max_entries=2, max_bytes=10_000)
    cache.get_or_compress(tools("one"))
    cache.get_or_compress(tools("two"))
    _, cache_hit = cache.get_or_compress(tools("one"))
    assert cache_hit is True
    cache.get_or_compress(tools("three"))
    _, cache_hit = cache.get_or_compress(tools("one"))
    assert cache_hit is True
    _, cache_hit = cache.get_or_compress(tools("two"))
    assert cache_hit is False

    byte_capped = SchemaCache(max_entries=2, max_bytes=1)
    byte_capped.get_or_compress(tools("too_large"))
    assert byte_capped.cached_entries == 0
    assert byte_capped.cached_bytes == 0


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    get_settings.cache_clear()
    stats.init_db()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def schema_client(tmp_db, monkeypatch):
    from proxy import main

    main.schema_cache.clear()
    monkeypatch.setattr(main, "_reasoning_mandatory_models", set())
    captured_bodies: list[dict] = []
    captured_payloads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(request.content)
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-schema", "object": "chat.completion", "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        })

    main.app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test/v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        yield client, captured_bodies, captured_payloads
    await main.app.state.http.aclose()


@pytest.mark.asyncio
async def test_repeated_schema_requests_hit_cache_and_record_ledger(schema_client, monkeypatch):
    client, captured_bodies, captured_payloads = schema_client
    monkeypatch.setenv("L1_ENABLED", "false")
    monkeypatch.setenv("CODEBASE_OPTIMIZATION_ENABLED", "false")
    get_settings.cache_clear()
    from proxy import main
    from proxy import l1_clean
    monkeypatch.setattr(main, "compress_messages", lambda messages: messages)
    monkeypatch.setattr(l1_clean, "clean_messages", lambda messages, **kwargs: messages)
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "find this account"}],
        "tools": VERBOSE_TOOLS,
    }

    for _ in range(10):
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json=payload,
        )
        assert response.status_code == 200

    assert len(captured_bodies) == 10
    assert all(b'"tools":[' in payload for payload in captured_payloads)
    assert all(
        "description" not in body["tools"][0]["function"]["parameters"]["properties"]["email"]
        for body in captured_bodies
    )
    assert all(
        body["tools"][0]["function"]["parameters"]["properties"]["api_key"]["description"]
        == "API credential for the Acme billing service."
        for body in captured_bodies
    )

    from proxy import main
    assert main.schema_cache.compression_count == 1
    assert main.schema_cache.cache_hits == 9
    with stats.get_conn() as conn:
        rows = conn.execute(
            "SELECT schema_cache_hit, schema_bytes_saved FROM requests ORDER BY id"
        ).fetchall()
    assert len(rows) == 10
    assert [row["schema_cache_hit"] for row in rows] == [0] + [1] * 9
    assert all(row["schema_bytes_saved"] > 0 for row in rows)
    with stats.get_conn() as conn:
        ledger_rows = conn.execute(
            "SELECT input_tokens_before, input_tokens_after, tool_compression_saved "
            "FROM requests ORDER BY id"
        ).fetchall()
    from proxy.tool_protocol import estimate_schema_token_savings

    schema_delta = estimate_schema_token_savings(
        VERBOSE_TOOLS, captured_bodies[0]["tools"], "gpt-4o-mini"
    )
    assert all(body["messages"] == payload["messages"] for body in captured_bodies)
    assert schema_delta > 0
    assert all(
        row["input_tokens_before"] > row["input_tokens_after"]
        for row in ledger_rows
    )
    assert all(row["tool_compression_saved"] > 0 for row in ledger_rows)
    assert all(
        row["tool_compression_saved"]
        <= max(0, row["input_tokens_before"] - row["input_tokens_after"])
        for row in ledger_rows
    )


def test_tool_schema_token_estimate_never_exceeds_removed_utf8_bytes():
    from proxy.tool_protocol import (
        compact_schema_bytes,
        estimate_schema_token_savings,
        minify_tool_schema,
    )

    schemas = [VERBOSE_TOOLS] + [
        [{"type": "function", "function": {"name": "f", "description": "🙂" * n,
          "parameters": {"type": "object", "properties": {"email": {
              "type": "string", "description": "Email"}}}}}]
        for n in range(0, 25)
    ]
    for tools in schemas:
        minified = [minify_tool_schema(tool) for tool in tools]
        removed_bytes = max(0, compact_schema_bytes(tools) - compact_schema_bytes(minified))
        estimated_tokens = estimate_schema_token_savings(
            tools, minified, "gpt-4o-mini"
        )
        assert estimated_tokens <= removed_bytes


@pytest.mark.asyncio
async def test_schema_minification_can_be_disabled(schema_client, monkeypatch):
    client, captured_bodies, _ = schema_client
    monkeypatch.setenv("TOOL_SCHEMA_MINIFY", "false")
    get_settings.cache_clear()

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "find this account"}],
            "tools": VERBOSE_TOOLS,
        },
    )

    assert response.status_code == 200
    assert (
        captured_bodies[0]["tools"][0]["function"]["parameters"]["properties"]
        ["email"]["description"]
        == "User email address"
    )

    from proxy import main

    assert main.schema_cache.compression_count == 0
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT schema_cache_hit, schema_bytes_saved FROM requests"
        ).fetchone()
    assert row["schema_cache_hit"] == 0
    assert row["schema_bytes_saved"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_flag", ["TOOL_SCHEMA_CACHE_ENABLED", "CACHE_ENABLED"])
async def test_schema_cache_can_be_disabled_without_disabling_minification(
    schema_client, monkeypatch, cache_flag
):
    client, captured_bodies, _ = schema_client
    monkeypatch.setenv(cache_flag, "false")
    get_settings.cache_clear()
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "find this account"}],
        "tools": VERBOSE_TOOLS,
    }

    for _ in range(2):
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json=payload,
        )
        assert response.status_code == 200

    assert all(
        "description" not in body["tools"][0]["function"]["parameters"]["properties"]["email"]
        for body in captured_bodies
    )
    from proxy import main

    assert main.schema_cache.compression_count == 2
    assert main.schema_cache.cache_hits == 0
