"""AC-A13: raw API credentials never cross the persistence/log/error boundary."""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import DEFAULT_TENANT, drop_database, make_database, unique_db_name  # noqa: E402
from proxy.config import get_settings  # noqa: E402


DB_NAME = unique_db_name("ts_ac_a13_secrets")
RAW_KEY = "sk_live_qa_a13_raw_key_must_not_persist"


class ErrorTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.seen_authorization = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen_authorization = request.headers.get("authorization")
        return httpx.Response(502, json={"error": {"message": "upstream unavailable"}})


@pytest.fixture()
def secret_env(monkeypatch):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO api_keys (tenant_id, key_hash, key_last4) VALUES (%s, %s, 'a13!')",
            (DEFAULT_TENANT, hashlib.sha256(RAW_KEY.encode()).hexdigest()),
        )

    from proxy import main as main_mod

    transport = ErrorTransport()
    upstream = httpx.AsyncClient(
        base_url="http://upstream.test/v1", transport=transport
    )
    try:
        with TestClient(main_mod.app) as client:
            main_mod.app.state.http = upstream
            main_mod.app.state.http_clients = {}
            yield client, dsn, transport, main_mod
    finally:
        # TestClient closes the replacement client during app shutdown.
        main_mod._client_factory = None
        get_settings.cache_clear()
        drop_database(DB_NAME)


def test_api_key_table_contains_hash_not_plaintext(secret_env):
    _client, dsn, _transport, _main_mod = secret_env
    with psycopg.connect(dsn) as pg:
        columns = {
            row[0]
            for row in pg.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'api_keys'"""
            ).fetchall()
        }
        row = pg.execute(
            "SELECT key_hash, key_last4 FROM api_keys WHERE key_last4 = 'a13!'"
        ).fetchone()
    assert "key_hash" in columns
    assert "key_last4" in columns
    assert RAW_KEY not in columns
    assert row[0] == hashlib.sha256(RAW_KEY.encode()).hexdigest()
    assert row[0] != RAW_KEY


def test_live_byok_request_does_not_persist_or_log_raw_key(secret_env, caplog):
    client, dsn, transport, _main_mod = secret_env
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {RAW_KEY}"},
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert transport.seen_authorization == f"Bearer {RAW_KEY}"
    assert RAW_KEY not in response.text
    assert RAW_KEY not in caplog.text

    # Scan every textual column in the committed schema, not just the columns
    # currently expected by the implementation.  This catches accidental
    # additions of raw-key fields and logger/event-table persistence.
    with psycopg.connect(dsn) as pg:
        text_columns = pg.execute(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND data_type IN ('text', 'character varying', 'character')"""
        ).fetchall()
        hits = []
        for table_name, column_name in text_columns:
            query = sql.SQL("SELECT 1 FROM {} WHERE CAST({} AS text) = %s LIMIT 1").format(
                sql.Identifier(table_name), sql.Identifier(column_name)
            )
            if pg.execute(query, (RAW_KEY,)).fetchone():
                hits.append(f"{table_name}.{column_name}")
    assert not hits, f"raw API key persisted in: {hits}"
