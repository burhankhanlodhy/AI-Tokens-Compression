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
from proxy.tool_protocol import (
    is_tool_result_compressible,
    is_tool_schema_compressible,
)


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


def test_tool_compression_eligibility_distinguishes_results_from_call_envelopes():
    tool_result = {"role": "tool", "tool_call_id": "call_1", "content": "{}"}
    tool_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "function": {"arguments": "{}"}}],
    }

    assert is_tool_result_compressible(tool_result) is True
    assert is_tool_result_compressible(tool_call) is False
    assert is_tool_result_compressible({"role": "user", "content": "{}"}) is False
    assert is_tool_schema_compressible([{"type": "function", "function": {}}]) is True
    assert is_tool_schema_compressible([]) is False
    assert is_tool_schema_compressible(["not-a-schema"]) is False


def test_eligibility_excludes_malformed_envelopes_and_tool_choice():
    """AC-T5: malformed envelopes and tool-choice directives fail closed."""
    # An empty/partial tool_calls value is still protocol state — never
    # eligible, even though the list itself is empty.
    assert is_tool_result_compressible({"role": "tool", "tool_calls": []}) is False
    assert is_tool_result_compressible(
        {"role": "assistant", "content": "", "tool_calls": []}
    ) is False
    # A tool-choice directive is not a schema: any non-empty-array of
    # mappings is the only compressible shape for the tools slot.
    assert is_tool_schema_compressible({"tool_choice": "auto"}) is False
    assert is_tool_schema_compressible("auto") is False
    assert is_tool_schema_compressible(None) is False
    assert is_tool_schema_compressible(
        [{"type": "function"}, "not-a-schema"]
    ) is False


def test_l1_cleans_tool_result_but_never_rewrites_tool_call_envelope():
    messages = [
        {"role": "tool", "tool_call_id": "call_1",
         "content": '{\n  "content": "result",\n  "score": 0.9\n}'},
        {"role": "assistant", "content": '{"content":"plan","score":0.9}',
         "tool_calls": [{"id": "call_1", "function": {"arguments": "{}"}}]},
    ]
    cleaned = clean_messages(messages)
    assert cleaned[0]["content"] == '{"content":"result","score":0.9}'
    assert cleaned[0]["tool_call_id"] == "call_1"
    assert cleaned[1] == messages[1]


def test_tool_result_compaction_preserves_json_number_and_escape_lexemes():
    content = (
        '{ "amount": 123456789012345678901234567890.12345678901234567890, '
        '"negative_zero": -0, "escaped": "\\u00e9" }'
    )
    cleaned = clean_messages([
        {"role": "tool", "tool_call_id": "call_1", "content": content}
    ])
    assert cleaned[0]["content"] == (
        '{"amount":123456789012345678901234567890.12345678901234567890,'
        '"negative_zero":-0,"escaped":"\\u00e9"}'
    )


def test_tool_calling_l1_only_cleans_safe_tool_result_and_system_content():
    messages = [
        {"role": "system", "content": '{\n  "instruction": "use tools"\n}'},
        {"role": "system", "content": '{\n  "instruction": "use tools"\n}'},
        {"role": "user", "content": '{\n  "query": "unchanged"\n}'},
        {"role": "tool", "tool_call_id": "call_1", "content": '{\n  "ok": true\n}'},
        {"role": "assistant", "tool_calls": [{"id": "call_1"}], "content": ""},
    ]
    assert clean_messages(messages, tool_calling=True) == [
        {"role": "system", "content": '{"instruction":"use tools"}'},
        {"role": "system", "content": '{"instruction":"use tools"}'},
        messages[2],
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
        messages[4],
    ]


def test_tool_schema_minification_preserves_function_calling_round_trip():
    """AC-T3: minified tools stay semantically identical for function calling."""
    from proxy.main import _serialize_request_payload

    tools = [{
        "type": "function",
        "function": {
            "name": "lookup_weather",
            "description": "Look up weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city"],
            },
        },
    }]
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "weather in Paris?"}],
        "tools": tools,
        "tool_choice": {"type": "function", "function": {"name": "lookup_weather"}},
    }

    raw = _serialize_request_payload(body, minify_tools=True)
    wire = json.loads(raw)

    # The tools member is serialized compactly on the wire.
    assert b'"tools":[' in raw
    # The round-trip preserves every function-calling field exactly.
    assert wire["tools"] == tools
    assert wire["tool_choice"] == body["tool_choice"]
    # Only the tools member is compacted; the rest of the body keeps the
    # default (space-after-colon) serialization.
    assert b'"model": "gpt-4o-mini"' in raw
    assert b'"tool_choice": {' in raw
    # Schema minification is whitespace-only: parsed values are identical.
    assert wire["tools"][0]["function"]["parameters"]["required"] == ["city"]
    assert wire["tools"][0]["function"]["name"] == "lookup_weather"


def test_tool_schema_compression_disabled_keeps_default_serialization():
    """AC-T5: TOOL_SCHEMA_COMPRESSION_ENABLED=false bypasses schema minify."""
    from proxy.main import _serialize_request_payload

    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    raw = _serialize_request_payload(body, minify_tools=False)
    assert b'"tools": [' in raw
    # A flag-off request must be byte-identical to the plain serialization.
    assert raw == json.dumps(body).encode()


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


def test_stats_persists_tool_compression_attribution(tmp_db):
    stats.log_request(
        model="gpt-4o-mini", route="passthrough",
        input_tokens_before=120, input_tokens_after=100, output_tokens=0,
        est_cost_before=0.00006, est_cost_after=0.00005, latency_ms=1.0,
        compressed=False, status=200, tool_compression_saved=20,
    )
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT tool_compression_saved FROM requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["tool_compression_saved"] == 20


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
async def test_tool_calling_request_selectively_cleans_safe_fields(
        capturing_client, monkeypatch):
    """Tool calls stay exact while safe result/schema bytes are compacted."""
    c, captured = capturing_client
    from proxy import main as main_module

    monkeypatch.setenv("L1_ENABLED", "true")
    monkeypatch.setenv("TOOL_RESULT_COMPRESSION_ENABLED", "true")
    monkeypatch.setenv("TOOL_SCHEMA_COMPRESSION_ENABLED", "true")
    get_settings.cache_clear()

    monkeypatch.setattr(
        main_module, "compress_messages",
        lambda messages: pytest.fail("tool-calling request reached compression"),
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
            {"role": "tool", "tool_call_id": "call_1", "content": '{\n  "ok": true,\n  "nested": {"value": 1}\n}'},
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
    assert captured["body"]["messages"][0] == payload["messages"][0]
    assert captured["body"]["messages"][1] == payload["messages"][1]
    assert captured["body"]["messages"][2] == payload["messages"][2]
    assert captured["body"]["messages"][3]["tool_call_id"] == "call_1"
    assert captured["body"]["messages"][3]["content"] == '{"ok":true,"nested":{"value":1}}'
    assert captured["body"]["tools"] == payload["tools"]
    assert captured["body"]["tool_choice"] == payload["tool_choice"]
    assert b'"tools":[{"type":"function"' in captured["raw"]
    assert b'"tool_choice": "auto"' in captured["raw"]
    assert b'"tool_calls": [{' in captured["raw"]
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT tool_compression_saved FROM requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["tool_compression_saved"] > 0
    # The P0 gate is limited to content transforms; existing model-family
    # reasoning policy remains unchanged pending a separate product ruling.
    assert captured["body"]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_tool_result_content_parts_are_l1_cleaned_and_attributed(
        capturing_client, monkeypatch):
    c, captured = capturing_client
    monkeypatch.setenv("L1_ENABLED", "true")
    monkeypatch.setenv("TOOL_RESULT_COMPRESSION_ENABLED", "true")
    monkeypatch.setenv("TOOL_SCHEMA_COMPRESSION_ENABLED", "false")
    get_settings.cache_clear()

    response = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "tool", "tool_call_id": "call_1", "content": [{
                    "type": "text", "text": '{\n  "ok": true\n}'
                }]},
            ],
        },
    )

    assert response.status_code == 200
    assert captured["body"]["messages"][0]["content"][0]["text"] == '{"ok":true}'
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT tool_compression_saved FROM requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["tool_compression_saved"] > 0


@pytest.mark.asyncio
async def test_tool_result_compression_disabled_leaves_content_untouched(
        capturing_client, monkeypatch):
    """AC-T5: TOOL_RESULT_COMPRESSION_ENABLED=false bypasses the transform.

    The old conservative behavior must remain recoverable: the tool result
    content reaches upstream byte-identical and no tool savings are claimed.
    """
    c, captured = capturing_client
    monkeypatch.setenv("L1_ENABLED", "true")
    monkeypatch.setenv("TOOL_RESULT_COMPRESSION_ENABLED", "false")
    monkeypatch.setenv("TOOL_SCHEMA_COMPRESSION_ENABLED", "false")
    get_settings.cache_clear()

    raw_tool_content = '{\n  "ok": true,\n  "nested": {"value": 1}\n}'
    response = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key-123"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "tool", "tool_call_id": "call_1",
                 "content": raw_tool_content},
            ],
        },
    )

    assert response.status_code == 200
    assert captured["body"]["messages"][0]["content"] == raw_tool_content
    assert captured["raw"].count(b"\\n") >= 1  # pretty-printing preserved
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT tool_compression_saved FROM requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["tool_compression_saved"] == 0


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
