"""Routing-off ledger attribution must name the egress path, not the vendor.

AC-A12 follow-up (benchmark-trap regression): with PROVIDER_ROUTING=false the
proxy serves EVERY model through the legacy single upstream, but the old code
passed provider=None and stats.log_request re-derived the provider from
PREFIX_ROUTES — so an ``anthropic/claude-sonnet-5`` request egressed to
OpenRouter was ledgered as provider_id=anthropic.  The ledger's provider
column means "who actually served/egressed this request", so routing-off rows
must land on the seeded 'legacy' providers row — on the success path AND on
the transport-failure paths (504/502/unknown-provider 400).
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

_UPSTREAM_OK = {
    "id": "chatcmpl-legacy-attr",
    "object": "chat.completion",
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


class CaptureTransport(httpx.AsyncBaseTransport):
    """Returns a valid completion, or raises the fixture-injected error."""

    def __init__(self, error: Exception | None = None):
        self._error = error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._error is not None:
            raise self._error
        return httpx.Response(200, json=_UPSTREAM_OK)


@pytest.fixture()
def legacy_env(request, monkeypatch):
    """Real-ASGI + real-Postgres app with the legacy upstream captured.

    Indirect-parametrize with an exception instance to make the upstream
    transport raise it (transport-failure attribution cases).
    """
    upstream_error = getattr(request, "param", None)
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    # Routing OFF is the default, but be explicit: this is the legacy path.
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    get_settings.cache_clear()
    from proxy import main as main_mod

    upstream = httpx.AsyncClient(
        base_url="http://upstream.test/v1",
        transport=CaptureTransport(upstream_error),
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


def _last_row(dsn: str, model: str):
    with psycopg.connect(dsn) as pg:
        return pg.execute(
            "SELECT p.name, r.status FROM requests r"
            " JOIN providers p ON p.id = r.provider_id"
            " WHERE r.model = %s ORDER BY r.id DESC LIMIT 1",
            (model,),
        ).fetchone()


def test_routing_off_anthropic_prefix_ledgers_legacy(legacy_env):
    """The exact benchmark trap: claude-via-OpenRouter must NOT say 'anthropic'."""
    client, dsn = legacy_env
    resp = _chat(client, "anthropic/claude-sonnet-5")
    assert resp.status_code == 200
    assert _last_row(dsn, "anthropic/claude-sonnet-5") == ("legacy", 200)


def test_routing_off_openai_prefix_ledgers_legacy(legacy_env):
    """Same contract for every prefix: routing off = one legacy egress path."""
    client, dsn = legacy_env
    resp = _chat(client, "openai/gpt-4o")
    assert resp.status_code == 200
    assert _last_row(dsn, "openai/gpt-4o") == ("legacy", 200)


@pytest.mark.parametrize(
    "legacy_env",
    [httpx.ConnectTimeout("upstream down"), httpx.ConnectError("refused")],
    indirect=True,
)
def test_routing_off_transport_failure_still_ledgers_legacy(legacy_env):
    """504/502 error rows must attribute to legacy too, not the prefix table."""
    client, dsn = legacy_env
    resp = _chat(client, "anthropic/claude-sonnet-5")
    assert resp.status_code in (502, 504)
    row = _last_row(dsn, "anthropic/claude-sonnet-5")
    assert row[0] == "legacy"
    assert row[1] in (502, 504)
