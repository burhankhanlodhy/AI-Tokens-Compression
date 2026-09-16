"""Optional production-Postgres gate for tests that otherwise use SQLite."""
from __future__ import annotations

import os

import psycopg
import pytest


def require_pg_dsn() -> str:
    """Return a reachable DSN or skip with an actionable, non-secret reason."""
    dsn = os.environ.get("TOKEN_SAVER_PG_DSN")
    if not dsn:
        pytest.skip("TOKEN_SAVER_PG_DSN must be set for Postgres-sensitive tests")
    assert dsn is not None

    try:
        with psycopg.connect(dsn, connect_timeout=3):
            pass
    except psycopg.Error:
        pytest.skip("TOKEN_SAVER_PG_DSN is unavailable for Postgres-sensitive tests")
    return dsn
