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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from proxy.config import get_settings  # noqa: E402


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
    get_settings.cache_clear()


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
