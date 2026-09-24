from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.providers.anthropic import AnthropicAdapter
from proxy.providers.model import NormalizedRequest
from proxy.providers.openai_compat import OpenAICompatAdapter


def test_normalized_provider_usage_distinguishes_missing_from_measured_zero():
    request = NormalizedRequest(model="openai/gpt-4o")
    adapter = OpenAICompatAdapter(name="openai")

    absent = adapter.translate_response(httpx.Response(200, json={
        "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }), request).usage
    zero = adapter.translate_response(httpx.Response(200, json={
        "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                         "prompt_tokens_details": {"cached_tokens": 0}},
    }), request).usage

    assert absent is not None and absent.cache_read_tokens is None
    assert zero is not None and zero.cache_read_tokens == 0


def test_anthropic_normalized_usage_keeps_cache_read_write_lanes_separate():
    request = NormalizedRequest(model="anthropic/claude-sonnet-5")
    response = httpx.Response(200, json={
        "content": [], "usage": {"input_tokens": 12, "output_tokens": 2,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 7},
    })

    usage = AnthropicAdapter().translate_response(response, request).usage

    assert usage is not None
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 7
    assert not hasattr(usage, "cache_savings")


def test_main_log_passes_cache_usage_without_merging_savings(monkeypatch):
    from proxy import main

    captured = {}
    monkeypatch.setattr(main.stats, "log_request", lambda **kwargs: captured.update(kwargs))
    main._log("openai/gpt-4o", "compress", 100, 80, 5, 1.0, True, 200,
              cache_savings=0.25, l1_savings=0.5, tool_compression_saved=3,
              provider_cache_read_tokens=0, provider_cache_write_tokens=2)

    assert captured["provider_cache_read_tokens"] == 0
    assert captured["provider_cache_write_tokens"] == 2
    assert captured["cache_savings"] == 0.25
    assert captured["l1_savings"] == 0.5
    assert captured["tool_compression_saved"] == 3


def test_sqlite_ledger_remains_compatible_without_provider_cache_columns(tmp_path, monkeypatch):
    from proxy import stats

    db_path = tmp_path / "stats.sqlite"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    stats.init_db()
    stats.log_request(
        model="openai/gpt-4o", route="passthrough",
        input_tokens_before=10, input_tokens_after=10, output_tokens=2,
        est_cost_before=0, est_cost_after=0, latency_ms=1,
        compressed=False, status=200, provider_cache_read_tokens=0,
        provider_cache_write_tokens=None,
    )
    with stats.get_conn() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
        row = conn.execute("SELECT l1_savings, tool_compression_saved "
                           "FROM requests").fetchone()

    assert "provider_cache_read_tokens" not in columns
    assert "provider_cache_write_tokens" not in columns
    assert tuple(row) == (0.0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage,expected_read,expected_write",
    [
        ({"input_tokens": 8, "output_tokens": 1,
          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 4}, 0, 4),
        ({"input_tokens": 8, "output_tokens": 1}, None, None),
    ],
)
async def test_nonstream_relay_logs_normalized_provider_cache_evidence(
    monkeypatch, usage, expected_read, expected_write
):
    from proxy import main

    captured = []
    monkeypatch.setattr(main, "_log", lambda *args, **kwargs: captured.append(kwargs))
    response = httpx.Response(200, json={"content": [], "usage": usage})
    await main._relay(
        response, 1.0, model="anthropic/claude-sonnet-5", route="passthrough",
        in_before=10, in_after=10, compressed=False, provider="anthropic",
    )

    assert captured[0]["provider_cache_read_tokens"] == expected_read
    assert captured[0]["provider_cache_write_tokens"] == expected_write


@pytest.mark.asyncio
async def test_translated_stream_relay_logs_cache_read_and_write_zero(monkeypatch):
    import json
    from proxy import main

    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    main.get_settings.cache_clear()
    captured = []
    monkeypatch.setattr(main, "_log", lambda *args, **kwargs: captured.append(kwargs))
    events = [
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 8, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 5,
        }}},
        {"type": "message_delta", "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    response = httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )
    relayed = await main._relay(
        response, 1.0, model="anthropic/claude-sonnet-5", route="passthrough",
        in_before=10, in_after=10, compressed=False, streaming=True,
        provider="anthropic",
    )
    async for _ in relayed.body_iterator:
        pass
    main.get_settings.cache_clear()

    assert captured[0]["provider_cache_read_tokens"] == 0
    assert captured[0]["provider_cache_write_tokens"] == 5
