"""Shared isolated-Postgres helpers for Phase-A acceptance tests."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

# One source of truth for every Postgres acceptance test.  Never guess a
# credential: CI/local acceptance runs must provide the admin DSN explicitly.
PG_BASE = os.environ.get("TOKEN_SAVER_PG_BASE", "")


def require_pg_base() -> str:
    if not PG_BASE:
        pytest.skip(
            "TOKEN_SAVER_PG_BASE must be set for Postgres acceptance tests",
            allow_module_level=False,
        )
    assert PG_BASE is not None
    return PG_BASE

SCHEMA = (Path(__file__).resolve().parents[2] / "postgres-schema-v2.sql").read_text()
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
