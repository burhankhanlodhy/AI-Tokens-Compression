"""V2.2 runtime settings migration regression (t_d86fe22b).

Migration 8 (``migrations/20260925_v22_runtime_settings.sql``) adds the
``app_settings`` runtime-override table. Pins migration 8 to the same
standard as every earlier MIGRATIONS.md entry:

- fresh volume (Compose initdb): ``postgres-schema-v2.sql`` first, then the
  mounted ``50-v22-runtime-settings.sql`` — the idempotent re-apply must be
  a no-op, not an initdb failure;
- existing volume (upgrade runbook): a pre-V2.2 schema upgrades by applying
  only migration 8, and a repeated ops run must still succeed (rows survive);
- constraint rejection: an empty ``updated_by`` is rejected by
  ``chk_app_settings_updated_by`` (the audit identity is never blank);
- no regression to the requests ledger contract (byte-identical DDL
  fingerprint before/after) and no tenant scoping added to app_settings;
- wiring: Compose initdb mount present and ordered after the V2.1 session
  stores, CI standard-lane bootstrap lists the migration, MIGRATIONS.md
  documents it as entry 8, and the writer allowlist in proxy/settings.py
  matches the PM §3.1 contract exactly (runtime set disjoint from the
  deployment-only set; every allowlisted name is a real Settings field).

Requires a Postgres with the committed schema available (skips cleanly
without TOKEN_SAVER_PG_BASE like the other Postgres acceptance suites).
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg.conninfo  # noqa: E402
from pg_test_support import PG_BASE, unique_db_name, require_pg_base  # noqa: E402


def _db_dsn(name: str) -> str:
    """PG_BASE with the target database overridden — works whether or not
    the base DSN carries a default dbname path component (CI sets a bare
    host DSN; local lanes commonly end in /postgres)."""
    info = psycopg.conninfo.conninfo_to_dict(PG_BASE)
    info["dbname"] = name
    return psycopg.conninfo.make_conninfo(**info)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_SCHEMA = REPO_ROOT / "postgres-schema-v2.sql"
V22_MIGRATION = (
    REPO_ROOT / "token-saver/migrations/20260925_v22_runtime_settings.sql"
)

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"

_LEDGER_DDL_QUERY = """
SELECT column_name || ':' || data_type || ':' || is_nullable
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'requests'
ORDER BY ordinal_position
"""


def _apply(db_dsn: str, sql: str) -> None:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        pg.execute(sql)


def _ledger_fingerprint(db_dsn: str) -> str:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        return "|".join(r[0] for r in pg.execute(_LEDGER_DDL_QUERY).fetchall())


def _app_settings_columns(db_dsn: str) -> list[tuple[str, str, str]]:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        return [
            (r[0], r[1], r[2])
            for r in pg.execute(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'app_settings'
                   ORDER BY ordinal_position"""
            ).fetchall()
        ]


def _constraints(db_dsn: str) -> list[str]:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        return [
            r[0]
            for r in pg.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'app_settings'::regclass ORDER BY conname"
            ).fetchall()
        ]


# ------------------------------------------------------------ fresh volume

def test_fresh_volume_schema_then_migration_is_a_clean_no_op_on_reapply():
    """initdb order: base schema first, then the mounted V2.2 migration.

    The migration must apply cleanly over the canonical schema (the schema
    does NOT pre-declare app_settings) and a second ops run must be a no-op
    (Postgres DDL is transactional, so the re-apply proves the idempotency
    design; a loud second failure would mean fire-once, which is wrong for
    a file that is also mounted into initdb)."""
    base = require_pg_base()
    name = unique_db_name("ts_v22_settings_fresh")
    with psycopg.connect(base, autocommit=True, connect_timeout=3) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    dsn = _db_dsn(name)
    try:
        _apply(dsn, BASE_SCHEMA.read_text())
        _apply(dsn, f"INSERT INTO tenants (id, name) VALUES ('{DEFAULT_TENANT}', 'default')")
        migration_sql = V22_MIGRATION.read_text()

        _apply(dsn, migration_sql)
        _apply(dsn, migration_sql)  # re-apply: must be a silent no-op

        columns = _app_settings_columns(dsn)
        assert [(c[0], c[2]) for c in columns] == [
            ("name", "NO"), ("value", "NO"),
            ("updated_at", "NO"), ("updated_by", "NO"),
        ]
        assert columns[0][1] == "text" and columns[1][1] == "boolean"
        assert "chk_app_settings_updated_by" in _constraints(dsn)
        # No tenant scoping on operator settings (per-deployment state).
        assert "tenant_id" not in {c[0] for c in columns}
    finally:
        with psycopg.connect(base, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")


# ---------------------------------------------------------- upgrade volume

def test_upgrade_volume_migration_only_sequence_preserves_rows_and_ledger():
    """Existing pre-V2.2 volume: apply only migration 8; repeated runs keep
    rows; the requests ledger DDL is byte-identical before/after."""
    base = require_pg_base()
    name = unique_db_name("ts_v22_settings_upgrade")
    with psycopg.connect(base, autocommit=True, connect_timeout=3) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    dsn = _db_dsn(name)
    try:
        _apply(dsn, BASE_SCHEMA.read_text())
        _apply(dsn, f"INSERT INTO tenants (id, name) VALUES ('{DEFAULT_TENANT}', 'default')")
        before = _ledger_fingerprint(dsn)

        migration_sql = V22_MIGRATION.read_text()
        _apply(dsn, migration_sql)
        _apply(
            dsn,
            "INSERT INTO app_settings (name, value, updated_by) "
            "VALUES ('l1_enabled', false, 'admin')",
        )
        _apply(dsn, migration_sql)  # repeated ops run

        with psycopg.connect(dsn, autocommit=True) as pg:
            row = pg.execute(
                "SELECT name, value, updated_by FROM app_settings"
            ).fetchone()
        assert row == ("l1_enabled", False, "admin")
        assert _ledger_fingerprint(dsn) == before, (
            "migration 8 must not touch the requests ledger contract"
        )
    finally:
        with psycopg.connect(base, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")


# --------------------------------------------------------- constraint gate

def test_empty_updated_by_is_rejected():
    base = require_pg_base()
    name = unique_db_name("ts_v22_settings_check")
    with psycopg.connect(base, autocommit=True, connect_timeout=3) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    dsn = _db_dsn(name)
    try:
        _apply(dsn, BASE_SCHEMA.read_text())
        _apply(dsn, V22_MIGRATION.read_text())
        with psycopg.connect(dsn, autocommit=True) as pg:
            with pytest.raises(psycopg.errors.CheckViolation):
                pg.execute(
                    "INSERT INTO app_settings (name, value, updated_by) "
                    "VALUES ('l1_enabled', true, '')"
                )
    finally:
        with psycopg.connect(base, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")


# --------------------------------------------------------------- wiring

def test_migration_is_wired_to_fresh_init_ci_and_documented():
    """Compose initdb mount ordered after the V2.1 session stores; CI
    standard-lane bootstrap lists it; MIGRATIONS.md documents entry 8."""
    compose = (REPO_ROOT / "token-saver/docker-compose.yml").read_text()
    migrations_md = (REPO_ROOT / "MIGRATIONS.md").read_text()
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    v21_mount = "20260924_v21_session_stores.sql:/docker-entrypoint-initdb.d/48-v21-session-stores.sql"
    v22_mount = "20260925_v22_runtime_settings.sql:/docker-entrypoint-initdb.d/50-v22-runtime-settings.sql"
    assert v22_mount in compose
    assert compose.index(v21_mount) < compose.index(v22_mount), (
        "V2.2 settings mount must follow the V2.1 session stores in initdb order"
    )
    assert "migrations/20260925_v22_runtime_settings.sql" in ci
    assert "migrations/20260925_v22_runtime_settings.sql" in migrations_md
    assert "50-v22-runtime-settings.sql" in migrations_md


def test_writer_allowlist_matches_the_pm_contract():
    """proxy/settings.py carries the exact PM §3.1 allowlist; the runtime and
    deployment-only sets are disjoint; every name is a real Settings field
    so `effective()` can never AttributeError on a typo."""
    from proxy.config import Settings
    from proxy.settings import DEPLOYMENT_ONLY, RUNTIME_ALLOWED

    assert set(RUNTIME_ALLOWED) == {
        "l1_enabled",
        "tool_schema_minify",
        "tool_schema_cache_enabled",
        "tool_result_optimization",
        "tool_result_compression_enabled",
        "output_conciseness_enabled",
        "semantic_cache_enabled",
    }
    assert not set(RUNTIME_ALLOWED) & set(DEPLOYMENT_ONLY)
    field_names = set(Settings.model_fields)
    unknown = (set(RUNTIME_ALLOWED) | set(DEPLOYMENT_ONLY)) - field_names
    assert not unknown, f"allowlist names missing from Settings: {sorted(unknown)}"
    # Secrets never appear in the runtime-writable set.
    assert not any("token" in n or "key" in n or "secret" in n for n in RUNTIME_ALLOWED)
