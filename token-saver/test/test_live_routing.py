"""PA-1 live-path contract tests (QA blocker fix).

QA's finding: `model="anthropic/claude-sonnet-5"` reached upstream as
/chat/completions with OpenAI-shaped messages + reasoning — the adapters
existed but main.py never routed through them.

These tests drive the REAL app route (`POST /v1/chat/completions`) with
provider_routing enabled and a mock transport capturing the actual wire
request, asserting:
- Anthropic models hit /v1/messages with system-as-param, x-api-key auth,
  no `reasoning` field, Anthropic wire shape
- OpenAI-compat models still hit /chat/completions with Bearer auth
- provider_routing=off preserves legacy single-upstream behavior
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from proxy.config import get_settings  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE  # noqa: E402

PG_ADMIN_DSN = PG_BASE
_CACHE_DB = "ts_live_cache_test"


def _seed_cache_db(dsn: str) -> None:
    schema = (Path(__file__).resolve().parent.parent.parent
              / "postgres-schema-v2.sql").read_text()
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(schema)
        pg.execute("INSERT INTO tenants (id, name) VALUES "
                   "('00000000-0000-0000-0000-000000000000','default')")


class _CaptureTransport(httpx.AsyncBaseTransport):
    """Captures wire requests; returns a canned OpenAI/Anthropic response."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content) if request.content else {}
        # Respond in the provider's own shape; the proxy relays raw.
        if request.url.path.endswith("/messages"):
            payload = {"id": "msg_1", "type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": "ok"}],
                       "model": body.get("model", "?"),
                       "usage": {"input_tokens": 5, "output_tokens": 2}}
        else:
            payload = {"id": "c1", "object": "chat.completion",
                       "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                       "model": body.get("model", "?"),
                       "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        return httpx.Response(200, json=payload)


@pytest.fixture()
def routed_env(monkeypatch):
    """Routing on + capture transport wired through the _client_factory hook."""
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    # dedicated throwaway PG database for cache state so tests don't pollute
    # (or inherit) the dev token_saver cache
    try:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_CACHE_DB}")
            pg.execute(f"CREATE DATABASE {_CACHE_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres unavailable for cache fixture", allow_module_level=False)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", f"{PG_ADMIN_DSN}/{_CACHE_DB}")
    # seed schema + tenant/providers in the throwaway db
    _seed_cache_db(f"{PG_ADMIN_DSN}/{_CACHE_DB}")
    get_settings.cache_clear()
    from proxy import main as main_mod

    cap = _CaptureTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        yield c, cap
    main_mod._client_factory = None
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    get_settings.cache_clear()
    try:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_CACHE_DB}")
    except psycopg.OperationalError:
        pass


def _post_chat(client, model, system="You are helpful", user="hi", extra=None):
    body = {"model": model,
            "messages": ([{"role": "system", "content": system}] if system else []) +
                        [{"role": "user", "content": user}],
            **(extra or {})}
    return client.post("/v1/chat/completions", json=body)


def test_anthropic_model_uses_messages_endpoint(routed_env):
    """THE QA BLOCKER: anthropic model must reach /v1/messages, not /chat/completions."""
    client, cap = routed_env
    r = _post_chat(client, "anthropic/claude-sonnet-5")
    assert r.status_code == 200
    assert len(cap.requests) == 1
    req = cap.requests[0]
    assert req.url.path == "/v1/messages"
    body = json.loads(req.content)
    assert "messages" in body and body["messages"][0]["role"] == "user"
    # system is a top-level param, NOT a message
    assert body.get("system") == "You are helpful"
    assert all(m.get("role") != "system" for m in body["messages"])
    # reasoning override must not leak into Anthropic shape
    assert "reasoning" not in body


def test_anthropic_auth_header_is_x_api_key(routed_env):
    client, cap = routed_env
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-or-v1-test"},
                    json={"model": "anthropic/claude-sonnet-5",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    req = cap.requests[0]
    assert req.headers.get("x-api-key") == "sk-or-v1-test"
    assert "authorization" not in {k.lower() for k in req.headers}


def test_openai_model_still_uses_chat_completions(routed_env):
    client, cap = routed_env
    r = _post_chat(client, "openai/gpt-4o")
    assert r.status_code == 200
    req = cap.requests[0]
    assert req.url.path.endswith("/chat/completions")
    body = json.loads(req.content)
    assert body["messages"][0]["role"] == "system"  # OpenAI keeps system in messages


def test_routing_off_is_legacy_passthrough(monkeypatch):
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()
    from proxy import main as main_mod

    cap = _CaptureTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-x"},
                   json={"model": "anthropic/claude-sonnet-5",
                         "messages": [{"role": "system", "content": "s"},
                                      {"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text[:200]
        # legacy path: single upstream, OpenAI shape, /chat/completions
        assert cap.requests[0].url.path.endswith("/chat/completions")
        body = json.loads(cap.requests[0].content)
        assert body["messages"][0]["role"] == "system"
    main_mod._client_factory = None
    get_settings.cache_clear()


# ---------------------------------------------- response normalization (C4/C8)

def test_anthropic_response_reshaped_to_openai(routed_env):
    """QA blocker 1: Anthropic response must come back as OpenAI choices shape."""
    client, cap = routed_env
    r = _post_chat(client, "anthropic/claude-sonnet-5")
    assert r.status_code == 200
    data = r.json()
    assert "choices" in data, f"client got non-OpenAI shape: {data}"
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "ok"
    assert data["object"] == "chat.completion"
    assert data["usage"]["prompt_tokens"] == 5
    assert data["usage"]["completion_tokens"] == 2


def test_openai_response_untouched(routed_env):
    client, cap = routed_env
    r = _post_chat(client, "openai/gpt-4o")
    data = r.json()
    assert "choices" in data and data["choices"][0]["message"]["content"] == "ok"


def test_anthropic_error_relayed_with_normalized_kind(routed_env):
    """Errors keep the provider's body but the ledger gets a normalized kind."""
    client, cap = routed_env
    # force an error response from the mock
    class ErrTransport(_CaptureTransport):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(429, json={"error": {"type": "rate_limit_error",
                                                       "message": "slow down"}})
    from proxy import main as main_mod
    err_cap = ErrTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=err_cap)
    try:
        r = _post_chat(client, "anthropic/claude-sonnet-5")
        assert r.status_code == 429
        assert "error" in r.json()
    finally:
        main_mod._client_factory = lambda b, t: httpx.AsyncClient(
            base_url=b, timeout=t, transport=cap)


# ---------------------------------------------- URL joining (QA blocker 3)

def test_openai_base_url_with_v1_does_not_double(routed_env):
    """openai base ends in /v1 and adapter path starts with /v1 — must not
    produce /v1/v1/chat/completions (QA-found 404 regression)."""
    client, cap = routed_env
    r = _post_chat(client, "openai/gpt-4o")
    assert r.status_code == 200
    assert "/v1/v1/" not in str(cap.requests[0].url)
    assert cap.requests[0].url.path == "/v1/chat/completions"


def test_anthropic_base_path_exact(routed_env):
    client, cap = routed_env
    _post_chat(client, "anthropic/claude-sonnet-5")
    assert str(cap.requests[0].url) == "https://api.anthropic.com/v1/messages"


main_mod_test_cap = [None]  # indirection used by the /v1-less base test


def test_base_without_v1_keeps_adapter_path(routed_env, monkeypatch):
    """A base URL with no /v1 suffix must still yield the full adapter path."""
    from proxy import main as main_mod
    custom_cap = _CaptureTransport()
    main_mod_test_cap[0] = custom_cap
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=custom_cap)
    try:
        client, _ = routed_env
        monkeypatch.setenv("OPENAI_BASE_URL_OVERRIDE", "")
        # directly override the settings dict used by _forward_routed
        s = get_settings()
        monkeypatch.setattr(s, "provider_base_urls",
                            {**s.provider_base_urls,
                             "openai": "https://custom.example.com"})
        r = _post_chat(client, "openai/gpt-4o")
        assert r.status_code == 200
        urls = [str(q.url) for q in custom_cap.requests]
        assert any(u.startswith("https://custom.example.com/v1/chat/completions")
                   for u in urls), urls
    finally:
        main_mod._client_factory = None
        main_mod_test_cap[0] = None


# ---------------------------------------------- non-2xx relay (QA blocker 3b)

def test_non_json_error_body_relayed_without_500(routed_env):
    """A non-JSON upstream error (e.g. HTML 502) must relay raw, not 500."""
    client, cap = routed_env
    class HtmlErrTransport(_CaptureTransport):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(502, text="<html>Bad Gateway</html>",
                                  headers={"content-type": "text/html"})
    from proxy import main as main_mod
    err_cap = HtmlErrTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=err_cap)
    try:
        r = _post_chat(client, "openai/gpt-4o")
        assert r.status_code == 502
        assert "Bad Gateway" in r.text
        assert "text/html" in r.headers.get("content-type", "")
    finally:
        main_mod._client_factory = lambda b, t: httpx.AsyncClient(
            base_url=b, timeout=t, transport=cap)


def test_json_error_body_relayed_cleanly(routed_env):
    """A JSON upstream error must relay its body with the provider's status."""
    client, cap = routed_env
    class JsonErrTransport(_CaptureTransport):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(429, json={"error": {"message": "slow down"}})
    from proxy import main as main_mod
    err_cap = JsonErrTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=err_cap)
    try:
        r = _post_chat(client, "openai/gpt-4o")
        assert r.status_code == 429
        assert r.json()["error"]["message"] == "slow down"
    finally:
        main_mod._client_factory = lambda b, t: httpx.AsyncClient(
            base_url=b, timeout=t, transport=cap)

def test_cache_status_reaches_ledger(routed_env, monkeypatch):
    """QA blocker 2: second identical request logs cache_status='exact_hit'."""
    client, cap = routed_env
    import proxy.stats as stats_mod
    from proxy import caching

    seen = []
    real_log = stats_mod.log_request

    def spy(**kwargs):
        seen.append(kwargs.get("cache_status", "MISSING"))
        # don't actually write (test db not seeded); just capture
        return None

    monkeypatch.setattr(stats_mod, "log_request", spy)
    _post_chat(client, "anthropic/claude-sonnet-5", user="q1")
    _post_chat(client, "anthropic/claude-sonnet-5", user="q1")  # identical prefix
    assert len(seen) == 2, seen
    assert seen[0] == "miss"
    assert seen[1] == "exact_hit", f"second identical request must be exact_hit, got {seen}"


def test_non_routed_cache_status_defaults_miss(monkeypatch):
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()
    from proxy import main as main_mod
    import proxy.stats as stats_mod

    seen = []
    monkeypatch.setattr(stats_mod, "log_request",
                        lambda **kw: seen.append(kw.get("cache_status", "MISSING")))
    cap = _CaptureTransport()
    main_mod._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=cap)
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        c.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-x"},
               json={"model": "anthropic/claude-sonnet-5",
                     "messages": [{"role": "user", "content": "hi"}]})
    assert seen == ["miss"]
    main_mod._client_factory = None
    get_settings.cache_clear()
