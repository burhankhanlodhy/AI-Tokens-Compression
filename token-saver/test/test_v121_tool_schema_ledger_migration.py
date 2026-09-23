"""v1.2.1 schema-ledger migration coverage for legacy Postgres volumes."""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE, unique_db_name, require_pg_base, make_database  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "token-saver/migrations/20260922_v121_tool_schema_ledger.sql"


@pytest.fixture()
def _legacy_db(monkeypatch):
    require_pg_base()
    name = unique_db_name("v121_ledger_legacy")
    dsn = f"{PG_BASE}/{name}"
    dsn = make_database(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("ALTER TABLE requests DROP COLUMN schema_cache_hit")
            db.execute("ALTER TABLE requests DROP COLUMN schema_bytes_saved")
            db.execute(MIGRATION.read_text(encoding="utf-8"))
            db.execute(MIGRATION.read_text(encoding="utf-8"))
        monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
        yield dsn
    finally:
        with psycopg.connect(PG_BASE, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name}")


def test_legacy_volume_migration_is_idempotent_and_ledger_persists_schema_cache_metrics(_legacy_db):
    from proxy import stats

    stats.log_request(
        model="openai/gpt-4o", route="compress",
        input_tokens_before=100, input_tokens_after=80, output_tokens=10,
        est_cost_before=0.01, est_cost_after=0.008, latency_ms=12,
        compressed=True, status=200, schema_cache_hit=True,
        schema_bytes_saved=128,
    )
    with psycopg.connect(_legacy_db) as db:
        row = db.execute(
            "SELECT schema_cache_hit, schema_bytes_saved FROM requests"
        ).fetchone()
    assert row == (True, 128)


def test_migration_is_wired_to_fresh_init_and_existing_volume_startup():
    compose = (REPO_ROOT / "token-saver/docker-compose.yml").read_text()
    dockerfile = (REPO_ROOT / "token-saver/proxy/Dockerfile").read_text()
    main = (REPO_ROOT / "token-saver/proxy/main.py").read_text()
    assert "20260922_v121_tool_schema_ledger.sql:/docker-entrypoint-initdb.d/" in compose
    assert "COPY migrations/ ./migrations/" in dockerfile
    assert "_apply_v121_tool_schema_migration()" in main
