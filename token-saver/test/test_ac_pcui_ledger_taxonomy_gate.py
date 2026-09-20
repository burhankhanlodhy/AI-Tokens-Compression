"""AC-PC-UI gate: the ledger's cache_status constraint must accept the full
frozen taxonomy on real Postgres.

QA/PM finding (2026-09-20): the live constraint is
    chk_cache_status CHECK (cache_status IN ('miss','exact_hit','semantic_hit'))
but the v1.1 dashboard spec (cache-status-dashboard-spec.md, AC-PC-UI) freezes
the taxonomy at four values, adding `semantic_threshold_miss`.  The writer
swallows ledger failures at main.py's `except Exception: LEDGER_WRITE_FAILURES
+= 1`, so the first threshold-miss request would return 200 while the ENTIRE
ledger row (savings, tokens, cost) is silently dropped — the dashboard and
/metrics compute from exactly those rows.  The SQLite fallback INSERT in
stats.py does not carry cache_status at all, so the unit suite cannot catch
this: only real Postgres can.

Owned here (base-schema lane, stock postgres:16 — no pgvector needed):
  1. The constraint as committed accepts EVERY value of the frozen taxonomy
     (fails today on `semantic_threshold_miss`; goes green exactly when
     @database-administrator's chk_cache_status migration lands).
  2. One full ledger row per taxonomy value inserts cleanly and reads back
     byte-identical (the writer's literals must survive the round trip).
  3. Row count matches rows written — the ledger never silently eats a row.
  4. An out-of-taxonomy value is still rejected (the widened constraint must
     stay a constraint, not become a free-text column).

Merging order is mandated by PM: DBA's migration lands BEFORE the writer
emits `semantic_threshold_miss`.  When merging this file, bump the CI
standard-suite executed floor in .github/workflows/ci.yml (+1 for each job
that runs the standard suite).
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pg_test_support import drop_database, make_database, unique_db_name  # noqa: E402

# Frozen taxonomy (cache-status-dashboard-spec.md, AC-PC-UI).  Order is
# irrelevant; the set is the contract.
FROZEN_TAXONOMY = ("miss", "exact_hit", "semantic_hit", "semantic_threshold_miss")

_DB_NAME = unique_db_name("ts_pcui_taxonomy")


@pytest.fixture(scope="module")
def dsn():
    # TOKEN_SAVER_PG_TEST_DSN: run against an externally prepared database
    # (e.g. simulating DBA's pending migration); the gate neither creates nor
    # drops it.
    override = os.environ.get("TOKEN_SAVER_PG_TEST_DSN")
    if override:
        yield override
        return
    dsn = make_database(_DB_NAME)
    yield dsn
    drop_database(_DB_NAME)


@pytest.fixture()
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as pg:
        yield pg


def _constraint_def(pg: psycopg.Connection) -> str:
    row = pg.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'chk_cache_status'"
    ).fetchone()
    assert row is not None, "chk_cache_status constraint missing from live schema"
    return row[0]


def _accepted_values(pg: psycopg.Connection) -> set[str]:
    # Postgres normalizes `IN ('a','b')` to `= ANY (ARRAY['a'::text, 'b'::text])`
    # in pg_get_constraintdef, so both spellings must parse.
    definition = _constraint_def(pg)
    m = re.search(r"IN \((.*)\)", definition, re.IGNORECASE) or re.search(
        r"= ANY \(ARRAY\[(.*)\]\)", definition, re.IGNORECASE
    )
    assert m, f"chk_cache_status is not an IN-list or ANY-ARRAY: {definition}"
    return {
        v.strip().strip("'\"").split("::")[0].strip("'\"")
        for v in m.group(1).split(",")
        if v.strip()
    }


def _seed_lookups(pg: psycopg.Connection) -> tuple[str, int]:
    tenant = str(uuid.uuid4())
    pg.execute("INSERT INTO tenants (id, name) VALUES (%s, 'pcui-taxonomy-gate')", (tenant,))
    provider = pg.execute(
        "INSERT INTO providers (name, base_url, adapter_class, auth_style)"
        " VALUES (%s, 'http://pcui-gate.invalid', 'OpenAICompatAdapter', 'bearer')"
        " RETURNING id",
        (f"pcui-gate-{tenant[:8]}",),
    ).fetchone()[0]
    return tenant, provider


def _insert_ledger_row(pg: psycopg.Connection, tenant: str, provider: int, cache_status: str) -> None:
    pg.execute(
        "INSERT INTO requests (tenant_id, provider_id, model, route, cache_status,"
        " input_tokens_before, input_tokens_after, output_tokens,"
        " est_cost_before, est_cost_after, cache_savings, status)"
        " VALUES (%s, %s, 'gate-model', 'compress', %s, 1000, 600, 50,"
        " 0.003, 0.0018, 0.0012, 200)",
        (tenant, provider, cache_status),
    )


def test_constraint_accepts_full_frozen_taxonomy(conn):
    """The RED edge of this gate: `semantic_threshold_miss` is rejected until
    DBA's chk_cache_status migration lands."""
    accepted = _accepted_values(conn)
    missing = [v for v in FROZEN_TAXONOMY if v not in accepted]
    assert not missing, (
        "chk_cache_status rejects frozen taxonomy values "
        f"{missing!r}; the writer emits these literals and the ledger write "
        f"fails silently. Live constraint: {_constraint_def(conn)}"
    )


def test_one_full_ledger_row_per_taxonomy_value(conn):
    """Each taxonomy value survives the insert/read round trip intact."""
    tenant, provider = _seed_lookups(conn)
    for value in FROZEN_TAXONOMY:
        _insert_ledger_row(conn, tenant, provider, value)
    rows = conn.execute(
        "SELECT cache_status, input_tokens_before, input_tokens_after,"
        " output_tokens, cache_savings, status FROM requests ORDER BY id"
    ).fetchall()
    assert len(rows) == len(FROZEN_TAXONOMY), (
        f"expected {len(FROZEN_TAXONOMY)} ledger rows, got {len(rows)}"
    )
    assert [r[0] for r in rows] == list(FROZEN_TAXONOMY)
    for r in rows:
        assert (r[1], r[2], r[3], Decimal(r[4]), r[5]) == (
            1000, 600, 50, Decimal("0.0012"), 200), r


def test_row_count_matches_rows_written(conn):
    """The AC-PC-UI acceptance invariant the PM pinned: ledger row count must
    equal rows sent — a check-constraint rejection here is the exact shape of
    the silent swallow at main.py's LEDGER_WRITE_FAILURES handler."""
    tenant, provider = _seed_lookups(conn)
    before = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    for value in FROZEN_TAXONOMY:
        _insert_ledger_row(conn, tenant, provider, value)
    after = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert after - before == len(FROZEN_TAXONOMY)


def test_out_of_taxonomy_value_still_rejected(conn):
    """Widening must not become a free-text column."""
    tenant, provider = _seed_lookups(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_ledger_row(conn, tenant, provider, "cache_hit_please")
