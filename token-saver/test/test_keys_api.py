"""C-2 Keys & Tenants API gates for keys-tenants-tab-spec.md §3."""
from __future__ import annotations

import hashlib
import logging
import sys
import tempfile
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import DEFAULT_TENANT, drop_database, make_database, unique_db_name  # noqa: E402
from proxy.config import get_settings  # noqa: E402


DB_NAME = unique_db_name("ts_c2_keys_api")
ADMIN_TOKEN = "c2-admin-token-for-test"
UNKNOWN_TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture()
def keys_api_env(monkeypatch):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    get_settings.cache_clear()

    from proxy import main as main_mod

    main_mod.app.state._state.pop("admin_token", None)
    try:
        with TestClient(main_mod.app) as client:
            yield client, dsn
    finally:
        main_mod.app.state._state.pop("admin_token", None)
        get_settings.cache_clear()
        drop_database(DB_NAME)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _create(client: TestClient, **overrides) -> dict:
    body = {
        "tenant_id": DEFAULT_TENANT,
        "scopes": ["chat", "embeddings"],
        "spend_cap_usd": 12.5,
        **overrides,
    }
    response = client.post("/api/keys", json=body, headers=_auth())
    assert response.status_code == 201, response.text
    return response.json()


def test_reads_list_tenants_and_redacted_keys_only(keys_api_env):
    client, dsn = keys_api_env
    created = _create(client)

    tenants = client.get("/api/tenants")
    assert tenants.status_code == 200, tenants.text
    assert tenants.json() == [{
        "id": DEFAULT_TENANT,
        "name": "default",
        "plan": "self_host",
        "spend_cap_usd": None,
        "created_at": tenants.json()[0]["created_at"],
    }]

    listed = client.get("/api/keys", params={"tenant_id": DEFAULT_TENANT})
    assert listed.status_code == 200, listed.text
    assert listed.json() == [{
        "id": created["id"],
        "tenant_id": DEFAULT_TENANT,
        "key_last4": created["key_last4"],
        "scopes": ["chat", "embeddings"],
        "spend_cap_usd": 12.5,
        "status": "active",
        "created_at": listed.json()[0]["created_at"],
        "revoked_at": None,
    }]
    assert "key_hash" not in listed.text
    assert created["key"] not in listed.text

    with psycopg.connect(dsn) as pg:
        stored_hash, stored_last4 = pg.execute(
            "SELECT key_hash, key_last4 FROM api_keys WHERE id = %s", (created["id"],)
        ).fetchone()
    assert stored_hash == hashlib.sha256(created["key"].encode()).hexdigest()
    assert stored_hash != created["key"]
    assert stored_last4 == created["key_last4"]
    route_counts = {}
    for route in client.app.routes:
        for method in getattr(route, "methods", set()):
            route_counts[(method, route.path)] = route_counts.get((method, route.path), 0) + 1
    assert {key: route_counts[key] for key in (
        ("GET", "/api/tenants"), ("GET", "/api/keys"), ("POST", "/api/keys"),
        ("POST", "/api/keys/{key_id}/rotate"), ("POST", "/api/keys/{key_id}/revoke"),
    )} == {
        ("GET", "/api/tenants"): 1, ("GET", "/api/keys"): 1, ("POST", "/api/keys"): 1,
        ("POST", "/api/keys/{key_id}/rotate"): 1, ("POST", "/api/keys/{key_id}/revoke"): 1,
    }


def test_every_key_write_rejects_missing_or_invalid_bearer_token(keys_api_env):
    """Each C-2 write route fails closed before it can change a key."""
    client, _dsn = keys_api_env
    existing = _create(client)
    expected_rows = client.get("/api/keys", params={"tenant_id": DEFAULT_TENANT}).json()
    writes = (
        ("/api/keys", {"tenant_id": DEFAULT_TENANT}),
        (f"/api/keys/{existing['id']}/rotate", None),
        (f"/api/keys/{existing['id']}/revoke", None),
    )

    for path, payload in writes:
        missing = client.post(path, json=payload)
        invalid = client.post(path, json=payload, headers={"Authorization": "Bearer wrong"})

        assert missing.status_code == 401 and missing.json() == {"detail": "Unauthorized"}
        assert invalid.status_code == 401 and invalid.json() == {"detail": "Unauthorized"}
        assert client.get("/api/keys", params={"tenant_id": DEFAULT_TENANT}).json() == expected_rows


def test_rotate_creates_replacement_and_stamps_old_key_rotated(keys_api_env):
    client, _dsn = keys_api_env
    old = _create(client)

    response = client.post(f"/api/keys/{old['id']}/rotate", headers=_auth())
    assert response.status_code == 200, response.text
    rotated = response.json()
    assert set(rotated) == {"id", "key_last4", "key", "previous_status"}
    assert rotated["id"] != old["id"]
    assert rotated["previous_status"] == "rotated"

    rows = client.get("/api/keys", params={"tenant_id": DEFAULT_TENANT}).json()
    old_row = next(row for row in rows if row["id"] == old["id"])
    new_row = next(row for row in rows if row["id"] == rotated["id"])
    assert old_row["status"] == "rotated" and old_row["revoked_at"] is None
    assert new_row["status"] == "active" and new_row["key_last4"] == rotated["key_last4"]


def test_revoke_is_idempotent_and_keeps_original_revocation_time(keys_api_env):
    client, _dsn = keys_api_env
    created = _create(client)

    first = client.post(f"/api/keys/{created['id']}/revoke", headers=_auth())
    second = client.post(f"/api/keys/{created['id']}/revoke", headers=_auth())

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["status"] == second.json()["status"] == "revoked"
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


def test_create_rejects_unknown_tenant_and_bad_scopes_with_human_400(keys_api_env):
    client, _dsn = keys_api_env
    unknown = client.post(
        "/api/keys", json={"tenant_id": UNKNOWN_TENANT}, headers=_auth()
    )
    malformed_scopes = client.post(
        "/api/keys", json={"tenant_id": DEFAULT_TENANT, "scopes": [""]}, headers=_auth()
    )

    assert unknown.status_code == 400 and unknown.json() == {"detail": "Unknown tenant."}
    assert malformed_scopes.status_code == 400
    assert "scopes" in malformed_scopes.json()["detail"].lower()


def test_unconfigured_admin_token_is_generated_once_at_boot(monkeypatch, caplog):
    dsn = make_database(DB_NAME)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    monkeypatch.setenv("DATABASE_PATH", tempfile.mktemp(suffix=".db"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()
    from proxy import main as main_mod

    main_mod.app.state._state.pop("admin_token", None)
    try:
        with caplog.at_level(logging.INFO, logger="token-saver"):
            with TestClient(main_mod.app) as client:
                generated = main_mod.app.state.admin_token
                assert len(generated) >= 32
                response = client.post(
                    "/api/keys", json={"tenant_id": DEFAULT_TENANT},
                    headers={"Authorization": f"Bearer {generated}"},
                )
                assert response.status_code == 201, response.text
            assert caplog.text.count("generated at boot; not persisted") == 1
    finally:
        main_mod.app.state._state.pop("admin_token", None)
        get_settings.cache_clear()
        drop_database(DB_NAME)
