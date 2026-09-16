"""PA-0: Migrate legacy SQLite `requests` ledger to Postgres (AC-A11).

Implements @database-administrator's backfill spec:
1. Seeds the default tenant + synthetic 'legacy' provider before inserting rows
   (legacy data predates multi-tenant/multi-provider).
2. Lossless REAL -> NUMERIC cast: values go through Decimal(str(v)) (text round
   trip), never Decimal(float) which would preserve the binary artifact.
3. Column mapping: ts (unix REAL) -> to_timestamp() TIMESTAMPTZ; compressed
   0/1 -> BOOLEAN; cache_status='miss', cache_savings=0 for legacy rows.
4. Pre-cutover verification: independent COUNT(*) + SUM(cost) on both sides.
5. Idempotent via a `backfill_batches` marker table (checksum of source).
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import psycopg

DEFAULT_SQLITE_PATH = str(Path(__file__).resolve().parent.parent / "data" / "stats.db")
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"
BATCH_SIZE = 500
logger = logging.getLogger(__name__)


def _dsn() -> str:
    return os.environ.get(
        "TOKEN_SAVER_PG_DSN",
        "postgresql://postgres:REDACTED@localhost:5433/token_saver",
    )


def _source_checksum(rows: list[sqlite3.Row]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def _seed_prerequisites(pg: psycopg.Connection) -> int:
    """Ensure default tenant + legacy provider exist; return provider_id."""
    pg.execute(
        """
        INSERT INTO tenants (id, name, plan)
        VALUES (%s::uuid, 'default', 'self_host')
        ON CONFLICT (id) DO NOTHING
        """,
        (DEFAULT_TENANT_ID,),
    )
    pg.execute(
        """
        INSERT INTO providers (name, base_url, adapter_class, auth_style)
        VALUES ('legacy', 'https://openrouter.ai/api/v1', 'OpenAICompatAdapter', 'bearer')
        ON CONFLICT (name) DO NOTHING
        """
    )
    row = pg.execute("SELECT id FROM providers WHERE name = 'legacy'").fetchone()
    assert row is not None
    return int(row[0])


def _create_marker_table(pg: psycopg.Connection) -> None:
    pg.execute(
        """
        CREATE TABLE IF NOT EXISTS backfill_batches (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_row_count BIGINT NOT NULL,
            checksum         TEXT NOT NULL UNIQUE,
            completed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _already_applied(pg: psycopg.Connection, checksum: str) -> bool:
    return pg.execute(
        "SELECT 1 FROM backfill_batches WHERE checksum = %s", (checksum,)
    ).fetchone() is not None


def _insert_batch(pg: psycopg.Connection, provider_id: int, batch: list[sqlite3.Row]) -> int:
    """Lossless cast: costs go to NUMERIC via their text representation."""
    remapped_routes = 0
    for r in batch:
        route = r["route"]
        if route not in {"compress", "passthrough"}:
            route = "passthrough"
            remapped_routes += 1
        pg.execute(
            """
            INSERT INTO requests (tenant_id, provider_id, ts, model, route,
                cache_status, input_tokens_before, input_tokens_after,
                output_tokens, est_cost_before, est_cost_after, cache_savings,
                l1_tokens_stripped, l1_savings, latency_ms, compressed, status)
            VALUES (%s::uuid, %s, to_timestamp(%s), %s, %s,
                'miss', %s, %s,
                %s, %s::numeric, %s::numeric, 0,
                %s, %s::numeric, %s::numeric, %s, %s)
            """,
            (
                DEFAULT_TENANT_ID,
                provider_id,
                r["ts"],                       # unix epoch REAL -> to_timestamp
                r["model"],
                route,
                r["input_tokens_before"],
                r["input_tokens_after"],
                r["output_tokens"],
                str(Decimal(str(r["est_cost_before"]))),   # text path, not float
                str(Decimal(str(r["est_cost_after"]))),
                int(r["l1_tokens_stripped"]) if "l1_tokens_stripped" in r.keys() else 0,
                str(Decimal(str(r["l1_savings"]))) if "l1_savings" in r.keys() else "0",
                str(r["latency_ms"]),
                bool(r["compressed"]),
                r["status"],
            ),
        )
    return remapped_routes


def _verify(sqlite_conn: sqlite3.Connection, pg: psycopg.Connection) -> dict:
    """Independent totals on both sides — the AC-A11 correctness gate.

    Cost sums are compared at the schema's NUMERIC(14,8) precision: Postgres
    rounds stored costs to 8 decimal places by design, so legacy REAL values
    carrying float artifacts beyond the 8th decimal (e.g. 0.1+0.2 accumulation)
    verify equal after quantization. Within that precision the transfer is
    exact — the text-path cast guarantees no drift is *introduced* below it.
    """
    q = Decimal("0.00000001")  # NUMERIC(14,8) scale
    sqlite_columns = {
        row[1] for row in sqlite_conn.execute("PRAGMA table_info(requests)")
    }
    # Older SQLite ledgers predate the L1 columns; those rows have the same
    # semantic defaults as the Postgres schema. Newer ledgers are reconciled
    # losslessly so a backfill cannot silently discard attribution.
    l1_tokens_expr = "l1_tokens_stripped" if "l1_tokens_stripped" in sqlite_columns else "0"
    l1_savings_expr = "l1_savings" if "l1_savings" in sqlite_columns else "0"
    sq = sqlite_conn.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(est_cost_before), 0),"
        " COALESCE(SUM(est_cost_after), 0),"
        " COALESCE(SUM(input_tokens_before), 0),"
        " COALESCE(SUM(output_tokens), 0),"
        f" COALESCE(SUM({l1_tokens_expr}), 0),"
        f" COALESCE(SUM({l1_savings_expr}), 0) FROM requests"
    ).fetchone()
    pgrow = pg.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(est_cost_before), 0),"
        " COALESCE(SUM(est_cost_after), 0),"
        " COALESCE(SUM(input_tokens_before), 0),"
        " COALESCE(SUM(output_tokens), 0),"
        " COALESCE(SUM(l1_tokens_stripped), 0),"
        " COALESCE(SUM(l1_savings), 0) FROM requests"
    ).fetchone()

    def _norm_pg(v):
        return Decimal(v) if isinstance(v, str) else v

    report = {
        "count": {"sqlite": sq[0], "postgres": pgrow[0]},
        "sum_cost_before": {"sqlite": sq[1], "postgres": _norm_pg(pgrow[1])},
        "sum_cost_after": {"sqlite": sq[2], "postgres": _norm_pg(pgrow[2])},
        "sum_input_before": {"sqlite": sq[3], "postgres": pgrow[3]},
        "sum_output": {"sqlite": sq[4], "postgres": pgrow[4]},
        "sum_l1_tokens_stripped": {"sqlite": sq[5], "postgres": pgrow[5]},
        "sum_l1_savings": {"sqlite": sq[6], "postgres": _norm_pg(pgrow[6])},
    }
    exact = (
        report["count"]["sqlite"] == report["count"]["postgres"]
        and Decimal(str(sq[1])).quantize(q) == _norm_pg(pgrow[1]).quantize(q)
        and Decimal(str(sq[2])).quantize(q) == _norm_pg(pgrow[2]).quantize(q)
        and sq[3] == pgrow[3]
        and sq[4] == pgrow[4]
        and sq[5] == pgrow[5]
        and Decimal(str(sq[6])).quantize(q) == _norm_pg(pgrow[6]).quantize(q)
    )
    report["match"] = exact
    return report


def run_backfill(sqlite_path: str = DEFAULT_SQLITE_PATH) -> dict:
    if not Path(sqlite_path).exists():
        return {"status": "skipped", "reason": f"no legacy sqlite db at {sqlite_path}"}

    sq = sqlite3.connect(sqlite_path)
    sq.row_factory = sqlite3.Row
    rows = sq.execute("SELECT * FROM requests ORDER BY id").fetchall()
    checksum = _source_checksum(rows)

    with psycopg.connect(_dsn()) as pg:
        _create_marker_table(pg)
        if _already_applied(pg, checksum):
            return {"status": "already_applied", "checksum": checksum,
                    "source_rows": len(rows)}
        # Guard: refuse a dirty target. If requests already holds rows, a
        # backfill would silently double-count against verification.
        pre = pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        if pre > 0:
            return {
                "status": "refused_dirty_target",
                "reason": (
                    f"target 'requests' table already holds {pre} row(s); "
                    "backfill only runs against an empty ledger. Point "
                    "TOKEN_SAVER_PG_DSN at a fresh database (schema applied) "
                    "or clean the table first."
                ),
                "existing_rows": int(pre),
            }
        provider_id = _seed_prerequisites(pg)
        remapped_routes = 0
        for i in range(0, len(rows), BATCH_SIZE):
            remapped_routes += _insert_batch(pg, provider_id, rows[i:i + BATCH_SIZE])
        logger.info(
            "backfill normalized %d legacy route value(s) to passthrough",
            remapped_routes,
        )
        pg.execute(
            "INSERT INTO backfill_batches (source_row_count, checksum) VALUES (%s, %s)",
            (len(rows), checksum),
        )
        report = _verify(sq, pg)
        report["remapped_routes"] = remapped_routes
        if not report["match"]:
            pg.rollback()
            return {"status": "verification_failed", "report": report}
        pg.commit()
    report.update({"status": "ok", "checksum": checksum, "source_rows": len(rows)})
    return report


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SQLITE_PATH
    result = run_backfill(path)
    print(result)
    sys.exit(0 if result.get("status") in ("ok", "already_applied", "skipped") else 1)
