"""T1 ledger-migration sequence regression (review finding 1).

Fresh-volume and existing-volume deployments must BOTH reach a database whose
``requests`` ledger carries ``tool_compression_saved`` with the exact type and
default the writer relies on:

- fresh volume (Compose initdb): the canonical base schema declares the final
  column AND ``docker-compose.yml`` executes the additive migration afterwards
  — the duplicate ``ADD COLUMN`` must be a no-op, not an initdb failure;
- existing volume (upgrade runbook): a pre-T1 schema upgrades by applying only
  the T1 migration.

The test applies the exact SQL files Compose mounts, in the same order, and
asserts the column contract afterwards. It also fails if the migration is
non-idempotent (second application must succeed).
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE, unique_db_name, require_pg_base  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_SCHEMA = REPO_ROOT / "postgres-schema-v2.sql"
T1_MIGRATION = (
    REPO_ROOT / "token-saver/migrations/20260922_t1_tool_compression_ledger.sql"
)
# The single line T1 added to the canonical schema. Removing it from the
# committed file yields the pre-T1 schema an existing volume carries — no git
# history needed, so this works in any checkout (CI actions/checkout is
# shallow and has no master ref to `git show`).
T1_SCHEMA_LINE = (
    "    tool_compression_saved  INTEGER NOT NULL DEFAULT 0,           "
    "-- T1: tool-result/schema lossless savings; attribution subset, never additive with L1 totals\n"
)
EXPECTED = "integer|0"


def _pre_t1_schema() -> str:
    """The pre-T1 base schema: the committed file minus the T1 column line."""
    text = BASE_SCHEMA.read_text()
    assert T1_SCHEMA_LINE in text, (
        "T1 column declaration moved in postgres-schema-v2.sql; update "
        "T1_SCHEMA_LINE in this test to match it exactly"
    )
    return text.replace(T1_SCHEMA_LINE, "")


def _apply(db_dsn: str, sql: str) -> None:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        pg.execute(sql)


def _column_contract(db_dsn: str) -> str:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        row = pg.execute(
            "SELECT data_type || '|' || COALESCE(column_default, '') "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'requests' "
            "AND column_name = 'tool_compression_saved'"
        ).fetchone()
    return row[0] if row else "MISSING"


@pytest.fixture()
def _fresh_db():
    require_pg_base()
    name = unique_db_name("t1_seq_fresh")
    dsn = f"{PG_BASE}/{name}"
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    yield dsn
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")


@pytest.fixture()
def _upgrade_db():
    require_pg_base()
    name = unique_db_name("t1_seq_upgrade")
    dsn = f"{PG_BASE}/{name}"
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    yield dsn
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")


def test_fresh_volume_schema_then_migration_sequence(_fresh_db):
    """initdb order: base schema first, then the mounted T1 migration."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    # The migration must be a no-op after the schema already declared the
    # column — a duplicate ADD COLUMN here aborts initdb and the proxy
    # container never starts (the exact regression this pins).
    _apply(_fresh_db, T1_MIGRATION.read_text())
    assert _column_contract(_fresh_db) == EXPECTED


def test_upgrade_volume_migration_only_sequence(_upgrade_db):
    """Existing pre-T1 volume: apply only the T1 migration."""
    _apply(_upgrade_db, _pre_t1_schema())
    _apply(_upgrade_db, T1_MIGRATION.read_text())
    assert _column_contract(_upgrade_db) == EXPECTED


def test_t1_migration_is_idempotent(_fresh_db):
    """A repeated migration run (ops re-run) must succeed, not fail closed."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    _apply(_fresh_db, T1_MIGRATION.read_text())
    _apply(_fresh_db, T1_MIGRATION.read_text())
    assert _column_contract(_fresh_db) == EXPECTED
