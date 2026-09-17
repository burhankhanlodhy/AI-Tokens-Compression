"""Routing-off ledger attribution must name the egress path, not the vendor.

AC-A12 follow-up (benchmark-trap regression): with PROVIDER_ROUTING=false the
proxy serves EVERY model through the legacy single upstream, but the old code
passed provider=None and stats.log_request re-derived the provider from
PREFIX_ROUTES — so an ``anthropic/claude-sonnet-5`` request egressed to
OpenRouter was ledgered as provider_id=anthropic.  The ledger's provider
column means "who actually served/egressed this request", so routing-off rows
must land on the seeded 'legacy' providers row.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import drop_database, make_database, unique_db_name  # noqa: E402
from proxy.config import get_settings  # noqa: E402

DB_NAME = unique_db_name("ts_legacy_attribution")


class CaptureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-legacy-attr",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


@pytest.fixture()
def legacy_env(monkeypatch):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    # Routing OFF is the default, but be explicit: this is the legacy path.
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    get_settings.cache_clear()
    from proxy import main as main_mod

    upstream = httpx.AsyncClient(
        base_url="http://upstream.test/v1", transport=CaptureTransport()
    )
    try:
        with TestClient(main_mod.app) as client:
            main_mod.app.state.http = upstream
            main_mod.app.state.http_clients = {}
            yield client, dsn
    finally:
        main_mod._client_factory = None
        get_settings.cache_clear()
        drop_database(DB_NAME)


def _chat(client: TestClient, model: str):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer legacy-attr-key"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def _provider_for_model(dsn: str, model: str) -> str | None:
    with psycopg.connect(dsn) as pg:
        row = pg.execute(
            "SELECT p.name FROM requests r JOIN providers p ON p.id = r.provider_id"
            " WHERE r.model = %s ORDER BY r.id DESC LIMIT 1",
            (model,),
        ).fetchone()
    return row[0] if row else None


def test_routing_off_anthropic_prefix_ledgers_legacy(legacy_env):
    """The exact benchmark trap: claude-via-OpenRouter must NOT say 'anthropic'."""
    client, dsn = legacy_env
    resp = _chat(client, "anthropic/claude-sonnet-5")
    assert resp.status_code == 200
    assert _provider_for_model(dsn, "anthropic/claude-sonnet-5") == "legacy"


def test_routing_off_openai_prefix_ledgers_legacy(legacy_env):
    """Same contract for every prefix: routing off = one legacy egress path."""
    client, dsn = legacy_env
    resp = _chat(client, "openai/gpt-4o")
    assert resp.status_code == 200
    assert _provider_for_model(dsn, "openai/gpt-4o") == "legacy"
