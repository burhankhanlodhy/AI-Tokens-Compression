"""V2.2 runtime settings store + API gates (t_d86fe22b, PM spec §3.1/§3.2).

Two layers are pinned here:

- proxy/settings.py store semantics: allowlist enforcement (unknown /
  deployment-only / non-boolean writes are refused BEFORE any storage
  touch), precedence runtime > env > default, delete-reverts-immediately,
  degraded read vs strict write, and the JSON fallback backend for
  SQLite-only deployments (open decision #4).
- /api/settings over the wire (TestClient on the real ASGI app): GET
  unauthenticated per the G2 ruling, PUT/DELETE ADMIN_TOKEN-gated (401),
  field-named 400s (B-10), 503 when the PG store is unreachable, source
  chips (runtime|env|default) in every item, the D-3 locked_reason on
  semantic_cache_enabled, and the secret-handling extension of AC-A13:
  admin_token/measurement_tag are presence-masked and no API path can
  ever return or persist a secret value.

Postgres-backed tests need TOKEN_SAVER_PG_BASE (skip cleanly without);
the JSON-backend and API-shape tests run everywhere.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy.config import get_settings  # noqa: E402
from proxy.settings import (  # noqa: E402
    DEPLOYMENT_ONLY,
    RUNTIME_ALLOWED,
    InvalidSettingValueError,
    NotRuntimeConfigurableError,
    SettingsStore,
    UnknownSettingError,
    reset_settings_store,
)

ADMIN_TOKEN = "v22-settings-admin-token"


# --------------------------------------------------------------- fixtures

@pytest.fixture()
def json_store(tmp_path):
    """Store pinned to a JSON settings file; no PG DSN in env."""
    import os

    old_dsn = os.environ.pop("TOKEN_SAVER_PG_DSN", None)
    store = SettingsStore(sqlite_path=str(tmp_path / "ledger.db"))
    yield store
    if old_dsn is not None:
        os.environ["TOKEN_SAVER_PG_DSN"] = old_dsn


@pytest.fixture()
def api_env(monkeypatch, tmp_path):
    """Real ASGI app with a JSON settings backend + throwaway SQLite ledger."""
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", "")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    get_settings.cache_clear()
    reset_settings_store()

    from proxy import main as main_mod

    main_mod.app.state._state.pop("admin_token", None)
    try:
        with TestClient(main_mod.app) as client:
            yield client, tmp_path
    finally:
        main_mod.app.state._state.pop("admin_token", None)
        reset_settings_store()
        get_settings.cache_clear()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ------------------------------------------------- allowlist enforcement

def test_unknown_name_is_refused(json_store):
    with pytest.raises(UnknownSettingError):
        json_store.write_override("not_a_setting", True)


def test_deployment_only_name_is_refused_before_storage(json_store):
    # A deployment-only control must never become runtime-writable — and
    # the refusal happens before any file is created (no partial writes).
    for name in ("compression_rate", "admin_token", "semantic_cache_ttl_seconds"):
        assert name in DEPLOYMENT_ONLY
        with pytest.raises(NotRuntimeConfigurableError):
            json_store.write_override(name, True)
    assert json_store.load_overrides() == {}


def test_non_boolean_value_is_refused(json_store):
    """PM §3.1: no runtime path can persist a number, string, URL, or JSON
    blob — the write-only surface is booleans, structurally."""
    for bad in (1, 0, "true", None, {"value": True}, [True], 3.14):
        with pytest.raises(InvalidSettingValueError):
            json_store.write_override("l1_enabled", bad)
    assert json_store.load_overrides() == {}


def test_every_allowlisted_name_is_a_real_boolean_settings_field():
    """effective() getattr's Settings by name — a typo would AttributeError
    at request time. Every allowlisted field must exist and default bool."""
    settings = get_settings()
    for name in RUNTIME_ALLOWED:
        value = getattr(settings, name)
        assert isinstance(value, bool), name


# ------------------------------------------------------- precedence (I1)

def test_precedence_runtime_over_env_over_default(json_store, monkeypatch):
    # default
    item = json_store.effective("l1_enabled")
    assert item["source"] == "default"
    assert item["value"] == item["default"]

    # env override wins over default
    env_now = item["value"]
    monkeypatch.setenv("L1_ENABLED", str(not env_now).lower())
    item = json_store.effective("l1_enabled")
    assert item["source"] == "env"
    assert item["value"] is (not env_now)

    # runtime override wins over env
    json_store.write_override("l1_enabled", env_now)
    item = json_store.effective("l1_enabled")
    assert item["source"] == "runtime"
    assert item["value"] is env_now

    # delete reverts immediately to env (no restart, no cache)
    assert json_store.delete_override("l1_enabled") is True
    item = json_store.effective("l1_enabled")
    assert item["source"] == "env"
    assert item["value"] is (not env_now)

    # deleting a never-overridden name is an idempotent no-op, not an error
    assert json_store.delete_override("l1_enabled") is False


def test_persists_across_store_restart(json_store):
    """AC-I-persist: a new store instance (proxy restart) sees the override."""
    json_store.write_override("tool_schema_minify", False)
    revived = SettingsStore(sqlite_path=json_store._sqlite_file())
    item = revived.effective("tool_schema_minify")
    assert item["source"] == "runtime" and item["value"] is False


def test_unparseable_env_falls_back_to_default(json_store, monkeypatch):
    """A non-boolean env literal degrades softly: _parse_bool treats it as
    false with a loud warning and effective() never raises (pydantic-settings
    rejects the same literal at boot — config.py's contract — so this path
    only fires for values injected after boot: tests, shells)."""
    monkeypatch.setenv("L1_ENABLED", "maybe")
    item = json_store.effective("l1_enabled")
    assert item["source"] in ("env", "default")
    assert item["value"] is False


# --------------------------------------------------- degraded vs strict

def test_pg_read_failure_degrades_and_write_surfaces(monkeypatch):
    """B4/§4 honesty: read degrades to env/default (traffic keeps flowing);
    write raises so the API can 503 an unacknowledged toggle."""
    store = SettingsStore(pg_dsn="postgresql://invalid-host:59999/none")
    # read: degraded, no exception
    assert store.load_overrides() == {}
    # write: loud failure
    with pytest.raises(Exception):
        store.write_override("l1_enabled", True)
    with pytest.raises(Exception):
        store.read_overrides_strict()


# ------------------------------------------------------ JSON corrupt file

def test_corrupt_json_settings_file_is_ignored(json_store, monkeypatch):
    json_store.write_override("l1_enabled", False)
    path = json_store._sqlite_file()
    path.write_text("{not json", encoding="utf-8")
    assert json_store.load_overrides() == {}
    item = json_store.effective("l1_enabled")
    assert item["source"] == "default"


def test_foreign_or_malformed_json_entries_are_dropped(json_store):
    """A hand-edited settings.json cannot smuggle in a non-allowlisted name
    or a non-boolean value."""
    path = json_store._sqlite_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "l1_enabled": {"value": True, "updated_by": "admin"},
        "admin_token": {"value": "hunter2"},       # non-allowlisted name
        "tool_schema_minify": "yes",               # non-boolean payload
    }), encoding="utf-8")
    overrides = json_store.load_overrides()
    assert set(overrides) == {"l1_enabled"}
    assert overrides["l1_enabled"][0] is True


# ------------------------------------------------------------- API shape

def test_api_get_is_unauthenticated_and_carries_sources(api_env):
    client, _ = api_env
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    items = resp.json()["settings"]
    by_name = {i["name"]: i for i in items}
    # every PM §3.1 runtime control present with a source chip
    for name in RUNTIME_ALLOWED:
        assert by_name[name]["source"] in ("runtime", "env", "default")
        assert by_name[name]["category"] == "runtime_configurable"
    # deployment inventory present, marked read-only
    for name in ("compression_rate", "admin_token"):
        assert by_name[name]["category"] == "deployment_only"


def test_api_secret_handling_presence_mask_only(api_env):
    """AC-A13 extension: admin_token/measurement_tag never render their
    values — a presence indicator at most — on ANY settings read."""
    client, _ = api_env
    items = client.get("/api/settings").json()["settings"]
    by_name = {i["name"]: i for i in items}
    assert by_name["admin_token"]["value"] in (None, "••••")
    assert by_name["measurement_tag"]["value"] in (None, "••••")
    assert by_name["admin_token"]["value"] != ADMIN_TOKEN


def test_api_put_requires_admin_and_validates(api_env):
    client, _ = api_env
    # 401: no token, wrong token
    assert client.put("/api/settings/l1_enabled", json={"value": True}).status_code == 401
    assert client.put(
        "/api/settings/l1_enabled", json={"value": True},
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    # 400 field-named: unknown / deployment-only / non-boolean / missing value
    for name, payload, field in [
        ("nope", {"value": True}, "name"),
        ("admin_token", {"value": "x"}, "name"),
        ("l1_enabled", {"value": "yes"}, "value"),
        ("l1_enabled", {}, "value"),
    ]:
        resp = client.put(f"/api/settings/{name}", json=payload, headers=_auth())
        assert resp.status_code == 400, (name, resp.text)
        assert field in resp.json()["detail"], (name, resp.json())
    # malformed body
    resp = client.put("/api/settings/l1_enabled", content=b"{bad", headers=_auth())
    assert resp.status_code == 400


def test_api_put_write_roundtrip_and_delete_revert(api_env):
    client, tmp = api_env
    current = client.get("/api/settings").json()["settings"]
    l1 = next(i for i in current if i["name"] == "l1_enabled")
    flipped = not l1["value"]

    put = client.put("/api/settings/l1_enabled", json={"value": flipped}, headers=_auth())
    assert put.status_code == 200, put.text
    assert put.json()["source"] == "runtime"
    assert put.json()["value"] is flipped

    # persisted on disk (restart-survivable) and visible in GET
    persisted = json.loads((tmp / "settings.json").read_text())
    assert persisted["l1_enabled"]["value"] is flipped
    get = client.get("/api/settings").json()["settings"]
    assert next(i for i in get if i["name"] == "l1_enabled")["value"] is flipped

    # DELETE reverts immediately; idempotent second delete is still 200
    assert client.delete("/api/settings/l1_enabled", headers=_auth()).status_code == 200
    assert client.delete("/api/settings/l1_enabled", headers=_auth()).status_code == 200
    after = client.get("/api/settings").json()["settings"]
    reverted = next(i for i in after if i["name"] == "l1_enabled")
    assert reverted["source"] in ("env", "default")
    assert reverted["value"] is not flipped


def test_api_delete_requires_admin(api_env):
    client, _ = api_env
    assert client.delete("/api/settings/l1_enabled").status_code == 401
    assert client.delete(
        "/api/settings/l1_enabled", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_api_locked_reason_on_semantic_cache_toggle(api_env):
    """D-3: semantic_cache_enabled carries locked_reason whenever the gate
    (ratified threshold + committed calibration artifact) holds; every
    other toggle never carries it."""
    client, _ = api_env
    items = client.get("/api/settings").json()["settings"]
    by_name = {i["name"]: i for i in items}
    if by_name["semantic_cache_enabled"].get("locked_reason"):
        assert by_name["semantic_cache_enabled"]["value"] is False
        assert isinstance(by_name["semantic_cache_enabled"]["locked_reason"], str)
    for name in RUNTIME_ALLOWED:
        if name != "semantic_cache_enabled":
            assert "locked_reason" not in by_name[name]


def test_api_settings_store_down_is_503(api_env, monkeypatch):
    """A broken PG store on GET/PUT/DELETE is the shared 503 envelope —
    never a 500 and never a misleading all-defaults 200."""
    from proxy import main as main_mod
    from proxy.settings import SettingsStore

    client, _ = api_env
    broken = SettingsStore(pg_dsn="postgresql://invalid-host:59999/none")
    monkeypatch.setattr(main_mod, "get_settings_store", lambda: broken)
    get = client.get("/api/settings")
    assert get.status_code == 503
    assert "error" in get.json()
    put = client.put("/api/settings/l1_enabled", json={"value": True}, headers=_auth())
    assert put.status_code == 503
    delete = client.delete("/api/settings/l1_enabled", headers=_auth())
    assert delete.status_code == 503


# -------------------------------------------------------- snapshot (B4)

def test_snapshot_is_plain_dict_and_independent_of_later_writes(json_store):
    json_store.write_override("l1_enabled", True)
    snap = json_store.snapshot()
    assert set(snap) == set(RUNTIME_ALLOWED)
    assert snap["l1_enabled"] is True
    json_store.write_override("l1_enabled", False)
    assert snap["l1_enabled"] is True  # in-flight request unaffected


def test_inventory_never_leaks_secret_values(json_store, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "super-secret-value")
    get_settings.cache_clear()
    try:
        items = json_store.inventory()
        by_name = {i["name"]: i for i in items}
        assert by_name["admin_token"]["value"] in (None, "••••")
        dumped = json.dumps(items)
        assert "super-secret-value" not in dumped
    finally:
        get_settings.cache_clear()


# ------------------------------------------------- PG-backed store parity

@pytest.fixture()
def pg_store_env():
    pytest.importorskip("psycopg")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pg_test_support import PG_BASE, unique_db_name, require_pg_base
    import psycopg

    base = require_pg_base()
    if not PG_BASE:
        pytest.skip("TOKEN_SAVER_PG_BASE not set")
    name = unique_db_name("ts_v22_settings_store")
    try:
        with psycopg.connect(base, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")
            pg.execute(f"CREATE DATABASE {name}")
    except psycopg.OperationalError:
        pytest.skip("TOKEN_SAVER_PG_BASE unavailable", allow_module_level=False)
    import psycopg.conninfo

    info = psycopg.conninfo.conninfo_to_dict(PG_BASE)
    info["dbname"] = name
    dsn = psycopg.conninfo.make_conninfo(**info)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            (Path(__file__).resolve().parents[2]
             / "token-saver/migrations/20260925_v22_runtime_settings.sql").read_text()
        )
    store = SettingsStore(pg_dsn=dsn)
    yield store
    with psycopg.connect(base, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")


def test_pg_store_roundtrip_matches_json_semantics(pg_store_env):
    store = pg_store_env
    store.write_override("l1_enabled", True, updated_by="ops-key-1")
    overrides = store.load_overrides()
    assert overrides["l1_enabled"][0] is True
    assert overrides["l1_enabled"][2] == "ops-key-1"
    assert overrides["l1_enabled"][1] is not None
    item = store.effective("l1_enabled")
    assert item["source"] == "runtime"
    assert store.delete_override("l1_enabled") is True
    assert store.effective("l1_enabled")["source"] in ("env", "default")
    # empty updated_by is rejected by the DB constraint, surfaced as failure
    with pytest.raises(Exception):
        store.write_override("l1_enabled", True, updated_by="   ")


def test_pg_store_write_persists_across_instances(pg_store_env):
    store = pg_store_env
    store.write_override("tool_result_optimization", False)
    revived = SettingsStore(pg_dsn=store._dsn())
    assert revived.effective("tool_result_optimization")["value"] is False
