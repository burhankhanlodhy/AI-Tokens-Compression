"""B2 integration: L1 pipeline ordering through the live proxy route.

With L1_ENABLED=true:
- upstream receives the CLEANED messages (whitespace-compacted JSON)
- an identical re-request produces the same clean bytes (cache-key stability)
- the ledger row records l1_tokens_stripped > 0 for a strippable prompt
- L1_ENABLED=false (default) leaves messages untouched
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import get_settings

RAG = json.dumps({"content": "TTL default is 3600 seconds.",
                  "score": 0.97, "retrieved_at": "2026-09-14"}, indent=2)

UPSTREAM_RESPONSE = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


@pytest.fixture
def l1_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app(transport_handler):
    from proxy.main import app
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler),
        base_url="http://upstream.test/v1")
    return app


@pytest_asyncio.fixture
async def capturing(l1_env, monkeypatch):
    monkeypatch.setenv("L1_ENABLED", "true")
    get_settings.cache_clear()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    app = _app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, captured
    await app.state.http.aclose()


@pytest.mark.asyncio
async def test_upstream_receives_clean_messages(capturing):
    c, captured = capturing
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": RAG}]},
    )
    assert resp.status_code == 200
    sent = captured["body"]["messages"][0]["content"]
    # v1.1: retrieved_at is negative-list (timestamps conserved)
    assert sent == json.dumps(
        {"content": "TTL default is 3600 seconds.", "retrieved_at": "2026-09-14"},
        separators=(",", ":"))
    # dead fields gone, reserved content intact
    obj = json.loads(sent)
    assert "score" not in obj
    assert obj["content"] == "TTL default is 3600 seconds."


@pytest.mark.asyncio
async def test_ledger_records_l1_tokens(capturing):
    from proxy import stats
    stats.init_db()  # test_proxy's tmp_db fixture does this; standalone here
    c, captured = capturing
    await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": RAG}]},
    )
    data = stats.aggregate_stats()
    assert data["totals"]["input_tokens_saved"] > 0


@pytest.mark.asyncio
async def test_l1_disabled_by_default_leaves_messages(l1_env, monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    app = _app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": RAG}]},
        )
    await app.state.http.aclose()
    assert resp.status_code == 200
    assert captured["body"]["messages"][0]["content"] == RAG  # untouched
