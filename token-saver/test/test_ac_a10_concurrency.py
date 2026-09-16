"""AC-A10: concurrent production routes against isolated Postgres.

The workload mixes writes (/v1/* ledger calls) and reads (/api/kpis,
/metrics, dashboard).  It asserts both absence of request failures and that
all ledger writes survive the concurrent run; a swallowed constraint or lock
error therefore cannot masquerade as a successful 200 response.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx
import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import drop_database, make_database  # noqa: E402
from proxy.config import get_settings  # noqa: E402
from proxy import stats  # noqa: E402


DB_NAME = "ts_ac_a10_concurrency"


class CaptureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": []})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"object": "list", "data": []})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-concurrency",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )


@pytest.mark.asyncio
async def test_postgres_survives_concurrent_proxy_dashboard_and_kpi_calls(monkeypatch, tmp_path):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    monkeypatch.setenv("CACHE_ENABLED", "false")
    get_settings.cache_clear()
    stats.init_db()

    from proxy import main as main_mod

    upstream = httpx.AsyncClient(
        base_url="http://upstream.test/v1", transport=CaptureTransport()
    )
    main_mod.app.state.http = upstream
    main_mod.app.state.http_clients = {}

    async def one_call(client: httpx.AsyncClient, index: int):
        operation = index % 5
        if operation == 0:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer concurrency-test-key"},
                json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )
        if operation == 1:
            return await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer concurrency-test-key"},
            )
        if operation == 2:
            return await client.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer concurrency-test-key"},
                json={"model": "openai/gpt-4o", "input": "hi"},
            )
        if operation == 3:
            return await client.get("/api/kpis", params={"bucket": "minute"})
        return await client.get("/metrics")

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_mod.app), base_url="http://proxy.test"
        ) as client:
            responses = await asyncio.gather(*(one_call(client, i) for i in range(75)))

        failures = [
            f"{response.status_code}: {response.text[:200]}"
            for response in responses
            if response.status_code >= 500
        ]
        assert not failures, "concurrent route failures: " + "; ".join(failures[:5])

        # 3 of every 5 calls are ledger routes.  This catches both lock
        # failures and telemetry writes rejected by the route taxonomy.
        expected_rows = 75 * 3 // 5
        with psycopg.connect(dsn) as pg:
            count = int(pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
            bad_routes = pg.execute(
                "SELECT COUNT(*) FROM requests WHERE route NOT IN ('compress', 'passthrough')"
            ).fetchone()[0]
        assert count == expected_rows
        assert bad_routes == 0
    finally:
        await upstream.aclose()
        main_mod._client_factory = None
        get_settings.cache_clear()
        drop_database(DB_NAME)
