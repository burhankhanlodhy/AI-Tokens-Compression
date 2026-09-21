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
from proxy.compression import compress_messages, has_compressible_content
from proxy.counting import count_messages, count_text, inject_conciseness
from proxy.l1_clean import clean_messages


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


def test_compression_never_rewrites_tool_protocol_messages(monkeypatch):
    monkeypatch.setattr("proxy.compression.compress_text", lambda text: "CORRUPTED")
    messages = [
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "planning", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "lookup", "arguments": '{"city": "Paris"}'},
        }]},
    ]
    assert compress_messages(messages) == messages


def test_l1_never_rewrites_tool_protocol_messages():
    messages = [
        {"role": "tool", "tool_call_id": "call_1",
         "content": '{"content":"result","score":0.9}'},
        {"role": "assistant", "content": '{"content":"plan","score":0.9}',
         "tool_calls": [{"id": "call_1", "function": {"arguments": "{}"}}]},
    ]
    assert clean_messages(messages) == messages


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
        captured["raw"] = request.content
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
async def test_tool_calling_request_skips_content_transforms(
        capturing_client, monkeypatch):
    """Agent/tool protocol traffic bypasses compression and L1 cleaning."""
    c, captured = capturing_client
    from proxy import l1_clean, main as main_module

    monkeypatch.setattr(
        main_module, "compress_messages",
        lambda messages: pytest.fail("tool-calling request reached compression"),
    )
    monkeypatch.setattr(
        l1_clean, "clean_messages",
        lambda messages: pytest.fail("tool-calling request reached L1"),
    )
    payload = {
        "model": "z-ai/glm-5.3-flash",
        "messages": [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": PROSE * 8},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"city":"Paris"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object"},
        }}],
        "tool_choice": "auto",
    }
    response = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json=payload,
    )
    assert response.status_code == 200
    assert captured["body"]["messages"] == payload["messages"]
    assert captured["body"]["tools"] == payload["tools"]
    assert captured["body"]["tool_choice"] == payload["tool_choice"]
    # The P0 gate is limited to content transforms; existing model-family
    # reasoning policy remains unchanged pending a separate product ruling.
    assert captured["body"]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_existing_reasoning_injection_remains_for_tool_calling_request(
        capturing_client):
    """Tool gating does not silently change the independent reasoning policy."""
    c, captured = capturing_client
    payload = {
        "model": "google/gemini-3.5-flash-lite",
        "messages": [{"role": "user", "content": PROSE}],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object"},
        }}],
    }
    response = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json=payload,
    )
    assert response.status_code == 200
    assert captured["body"]["messages"] == payload["messages"]
    assert captured["body"]["tools"] == payload["tools"]
    assert captured["body"]["thinking_level"] == "MINIMAL"


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
    """P1-1 SD gate evidence: the client can RECORD that the control was
    sent upstream (otherwise a silent mapping failure looks identical to
    working suppression). The header names what was injected."""
    c, captured = capturing_client
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "z-ai/glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.headers.get("x-token-saver-reasoning") == "injected:reasoning"


@pytest.mark.asyncio
async def test_gemini_family_gets_minimal_flooring_control(capturing_client):
    """PM v4 ruling: the Gemini family's injected control is the MINIMAL
    FLOORING control (reasoning-mandatory endpoints reject {"enabled":
    false}, and MINIMAL is their lowest floor), not the suppress control."""
    c, captured = capturing_client
    await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "google/gemini-3.5-flash-lite",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    assert captured["body"]["thinking_level"] == "MINIMAL"
    assert "reasoning" not in captured["body"]


@pytest.mark.asyncio
async def test_client_supplied_thinking_level_is_respected(capturing_client):
    """A client that explicitly sets thinking_level is never overridden."""
    c, captured = capturing_client
    await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={"model": "google/gemini-3.5-flash-lite",
              "messages": [{"role": "user", "content": "hi"}],
              "thinking_level": "HIGH"},
    )
    assert captured["body"]["thinking_level"] == "HIGH"


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
@pytest.mark.parametrize(
    "slug",
    [
        "z-ai/glm-5.3-flash",
        # SD-gate defect (309f924): the gemini slug prefix-routes to the
        # "google" registry row; with PROVIDER_ROUTING off the transport is
        # still the legacy OpenRouter upstream, and the retry guard used to
        # skip it because "google" wasn't in its provider allowlist. Every
        # slug we intend to route must take the retry path.
        "google/gemini-3.5-flash-lite",
    ],
)
async def test_reasoning_mandatory_model_retries_without_override(tmp_db, slug):
    from proxy import main as main_module
    from proxy.main import app

    main_module._reasoning_mandatory_models.clear()
    calls: list[dict] = []
    # The mock upstream rejects the control this slug would carry (as the
    # live OpenRouter endpoint did for gemini) so the retry path is exercised.
    rejected_keys = ({"reasoning"} if slug == "z-ai/glm-5.3-flash"
                     else {"thinking_level"})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if rejected_keys & body.keys():
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
            json={"model": slug,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    await app.state.http.aclose()

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"
    assert len(calls) == 2
    # calls[0] carries the slug's injected control; the retry (calls[1])
    # drops ALL of the injected keys, whatever family they belong to.
    expected_control = ({"reasoning": {"enabled": False}}
                        if slug == "z-ai/glm-5.3-flash"
                        else {"thinking_level": "MINIMAL"})
    for k, v in expected_control.items():
        assert calls[0][k] == v
    assert not any(k in calls[1] for k in expected_control)
    assert slug in main_module._reasoning_mandatory_models
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
            json={"model": slug,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    await app.state.http.aclose()

    assert resp.status_code == 200
    assert len(calls) == 1
    assert "reasoning" not in calls[0]

    main_module._reasoning_mandatory_models.clear()


@pytest.mark.asyncio
async def test_non_mandatory_400_relays_rejected_evidence_header(tmp_db):
    """A 400 that is NOT the mandatory-reasoning error is relayed raw — and
    the evidence header must record the rejection, never read 'injected' on
    a failed request (PM's header finding at 309f924)."""
    from proxy import main as main_module
    from proxy.main import app

    main_module._reasoning_mandatory_models.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "invalid api key", "code": 400}},
        )

    transport = httpx.MockTransport(handler)
    app.state.http = httpx.AsyncClient(transport=transport, base_url="http://upstream.test/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key-123"},
            json={"model": "google/gemini-3.5-flash-lite",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    await app.state.http.aclose()

    assert resp.status_code == 400
    assert resp.headers.get("x-token-saver-reasoning") == "rejected_400_relayed"


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
