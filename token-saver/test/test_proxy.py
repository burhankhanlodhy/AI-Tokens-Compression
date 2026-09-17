"""Unit tests: classifier routing, counting, stats, passthrough proxy.

Compression is mocked (no model download in tests); the passthrough proxy is
tested with httpx.MockTransport.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_optional_support import require_pg_dsn  # noqa: E402
from proxy import stats
from proxy.classifier import classify
from proxy.config import get_settings
from proxy.compression import has_compressible_content
from proxy.counting import count_messages, count_text, inject_conciseness


# ---------- classifier ----------

CODE = """Here is my function:

```python
def process(items):
    result = []
    for item in items:
        if item > 0:
            result.append(item * 2)
    return result
```
"""

PROSE = (
    "I need help planning a birthday party for my daughter who is turning eight. "
    "She loves dinosaurs and space, and we are expecting about twelve kids in the "
    "backyard. The party runs from two in the afternoon until five, and we would "
    "like to have some games, a small cake, and maybe a simple craft activity. "
    "Could you suggest a schedule that keeps twelve eight-year-olds happily "
    "entertained for three hours without anyone getting bored or overwhelmed? "
    "Please keep it practical and inexpensive."
)


def test_code_prompt_routes_to_passthrough():
    assert classify([{"role": "user", "content": CODE}]) == "passthrough"


def test_prose_prompt_routes_to_compress():
    assert classify([{"role": "user", "content": PROSE}]) == "compress"


def test_short_prompts_default_to_compress():
    assert classify([{"role": "user", "content": "hi there"}]) == "compress"


def test_multimodal_code_part_detected():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "explain this:\n" + CODE}]}]
    assert classify(msgs) == "passthrough"


# ---------- counting ----------

def test_count_text_reasonable():
    assert count_text("hello world", "gpt-4o-mini") >= 2


def test_count_messages_includes_overhead():
    msgs = [{"role": "user", "content": "hello"}]
    assert count_messages(msgs, "gpt-4o-mini") > count_text("hello", "gpt-4o-mini")


def test_inject_conciseness_merges_into_system():
    msgs = [{"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "hi"}]
    out = inject_conciseness(msgs)
    assert out[0]["role"] == "system"
    assert "Be helpful." in out[0]["content"]
    assert "concisely" in out[0]["content"]
    assert len(out) == 2


def test_inject_conciseness_adds_system_when_missing():
    out = inject_conciseness([{"role": "user", "content": "hi"}])
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"


def test_has_compressible_content_false_for_short_prompt():
    msgs = [{"role": "user", "content": "In two sentences: why is the sky blue?"}]
    assert has_compressible_content(msgs) is False


def test_has_compressible_content_true_for_long_prompt():
    msgs = [{"role": "user", "content": "word " * 200}]
    assert has_compressible_content(msgs) is True


def test_has_compressible_content_ignores_system_by_default():
    msgs = [{"role": "system", "content": "spec " * 200},
            {"role": "user", "content": "hi"}]
    assert has_compressible_content(msgs) is False


# ---------- stats ----------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # These unit tests intentionally exercise SQLite.  Do not let a process
    # level production DSN silently change their ledger backend.
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    get_settings.cache_clear()
    stats.init_db()
    yield
    get_settings.cache_clear()


@pytest.fixture
def postgres_stats():
    """Require a reachable production DSN before PG-sensitive unit paths."""
    return require_pg_dsn()


def test_stats_roundtrip(postgres_stats, tmp_db):
    stats.log_request(model="gpt-4o-mini", route="compress",
                      input_tokens_before=1000, input_tokens_after=600,
                      output_tokens=200, est_cost_before=0.0005,
                      est_cost_after=0.00039, latency_ms=120.0,
                      compressed=True, status=200)
    data = stats.aggregate_stats()
    t = data["totals"]
    assert t["requests"] == 1
    assert t["input_tokens_saved"] == 400
    assert t["input_savings_pct"] == 40.0
    assert data["by_route"][0]["route"] == "compress"


# ---------- proxy passthrough (mocked upstream) ----------

UPSTREAM_RESPONSE = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


@pytest_asyncio.fixture
async def client(tmp_db, monkeypatch):
    from proxy.main import app

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        # BYOK: the client's Authorization header must be forwarded untouched.
        assert request.headers["authorization"] == "Bearer test-key-123"
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    transport = httpx.MockTransport(handler)
    app.state.http = httpx.AsyncClient(transport=transport, base_url="http://upstream.test/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    await app.state.http.aclose()


@pytest.mark.asyncio
async def test_passthrough_forwards_auth_and_returns_response(client):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"


@pytest_asyncio.fixture
async def capturing_client(tmp_db):
    from proxy.main import app

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    transport = httpx.MockTransport(handler)
    app.state.http = httpx.AsyncClient(transport=transport, base_url="http://upstream.test/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, captured
    await app.state.http.aclose()


@pytest.mark.asyncio
async def test_reasoning_disabled_by_default(capturing_client):
    c, captured = capturing_client
    await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "z-ai/glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert captured["body"]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_reasoning_evidence_header_on_injected_override(capturing_client):
    """P1-1 SD gate evidence: the client can RECORD that the override was
    sent upstream (otherwise a silent mapping failure looks identical to
    working suppression)."""
    c, captured = capturing_client
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "z-ai/glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.headers.get("x-token-saver-reasoning") == "injected"


@pytest.mark.asyncio
async def test_reasoning_respects_explicit_client_choice(capturing_client):
    c, captured = capturing_client
    await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={
            "model": "z-ai/glm-5.3-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"enabled": True, "effort": "high"},
        },
    )
    assert captured["body"]["reasoning"] == {"enabled": True, "effort": "high"}


@pytest.mark.asyncio
async def test_no_reasoning_header_when_client_supplied_reasoning(capturing_client):
    c, _ = capturing_client
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={
            "model": "z-ai/glm-5.3-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"enabled": True, "effort": "high"},
        },
    )
    assert "x-token-saver-reasoning" not in resp.headers


@pytest.mark.asyncio
async def test_reasoning_mandatory_model_retries_without_override(tmp_db):
    from proxy import main as main_module
    from proxy.main import app

    main_module._reasoning_mandatory_models.clear()
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "reasoning" in body:
            return httpx.Response(
                400,
                json={"error": {"message": "Reasoning is mandatory for this "
                                            "endpoint and cannot be disabled.",
                                "code": 400}},
            )
        return httpx.Response(200, json=UPSTREAM_RESPONSE)

    transport = httpx.MockTransport(handler)
    app.state.http = httpx.AsyncClient(transport=transport, base_url="http://upstream.test/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json={"model": "z-ai/glm-5.3-flash",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    await app.state.http.aclose()

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"
    assert len(calls) == 2
    assert "reasoning" in calls[0]
    assert "reasoning" not in calls[1]
    assert "z-ai/glm-5.3-flash" in main_module._reasoning_mandatory_models
    # The rejection must be visible to the client, not swallowed by the
    # proxy's silent retry — otherwise zero observed reasoning tokens after
    # a rejected override could be misread as confirmed suppression.
    assert resp.headers.get("x-token-saver-reasoning") == (
        "rejected_retry_without_override"
    )

    # A second request to the same model should skip the override entirely
    # and go straight through in one call.
    calls.clear()
    app.state.http = httpx.AsyncClient(transport=transport, base_url="http://upstream.test/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json={"model": "z-ai/glm-5.3-flash",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    await app.state.http.aclose()

    assert resp.status_code == 200
    assert len(calls) == 1
    assert "reasoning" not in calls[0]

    main_module._reasoning_mandatory_models.clear()


@pytest.mark.asyncio
async def test_invalid_json_body_forwarded_untouched(client, monkeypatch):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123", "Content-Type": "application/json"},
        content=b"not json at all",
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"


@pytest.mark.asyncio
async def test_stats_endpoint(postgres_stats, client):
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    resp = await client.get("/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["requests"] >= 1
    assert "by_route" in data and "by_day" in data
