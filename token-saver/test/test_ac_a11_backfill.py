"""AC-A11/B-4: SQLite-era backfill release gate.

The tests use a populated 269-row SQLite fixture, a fresh Postgres database,
and the real migration entry point.  They verify route normalization, count
and NUMERIC(14,8) cost reconciliation, rollback, dirty-target refusal, and
idempotent rerun behavior through persisted state.
"""
from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import PG_BASE, drop_database, make_database, unique_db_name  # noqa: E402
from scripts import backfill_postgres as backfill  # noqa: E402


ROW_COUNT = 269


def _make_sqlite_fixture(path: Path) -> tuple[Decimal, Decimal]:
    before_sum = Decimal("0")
    after_sum = Decimal("0")
    with sqlite3.connect(path) as sq:
        sq.execute(
            """CREATE TABLE requests (
                id INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                model TEXT NOT NULL,
                route TEXT NOT NULL,
                input_tokens_before INTEGER NOT NULL,
                input_tokens_after INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                est_cost_before REAL NOT NULL,
                est_cost_after REAL NOT NULL,
                latency_ms REAL NOT NULL,
                compressed INTEGER NOT NULL,
                status INTEGER NOT NULL,
                l1_tokens_stripped INTEGER NOT NULL DEFAULT 0,
                l1_savings REAL NOT NULL DEFAULT 0
            )"""
        )
        for i in range(ROW_COUNT):
            before = 0.0004 + (i % 17) * 0.000001 + (0.00000004 if i == 0 else 0)
            after = before - 0.0000001
            route = "models" if i == ROW_COUNT - 1 else (
                "passthrough" if i >= 265 else "compress"
            )
            sq.execute(
                """INSERT INTO requests
                   (id, ts, model, route, input_tokens_before, input_tokens_after,
                    output_tokens, est_cost_before, est_cost_after, latency_ms,
                    compressed, status, l1_tokens_stripped, l1_savings)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    i + 1,
                    1_700_000_000 + i,
                    "openai/gpt-4o",
                    route,
                    100 + i,
                    80 + i,
                    20 + i,
                    before,
                    after,
                    10.0 + i,
                    int(route == "compress"),
                    200,
                    i % 3,
                    0.00000001 * (i % 3),
                ),
            )
            before_sum += Decimal(str(before))
            after_sum += Decimal(str(after))
    return before_sum, after_sum


@pytest.fixture()
def sqlite_fixture(tmp_path):
    path = tmp_path / "stats.db"
    expected = _make_sqlite_fixture(path)
    return path, expected


def _drop(name: str):
    drop_database(name)


def test_populated_backfill_reconciles_and_is_idempotent(sqlite_fixture, monkeypatch):
    source, (before_sum, after_sum) = sqlite_fixture
    name = unique_db_name("ts_ac_a11_complete")
    dsn = make_database(name)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    try:
        result = backfill.run_backfill(str(source))
        assert result["status"] == "ok", result
        assert result["source_rows"] == ROW_COUNT
        assert result["remapped_routes"] == 1
        assert result["match"] is True

        q = Decimal("0.00000001")
        with psycopg.connect(dsn) as pg:
            count = pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            route_counts = dict(
                pg.execute(
                    "SELECT route, COUNT(*) FROM requests GROUP BY route"
                ).fetchall()
            )
            sums = pg.execute(
                "SELECT SUM(est_cost_before), SUM(est_cost_after) FROM requests"
            ).fetchone()
            marker = pg.execute(
                "SELECT source_row_count FROM backfill_batches"
            ).fetchone()
            scales = pg.execute(
                """SELECT column_name, numeric_scale
                   FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'requests'
                     AND column_name IN ('est_cost_before', 'est_cost_after', 'l1_savings')"""
            ).fetchall()
        assert count == ROW_COUNT
        assert route_counts == {"compress": 265, "passthrough": 4}
        assert Decimal(sums[0]).quantize(q) == before_sum.quantize(q)
        assert Decimal(sums[1]).quantize(q) == after_sum.quantize(q)
        assert marker == (ROW_COUNT,)
        assert {row[0]: row[1] for row in scales} == {
            "est_cost_before": 8,
            "est_cost_after": 8,
            "l1_savings": 8,
        }

        again = backfill.run_backfill(str(source))
        assert again["status"] == "already_applied"
        with psycopg.connect(dsn) as pg:
            assert pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == ROW_COUNT
            assert pg.execute("SELECT COUNT(*) FROM backfill_batches").fetchone()[0] == 1
    finally:
        _drop(name)


def test_verification_failure_rolls_back_rows_and_marker(sqlite_fixture, monkeypatch):
    source, _expected = sqlite_fixture
    name = unique_db_name("ts_ac_a11_rollback")
    dsn = make_database(name)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    try:
        monkeypatch.setattr(
            backfill,
            "_verify",
            lambda _sqlite, _pg: {"match": False, "reason": "forced QA failure"},
        )
        result = backfill.run_backfill(str(source))
        assert result["status"] == "verification_failed"
        with psycopg.connect(dsn) as pg:
            assert pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
            assert pg.execute("SELECT COUNT(*) FROM backfill_batches").fetchone()[0] == 0
    finally:
        _drop(name)


def test_dirty_target_is_refused_without_mutation(sqlite_fixture, monkeypatch):
    source, _expected = sqlite_fixture
    name = unique_db_name("ts_ac_a11_dirty_target")
    dsn = make_database(name)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    try:
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute(
                """INSERT INTO requests
                   (tenant_id, provider_id, model, route,
                    input_tokens_before, input_tokens_after, output_tokens,
                    est_cost_before, est_cost_after, status)
                   SELECT '00000000-0000-0000-0000-000000000000', id,
                          'openai/dirty', 'compress', 1, 1, 0, 0, 0, 200
                   FROM providers WHERE name = 'openai'"""
            )
        result = backfill.run_backfill(str(source))
        assert result["status"] == "refused_dirty_target"
        assert result["existing_rows"] == 1
        with psycopg.connect(dsn) as pg:
            assert pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
            assert pg.execute("SELECT COUNT(*) FROM backfill_batches").fetchone()[0] == 0
    finally:
        _drop(name)
