"""AC-A7: tenant and proxy-key views must be scoped before aggregation.

The schema has tenant/key foreign keys, but that alone does not prevent a KPI
query from aggregating every tenant.  These tests exercise the HTTP KPI path
with the tenant/key selectors used by the acceptance contract and verify the
response contains only the selected ledger facts.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import (  # noqa: E402
    DEFAULT_TENANT,
    TENANT_A,
    TENANT_B,
    drop_database,
    make_database,
    unique_db_name,
)
from proxy.config import get_settings  # noqa: E402


DB_NAME = unique_db_name("ts_ac_a7_isolation")
KEY_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KEY_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture()
def isolation_env(monkeypatch):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    get_settings.cache_clear()
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'tenant-a'), (%s, 'tenant-b')",
            (TENANT_A, TENANT_B),
        )
        pg.execute(
            """INSERT INTO api_keys (id, tenant_id, key_hash, key_last4)
               VALUES (%s, %s, %s, 'aa11'), (%s, %s, %s, 'bb22')""",
            (
                KEY_A,
                TENANT_A,
                hashlib.sha256(b"tenant-a-secret").hexdigest(),
                KEY_B,
                TENANT_B,
                hashlib.sha256(b"tenant-b-secret").hexdigest(),
            ),
        )
        pg.execute(
            """INSERT INTO requests
               (tenant_id, api_key_id, provider_id, model, route,
                input_tokens_before, input_tokens_after, output_tokens,
                est_cost_before, est_cost_after, status)
               SELECT %s, %s, id, 'openai/tenant-a-model', 'compress',
                      100, 50, 10, 0.001, 0.0005, 200
               FROM providers WHERE name = 'openai'""",
            (TENANT_A, KEY_A),
        )
        pg.execute(
            """INSERT INTO requests
               (tenant_id, api_key_id, provider_id, model, route,
                input_tokens_before, input_tokens_after, output_tokens,
                est_cost_before, est_cost_after, status)
               SELECT %s, %s, id, 'openai/tenant-b-model', 'compress',
                      200, 100, 10, 0.002, 0.001, 200
               FROM providers WHERE name = 'openai'""",
            (TENANT_B, KEY_B),
        )

    from proxy import main as main_mod

    try:
        # Explicitly run the real ASGI path; lifespan also runs the normal
        # seed/bootstrap code against this isolated database.
        with TestClient(main_mod.app) as client:
            yield client, dsn
    finally:
        get_settings.cache_clear()
        drop_database(DB_NAME)


def test_kpi_tenant_selector_cannot_leak_other_tenant(isolation_env):
    client, _dsn = isolation_env
    response = client.get(
        "/api/kpis",
        params={"bucket": "day", "tenant_id": TENANT_A},
        headers={"X-Tenant-ID": TENANT_A},
    )
    assert response.status_code == 200, response.text
    overview = response.json()["overview"]
    assert overview["requests"] == 1
    assert overview["input_tokens_before"] == 100
    assert [row["model"] for row in response.json()["by_model"]] == ["openai/tenant-a-model"]


def test_kpi_key_selector_cannot_leak_other_key(isolation_env):
    client, _dsn = isolation_env
    response = client.get(
        "/api/kpis",
        params={"bucket": "day", "tenant_id": TENANT_A, "api_key_id": KEY_A},
        headers={"X-Tenant-ID": TENANT_A},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["overview"]["requests"] == 1
    assert data["overview"]["input_tokens_before"] == 100
    assert [row["model"] for row in data["by_model"]] == ["openai/tenant-a-model"]
