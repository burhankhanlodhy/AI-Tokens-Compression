"""Request-path semantic cache behavior without a live database."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import main, semantic_cache
from proxy.config import get_settings


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "id": "upstream",
                "choices": [{"message": {"role": "assistant", "content": "upstream"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )


def _body(stream: bool = False) -> dict:
    return {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def _run(monkeypatch, lookup_result, *, stream=False):
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_CACHE_MAX_COSINE_DISTANCE", "0.2")
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()
    transport = _Transport()
    captured = []
    monkeypatch.setattr(main, "acquire_embedding", _embedding)
    monkeypatch.setattr(semantic_cache, "lookup_result", lambda *a, **k: lookup_result)
    monkeypatch.setattr(main.stats, "log_request", lambda **kwargs: captured.append(kwargs))
    main._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=transport
    )
    try:
        with TestClient(main.app) as client:
            main.app.state.http = None
            main.app.state.http_clients = {}
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-test"}, json=_body(stream))
        return response, transport, captured
    finally:
        main._client_factory = None
        get_settings.cache_clear()


async def _embedding(*_args, **_kwargs):
    return [0.0] * 1536


def test_semantic_hit_replays_verbatim_without_upstream(monkeypatch):
    body = b'{"id":"cached", "choices":[]}'
    result = semantic_cache.SemanticLookupResult.hit_result(
        semantic_cache.SemanticCacheHit(7, "ref", 0.01),
        semantic_cache.SemanticCacheResponse(body, "", len(body)),
    )
    response, transport, rows = _run(monkeypatch, result)
    assert response.status_code == 200, response.text
    assert response.content == body
    assert transport.calls == 0
    assert rows[0]["cache_status"] == "semantic_hit"
    assert rows[0]["cache_savings"] > 0
    assert rows[0]["l1_tokens_stripped"] == 0


def test_semantic_threshold_miss_reaches_upstream_and_is_not_a_hit(monkeypatch):
    result = semantic_cache.SemanticLookupResult.threshold_miss()
    response, transport, rows = _run(monkeypatch, result)
    assert response.status_code == 200, response.text
    assert transport.calls == 1
    assert rows[0]["cache_status"] == "semantic_threshold_miss"
    assert rows[0]["cache_savings"] == 0.0


def test_streaming_never_acquires_or_stores_semantic_cache(monkeypatch):
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(main, "acquire_embedding", lambda *a, **k: calls.append(1))
    transport = _Transport()
    main._client_factory = lambda base_url, timeout: httpx.AsyncClient(
        base_url=base_url, timeout=timeout, transport=transport
    )
    try:
        with TestClient(main.app) as client:
            main.app.state.http = None
            main.app.state.http_clients = {}
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk-test"}, json=_body(True))
        assert response.status_code == 200, response.text
        assert calls == []
    finally:
        main._client_factory = None
        get_settings.cache_clear()
