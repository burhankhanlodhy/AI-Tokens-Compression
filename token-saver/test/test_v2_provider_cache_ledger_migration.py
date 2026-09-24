"""V2.0 provider-native cache usage ledger migration (t_ef7f0f71, AC-V2-4/6).

Fresh-volume and existing-volume deployments must BOTH reach a database whose
``requests`` ledger carries the provider-returned cache usage columns with the
exact type and nullability the attribution contract requires:

- fresh volume (Compose initdb): the canonical base schema declares the final
  columns AND ``docker-compose.yml`` executes the additive migration afterwards
  — the duplicate ``ADD COLUMN`` must be a no-op, not an initdb failure;
- existing volume (upgrade runbook): a pre-V2 schema upgrades by applying only
  the V2 migration, and rows that predate the migration read back NULL (the
  provider returned no observable cache evidence for them).

The attribution contract itself is pinned too:
- NULL means "no provider cache usage evidence" while 0 means "provider
  reported zero cached tokens" — the two states are distinguishable;
- measured usage is stored on the ledger without touching cache_savings /
  l1_savings / tool_compression_saved (no double count, AC-V2-4);
- the migration is idempotent (a repeated ops run must succeed).
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
V2_MIGRATION = (
    REPO_ROOT / "token-saver/migrations/20260924_v2_provider_cache_usage.sql"
)
# The two lines the V2 change added to the canonical schema. Removing them from
# the committed file yields the pre-V2 schema an existing volume carries — no
# git history needed, so this works in any checkout (CI actions/checkout is
# shallow and has no master ref to `git show`).
V2_SCHEMA_LINES = (
    "    provider_cache_read_tokens  INTEGER NULL,                    "
    "-- Anthropic cache_read_input_tokens / OpenAI-compat prompt_tokens_details.cached_tokens\n"
    "    provider_cache_write_tokens INTEGER NULL,                    "
    "-- Anthropic cache_creation_input_tokens; attribution only\n"
)
PROVIDER_CACHE_COLUMNS = ("provider_cache_read_tokens", "provider_cache_write_tokens")


def _pre_v2_schema() -> str:
    """The pre-V2 base schema: the committed file minus the V2 column lines."""
    text = BASE_SCHEMA.read_text()
    assert V2_SCHEMA_LINES in text, (
        "V2 column declarations moved in postgres-schema-v2.sql; update "
        "V2_SCHEMA_LINES in this test to match them exactly"
    )
    return text.replace(V2_SCHEMA_LINES, "")


def _apply(db_dsn: str, sql: str) -> None:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        pg.execute(sql)


def _column_contract(db_dsn: str) -> dict[str, str]:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        rows = pg.execute(
            "SELECT column_name, data_type || '|' || COALESCE(column_default, '')"
            " || '|' || is_nullable"
            " FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'requests'"
            "   AND column_name IN ('provider_cache_read_tokens',"
            "                       'provider_cache_write_tokens')"
        ).fetchall()
    return dict(rows)


def test_fresh_volume_schema_then_migration_sequence(_fresh_db):
    """initdb order: base schema first, then the mounted V2 migration."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    # The migration must be a no-op after the schema already declared the
    # columns — a duplicate ADD COLUMN here aborts initdb and the proxy
    # container never starts (the T1 regression class; pinned again here).
    _apply(_fresh_db, V2_MIGRATION.read_text())
    contract = _column_contract(_fresh_db)
    assert set(contract) == set(PROVIDER_CACHE_COLUMNS)
    for spec in contract.values():
        data_type, default, nullable = spec.split("|")
        assert data_type == "integer"
        assert default == ""          # no default: absence of evidence is NULL
        assert nullable == "YES"      # NULL = provider returned no usage fields


def test_upgrade_volume_migration_only_sequence(_upgrade_db):
    """Existing pre-V2 volume: apply only the V2 migration."""
    _apply(_upgrade_db, _pre_v2_schema())
    _apply(_upgrade_db, V2_MIGRATION.read_text())
    contract = _column_contract(_upgrade_db)
    assert set(contract) == set(PROVIDER_CACHE_COLUMNS)
    for spec in contract.values():
        data_type, default, nullable = spec.split("|")
        assert data_type == "integer"
        assert default == ""
        assert nullable == "YES"


def test_v2_migration_is_idempotent(_fresh_db):
    """A repeated migration run (ops re-run) must succeed, not fail closed."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    _apply(_fresh_db, V2_MIGRATION.read_text())
    _apply(_fresh_db, V2_MIGRATION.read_text())
    assert set(_column_contract(_fresh_db)) == set(PROVIDER_CACHE_COLUMNS)


def test_preexisting_rows_read_null_not_zero(_upgrade_db):
    """AC-V2-6: rows written before the migration carry no cache evidence.

    NULL (no evidence) must be distinguishable from 0 (provider reported zero
    cached tokens). A legacy backfilled ledger must read back NULL, never a
    fabricated 0 that would look like observed provider usage.
    """
    _apply(_upgrade_db, _pre_v2_schema())
    _apply(_upgrade_db, V2_MIGRATION.read_text())
    with psycopg.connect(_upgrade_db, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO tenants (id, name) VALUES"
            " ('00000000-0000-0000-0000-000000000000', 'default')"
        )
        pg.execute(
            "INSERT INTO providers (name, base_url, adapter_class, auth_style)"
            " VALUES ('legacy', 'https://openrouter.ai/api/v1',"
            " 'OpenAICompatAdapter', 'bearer')"
        )
        pg.execute(
            """INSERT INTO requests (tenant_id, provider_id, model, route,
                   input_tokens_before, input_tokens_after, output_tokens)
               SELECT t.id, p.id, 'test-model', 'compress', 100, 80, 10
                 FROM tenants t, providers p
                WHERE t.id = '00000000-0000-0000-0000-000000000000'
                  AND p.name = 'legacy'"""
        )
        row = pg.execute(
            "SELECT provider_cache_read_tokens, provider_cache_write_tokens"
            " FROM requests"
        ).fetchone()
    assert row[0] is None and row[1] is None


def test_attribution_dimensions_stay_disjoint(_fresh_db):
    """AC-V2-4: measured provider usage is its own attribution lane.

    A row carrying provider cache usage must accept simultaneous values in the
    provider-native columns while cache_savings / l1_savings /
    tool_compression_saved remain independently settable and NULL — nothing in
    the write path or schema merges the lanes (the decomposition invariants
    hold at row level; bucket-level reconciliation stays in the QA suite).
    """
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    _apply(_fresh_db, V2_MIGRATION.read_text())
    with psycopg.connect(_fresh_db, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO tenants (id, name) VALUES"
            " ('00000000-0000-0000-0000-000000000000', 'default')"
        )
        pg.execute(
            "INSERT INTO providers (name, base_url, adapter_class, auth_style)"
            " VALUES ('anthropic', 'https://api.anthropic.com',"
            " 'AnthropicAdapter', 'x-api-key')"
        )
        pg.execute(
            """INSERT INTO requests (tenant_id, provider_id, model, route,
                   input_tokens_before, input_tokens_after, output_tokens,
                   cache_status, cache_savings,
                   l1_tokens_stripped, l1_savings,
                   tool_compression_saved,
                   provider_cache_read_tokens, provider_cache_write_tokens)
               SELECT t.id, p.id, 'claude-test', 'compress', 1000, 900, 10,
                      'exact_hit', 0.5::numeric,
                      50, 0.25::numeric,
                      12,
                      640, 1000
                 FROM tenants t, providers p
                WHERE t.id = '00000000-0000-0000-0000-000000000000'
                  AND p.name = 'anthropic'"""
        )
        row = pg.execute(
            """SELECT provider_cache_read_tokens, provider_cache_write_tokens,
                      cache_savings, l1_savings, tool_compression_saved
                 FROM requests"""
        ).fetchone()
    assert row == (640, 1000, 0.5, 0.25, 12)


@pytest.fixture()
def _fresh_db():
    require_pg_base()
    name = unique_db_name("v2cache_fresh")
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
    name = unique_db_name("v2cache_upgrade")
    dsn = f"{PG_BASE}/{name}"
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    yield dsn
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
