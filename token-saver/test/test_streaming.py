"""PA-1 C4 streaming tests through the real /v1/chat/completions route.

Feeds an Anthropic SSE stream through the live path and asserts the
client-facing OpenAI chat.completion.chunk contract:
- content deltas re-emitted as OpenAI delta chunks
- usage survives (message_start -> prompt_tokens; message_delta -> completion)
- [DONE] terminates the stream exactly once
- provider error events are surfaced, never silently dropped
- malformed data lines don't crash the stream
- OpenAI-compat providers keep raw passthrough behavior
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_live_routing import (  # noqa: E402
    PG_ADMIN_DSN,
    _CACHE_DB,
    _CaptureTransport,
    _seed_cache_db,
)
from proxy.config import get_settings  # noqa: E402


ANTHROPIC_SSE = "\n".join([
    'event: message_start',
    'data: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":25,"output_tokens":0}}}',
    "",
    'event: content_block_start',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    "",
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}',
    "",
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo world"}}',
    "",
    'event: message_delta',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}',
    "",
    'event: message_stop',
    'data: {"type":"message_stop"}',
    "",
]) + "\n"


class SseTransport(_CaptureTransport):
    def __init__(self, body: str, status: int = 200):
        super().__init__()
        self._body = body
        self._status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self._status, content=self._body.encode(),
            headers={"content-type": "text/event-stream"},
        )


def _parse_sse_events(raw: str) -> list[dict]:
    """Extract the JSON payloads from the proxied OpenAI-shaped SSE stream."""
    events = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events


@pytest.fixture()
def streaming_env(monkeypatch):
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/ts_stream_test.db")
    try:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_CACHE_DB}")
            pg.execute(f"CREATE DATABASE {_CACHE_DB}")
    except psycopg.OperationalError:
        pytest.skip("Postgres unavailable", allow_module_level=False)
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


def test_anthropic_stream_translated_to_openai_chunks(streaming_env):
    main_mod = streaming_env
    cap = SseTransport(ANTHROPIC_SSE)
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}  # fresh per-provider clients
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-t"},
                      json={"model": "anthropic/claude-sonnet-5", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
    events = _parse_sse_events(raw)
    assert events, "no SSE events emitted"

    # every event is OpenAI chunk shape
    for e in events:
        assert e.get("object") == "chat.completion.chunk", e

    # deltas concatenate to the full assistant text
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in events if e.get("choices"))
    assert text == "Hello world", repr(text)

    # exactly one [DONE] terminator, at the end
    done_count = sum(1 for blk in raw.split("\n\n")
                     if blk.strip() == "data: [DONE]")
    assert done_count == 1
    assert raw.rstrip().endswith("data: [DONE]")

    # usage survived: prompt_tokens from message_start, completion from message_delta
    usage_events = [e for e in events if "usage" in e]
    assert usage_events, "usage event missing from translated stream"
    final_usage = usage_events[-1]["usage"]
    assert final_usage["prompt_tokens"] == 25
    assert final_usage["completion_tokens"] == 9


def test_stream_error_event_surfaced(streaming_env):
    """A provider error event mid-stream must be surfaced, not dropped."""
    main_mod = streaming_env
    err_body = "\n".join([
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3,"output_tokens":0}}}',
        'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    ]) + "\n"
    cap = SseTransport(err_body)
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}  # fresh per-provider clients
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-t"},
                      json={"model": "anthropic/claude-sonnet-5", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
    events = _parse_sse_events(raw)
    err_events = [e for e in events if "error" in e]
    assert err_events, "provider error event was silently dropped"
    assert err_events[0]["error"]["message"] == "Overloaded"


def test_stream_malformed_line_does_not_crash(streaming_env):
    main_mod = streaming_env
    bad_body = "\n".join([
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1,"output_tokens":0}}}',
        'data: {not valid json!!',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}',
        'data: {"type":"message_stop"}',
    ]) + "\n"
    cap = SseTransport(bad_body)
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}  # fresh per-provider clients
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-t"},
                      json={"model": "anthropic/claude-sonnet-5", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
    events = _parse_sse_events(raw)
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in events if e.get("choices"))
    assert "ok" in text
    assert raw.rstrip().endswith("data: [DONE]")


def test_openai_stream_passthrough_unchanged(streaming_env):
    """OpenAI-compat providers keep raw passthrough (no re-shaping)."""
    main_mod = streaming_env
    openai_sse = "\n".join([
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hey"}}]}',
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"delta":{}}],"usage":{"prompt_tokens":2,"completion_tokens":1}}',
        'data: [DONE]',
    ]) + "\n"
    cap = SseTransport(openai_sse)
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http_clients = {}  # fresh per-provider clients
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-t"},
                      json={"model": "openai/gpt-4o", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
    # passthrough: original ids/objects preserved verbatim
    assert '"object": "chat.completion.chunk"' in raw or '"object":"chat.completion.chunk"' in raw
    assert '"id":"c1"' in raw or '"id": "c1"' in raw
    assert raw.rstrip().endswith("data: [DONE]")


def test_non_routed_stream_passthrough(monkeypatch):
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    monkeypatch.setenv("DATABASE_PATH", "/tmp/ts_stream_legacy.db")
    get_settings.cache_clear()
    from proxy import main as main_mod

    sse = 'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'
    cap = SseTransport(sse)
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-t"},
                      json={"model": "anthropic/claude-sonnet-5", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}) as resp:
            raw = "".join(resp.iter_text())
    # legacy path: raw passthrough even for anthropic models
    assert '"delta":{"content":"x"}' in raw
    main_mod._client_factory = None
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    get_settings.cache_clear()


# psycopg imported at top; kept here for clarity of the fixture dependency
