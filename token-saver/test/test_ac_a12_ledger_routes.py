"""AC-A12/B-3: every registered API route must account for its ledger effect.

This is intentionally a real-ASGI + real-Postgres regression test.  A route
may be added to NO_LOG_ROUTES only when its lack of ledger accounting is an
explicit part of its contract.  In particular, /v1/models is a logged
passthrough route even though it has no prompt body.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import drop_database, ledger_count, make_database, unique_db_name  # noqa: E402
from proxy.config import get_settings  # noqa: E402


DB_NAME = unique_db_name("ts_ac_a12_routes")
NO_LOG_ROUTES = {
    ("GET", "/health"),
    ("GET", "/dashboard"),
    ("GET", "/static/dashboard.js"),
    ("GET", "/api/kpis"),
    ("GET", "/api/tripwire"),  # AC-P6f: read-only monitoring over the ledger
    ("GET", "/stats"),
    ("GET", "/metrics"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
}


class CaptureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": []})
        if path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={"object": "list", "data": [], "model": "openai/gpt-4o"},
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-route-test",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


def _request(client: TestClient, method: str, path: str):
    if path == "/v1/chat/completions":
        return client.request(
            method,
            path,
            headers={"Authorization": "Bearer route-test-key"},
            json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    if path == "/v1/embeddings":
        return client.request(
            method,
            path,
            headers={"Authorization": "Bearer route-test-key"},
            json={"model": "openai/gpt-4o", "input": "hi"},
        )
    if path == "/v1/models":
        return client.request(method, path, headers={"Authorization": "Bearer route-test-key"})
    return client.request(method, path)


@pytest.fixture()
def route_env(monkeypatch):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    monkeypatch.delenv("PROVIDER_ROUTING", raising=False)
    get_settings.cache_clear()
    from proxy import main as main_mod

    upstream = httpx.AsyncClient(
        base_url="http://upstream.test/v1", transport=CaptureTransport()
    )
    try:
        with TestClient(main_mod.app) as client:
            # Replace the lifespan-created real network client only after
            # startup has seeded the isolated Postgres database.
            main_mod.app.state.http = upstream
            main_mod.app.state.http_clients = {}
            yield client, dsn, main_mod
    finally:
        main_mod._client_factory = None
        get_settings.cache_clear()
        drop_database(DB_NAME)


def test_every_registered_route_has_ledger_contract(route_env):
    client, dsn, main_mod = route_env
    registered = []
    for route in main_mod.app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
        registered.extend((method, route.path) for method in methods)

    assert registered, "route inventory unexpectedly empty"
    before = ledger_count(dsn)
    for method, path in registered:
        response = _request(client, method, path)
        assert response.status_code < 500, f"{method} {path}: {response.status_code} {response.text[:300]}"
        after = ledger_count(dsn)
        should_log = (method, path) not in NO_LOG_ROUTES
        if should_log:
            assert after == before + 1, (
                f"{method} {path} is registered as a logged route but did not "
                f"persist one ledger row (before={before}, after={after})"
            )
            before = after
        else:
            assert after == before, f"no-log route {method} {path} changed the ledger"

    # The taxonomy is part of the ledger contract: every logged route must use
    # one of the schema's allowed values rather than relying on swallowed DB
    # errors to preserve the client response.
    with psycopg.connect(dsn) as pg:
        routes = [r[0] for r in pg.execute("SELECT route FROM requests").fetchall()]
    assert routes and set(routes) <= {"compress", "passthrough"}
