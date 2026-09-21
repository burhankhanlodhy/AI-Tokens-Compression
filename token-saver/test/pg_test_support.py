"""Shared isolated-Postgres helpers for Phase-A acceptance tests."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

# One source of truth for every Postgres acceptance test.  Never guess a
# credential: CI/local acceptance runs must provide the admin DSN explicitly.
PG_BASE = os.environ.get("TOKEN_SAVER_PG_BASE", "")


def unique_db_name(prefix: str) -> str:
    """Return a Postgres test database name isolated per process/xdist worker."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "").replace("-", "_")
    suffix = str(os.getpid())
    if worker:
        suffix = f"{suffix}_{worker}"
    return f"{prefix}_{suffix}"


def require_pg_base() -> str:
    if not PG_BASE:
        pytest.skip(
            "TOKEN_SAVER_PG_BASE must be set for Postgres acceptance tests",
            allow_module_level=False,
        )
    assert PG_BASE is not None
    return PG_BASE

SCHEMA = (Path(__file__).resolve().parents[2] / "postgres-schema-v2.sql").read_text()
# PC1/PC2 upgrade (pgvector image + semantic_cache_entries DDL), applied after
# the base schema on every pgvector-lane database.
PGVECTOR_MIGRATION = (
    Path(__file__).resolve().parents[2] / "token-saver/migrations/20260918_pc1_pgvector.sql"
).read_text()
# AC-PC2/PC3 response payload store, applied after the pgvector migration on
# every response-store-lane database.
PC2_MIGRATION = (
    Path(__file__).resolve().parents[2] / "token-saver/migrations/20260919_pc2_semantic_responses.sql"
).read_text()
PC5_MIGRATION = (
    Path(__file__).resolve().parents[2] / "token-saver/migrations/20260920_pc5_request_versions.sql"
).read_text()
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"
TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


def make_database(name: str) -> str:
    """Create a clean database, apply the committed schema, and seed providers."""
    base = require_pg_base()
    try:
        with psycopg.connect(base, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")
            pg.execute(f"CREATE DATABASE {name}")
    except psycopg.OperationalError:
        pytest.skip(
            "TOKEN_SAVER_PG_BASE is unavailable for Postgres acceptance tests",
            allow_module_level=False,
        )

    dsn = f"{base}/{name}"
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(SCHEMA)
        pg.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'default')",
            (DEFAULT_TENANT,),
        )
        pg.execute(
            """INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES
               ('legacy', 'https://openrouter.ai/api/v1', 'OpenAICompatAdapter', 'bearer'),
               ('openai', 'https://api.openai.com/v1', 'OpenAICompatAdapter', 'bearer')"""
        )
    return dsn


def make_pgvector_database(name: str) -> str:
    """Create a clean database, apply the base schema, then the pgvector migration.

    Requires a pgvector-capable server (the pinned pgvector/pgvector:0.8.6-pg16
    lane).  Used only by test_pc_pgvector_gates.py, which CI runs exclusively
    on that lane so the stock postgres:16 job never collects it.
    """
    dsn = make_database(name)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(PGVECTOR_MIGRATION)
        pg.execute(PC5_MIGRATION)
    return dsn


def make_response_store_database(name: str) -> str:
    """Base schema + pgvector migration + AC-PC2 response-store migration.

    Proves the committed 20260919_pc2_semantic_responses.sql applies cleanly
    (it is deliberately fire-once: any breakage surfaces here as an error,
    never as a silent skip).  Used only by test_pc2_response_store_gates.py on
    the pinned pgvector lane.
    """
    dsn = make_pgvector_database(name)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(PC2_MIGRATION)
    return dsn


def drop_database(name: str) -> None:
    try:
        with psycopg.connect(PG_BASE, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")
    except psycopg.OperationalError:
        pass


def ledger_count(dsn: str) -> int:
    with psycopg.connect(dsn) as pg:
        return int(pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0])


def ledger_routes(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as pg:
        return [r[0] for r in pg.execute("SELECT route FROM requests ORDER BY id").fetchall()]
