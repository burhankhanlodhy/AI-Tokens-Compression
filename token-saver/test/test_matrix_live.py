"""PA-1 C1-C10 live-route coverage: PM-scoped final matrix items.

(1) auth_style variation through the live route for the remaining
    OpenAICompatAdapter providers (google=x-goog-api-key, ollama=none,
    plus openrouter/xai bearer rows) — full C1-C10 depth stays on the
    shared-adapter class per PM's scoping, since OpenAI already covers it.
(2) multimodal payload through the live route (C6).
(3) upstream timeout behavior through the live route (C7).

All tests drive the real /v1/chat/completions route with the
_client_factory capture-transport seam.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from test_live_routing import (  # noqa: E402
    PG_ADMIN_DSN,
    _CACHE_DB,
    _CaptureTransport,
    _seed_cache_db,
)
from proxy.config import get_settings  # noqa: E402


@pytest.fixture()
def routed(monkeypatch):
    """Provider routing on + throwaway cache DB + capture-transport hook."""
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/ts_matrix_test.db")
    try:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_CACHE_DB}")
            pg.execute(f"CREATE DATABASE {_CACHE_DB}")
    except psycopg.OperationalError:
        pytest.skip("TOKEN_SAVER_PG_BASE is unavailable for Postgres acceptance tests", allow_module_level=False)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", f"{PG_ADMIN_DSN}/{_CACHE_DB}")
    _seed_cache_db(f"{PG_ADMIN_DSN}/{_CACHE_DB}")
    get_settings.cache_clear()
    from proxy import main as main_mod
    yield main_mod
    main_mod._client_factory = None
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    get_settings.cache_clear()
    try:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_CACHE_DB}")
    except psycopg.OperationalError:
        pass


def _openai_ok() -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


# ------------------------------------- (1) auth_style live-route smoke matrix

@pytest.mark.parametrize("model,provider,expect_header,base_contains", [
    ("openrouter/openai/gpt-4o", "openrouter", "authorization", "openrouter.ai"),
    ("xai/grok-4", "xai", "authorization", "api.x.ai"),
    ("google/gemini-2.5-flash", "google", "x-goog-api-key", "generativelanguage"),
    ("ollama/llama3", "ollama", None, "localhost:11434"),
])
def test_auth_style_smoke(routed, model, provider, expect_header, base_contains):
    """C2 via live route per auth_style row of the shared adapter class."""
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-smoke"},
                   json={"model": model,
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, (provider, r.text[:200])
    req = cap.requests[0]
    # routed to the provider's configured base URL
    assert base_contains in str(req.url), str(req.url)
    headers = {k.lower(): v for k, v in req.headers.items()}
    if expect_header == "authorization":
        # credential placed as Bearer for bearer-style providers
        assert headers.get("authorization") == "Bearer sk-smoke", (provider, headers)
        assert "x-goog-api-key" not in headers
    elif expect_header == "x-goog-api-key":
        assert headers.get("x-goog-api-key") == "sk-smoke"
        assert "authorization" not in headers  # C2: no stray auth header
    else:  # ollama: no credential header at all
        assert "authorization" not in headers
        assert "x-goog-api-key" not in headers
    # wire path stays OpenAI-compat chat/completions (shared adapter class)
    assert req.url.path.endswith("/chat/completions")


@pytest.mark.parametrize("model,provider", [
    ("openai/gpt-4o", "openai"),
    ("anthropic/claude-sonnet-5", "anthropic"),
    ("openrouter/openai/gpt-4o", "openrouter"),
    ("xai/grok-4", "xai"),
    ("google/gemini-2.5-flash", "google"),
    ("vllm/qwen-72b", "vllm"),
])
@pytest.mark.parametrize("schema_compression_enabled", [True, False])
def test_tool_schema_compression_flag_controls_every_provider_wire(
        routed, monkeypatch, model, provider, schema_compression_enabled):
    """T1 AC-T4: every routed provider honors the schema wire-byte setting.

    The decoded tool schema and protocol fields must remain semantically
    identical; only whitespace surrounding the translated ``tools`` value may
    change. This specifically protects against the old ``json=wire.json_body``
    route, which ignored the configuration for every adapter.
    """
    monkeypatch.setenv(
        "TOOL_SCHEMA_COMPRESSION_ENABLED",
        "true" if schema_compression_enabled else "false",
    )
    get_settings.cache_clear()
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)

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
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        response = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tool-test"},
            json={
                "model": model,
                "messages": [
                    {"role": "assistant", "content": "", "tool_calls": [assistant_call]},
                    {"role": "tool", "tool_call_id": "call_1",
                     "content": '{\n  "forecast": "sunny"\n}'},
                ],
                "tools": tools,
                "tool_choice": "auto",
            },
        )
    assert response.status_code == 200, (provider, response.text[:200])
    assert len(cap.requests) == 1
    raw = cap.requests[0].content
    wire = json.loads(raw)
    assert wire["tool_choice"] == "auto"
    assert wire["tools"]
    if provider == "anthropic":
        assert wire["tools"][0]["name"] == "lookup_weather"
        assert wire["messages"][0]["content"][0]["id"] == "call_1"
    else:
        assert wire["tools"] == tools
        assert wire["messages"][0]["tool_calls"] == [assistant_call]

    # Only the tools member changes lexical representation.  The enabled
    # branch has no whitespace after a tools comma/colon; disabled uses the
    # standard preserving serializer that leaves schema delimiters spaced.
    # Anthropic's wire schema uses name/input_schema members (no "function"
    # nesting), so assert on its translated key instead.
    schema_key = b'"input_schema":{' if provider == "anthropic" else b'"function":{'
    schema_key_spaced = (
        b'"input_schema": {' if provider == "anthropic" else b'"function": {'
    )
    if schema_compression_enabled:
        assert b'"tools":[' in raw
        assert schema_key in raw
    else:
        assert b'"tools": [' in raw
        assert schema_key_spaced in raw


# ------------------------------------- (2) multimodal through the live route

def test_multimodal_live_route(routed):
    """C6: image parts translate through the live path (base64 + URL)."""
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-img"},
                   json={"model": "openai/gpt-4o", "messages": [
                       {"role": "user", "content": [
                           {"type": "text", "text": "what is this?"},
                           {"type": "image_url",
                            "image_url": {"url": "https://x.test/i.png"}},
                           {"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64,abc123"}},
                       ]}]})
        assert r.status_code == 200, r.text[:200]
    body = json.loads(cap.requests[0].content)
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "https://x.test/i.png"
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_multimodal_to_anthropic_live_route(routed):
    """C6: multimodal parts translate to Anthropic base64/url source shape."""
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-img"},
                   json={"model": "anthropic/claude-sonnet-5", "messages": [
                       {"role": "user", "content": [
                           {"type": "text", "text": "describe"},
                           {"type": "image_url",
                            "image_url": {"url": "data:image/png;base64,xyz"}},
                       ]}]})
        assert r.status_code == 200, r.text[:200]
    req = cap.requests[0]
    assert req.url.path == "/v1/messages"
    body = json.loads(req.content)
    parts = body["messages"][0]["content"]
    img = [p for p in parts if p.get("type") == "image"][0]
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"] == "xyz"


# ------------------------------------- (3) timeout / retry through live route

def test_upstream_timeout_maps_to_504(routed):
    """C7: upstream timeout (httpx.ConnectTimeout/ReadTimeout) surfaces as a
    normalized 504 to the client — not a crash and not a 500."""
    main_mod = routed

    class TimeoutTransport(_CaptureTransport):
        async def handle_async_request(self, request):
            self.requests.append(request)
            raise httpx.ReadTimeout("timed out", request=request)

    cap = TimeoutTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-t"},
                   json={"model": "openai/gpt-4o",
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 504, (r.status_code, r.text[:200])
        assert "error" in r.json()


def test_upstream_5xx_relays_normalized(routed):
    """C7/C8: upstream 5xx relays with the provider's status and error body."""
    main_mod = routed

    class Err5xxTransport(_CaptureTransport):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(503, json={"error": {"message": "overloaded"}},
                                  headers={"retry-after": "5"})

    cap = Err5xxTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-t"},
                   json={"model": "openai/gpt-4o",
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "overloaded"


def test_retry_after_header_preserved_on_429(routed):
    """C7: 429 retry-after info reaches the client response headers."""
    main_mod = routed

    class RateLimitTransport(_CaptureTransport):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(429, json={"error": {"message": "slow down"}},
                                  headers={"retry-after": "7"})

    cap = RateLimitTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-t"},
                   json={"model": "openai/gpt-4o",
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 429
        assert r.headers.get("retry-after") == "7"
