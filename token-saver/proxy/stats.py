"""SQLite logging of token counts and estimated costs per request."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import get_settings

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    route TEXT NOT NULL,                -- 'compress' | 'passthrough'
    input_tokens_before INTEGER NOT NULL,
    input_tokens_after INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_before REAL NOT NULL DEFAULT 0,
    est_cost_after REAL NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    compressed INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    schema_cache_hit INTEGER NOT NULL DEFAULT 0,
    schema_bytes_saved INTEGER NOT NULL DEFAULT 0,
    dose_tier TEXT,                     -- AC-P6f: resolved tier ('none'|'bounded'|'full'); NULL = discriminator never ran
    grounded_risk TEXT,                 -- AC-P6f: discriminator risk ('none'|'bounded'|'fidelity_critical'); NULL = never ran
    envelope_shape INTEGER,             -- AC-P6f: AC-P6j scanner hit on the raw request (1/0); NULL = no content logged
    measurement_tag TEXT                -- AC-P6f: stamp from a measurement deployment (TOKEN_SAVER_MEASUREMENT_TAG); tripwire excludes tagged rows
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""

# B2-d: L1 ledger columns on the SQLite path too. B2 originally added them
# to the Postgres schema only, so the local/single-user INSERT silently
# dropped l1_tokens_stripped / l1_savings (caught by QA's live persistence
# probe; the unit test asserted on input_tokens_saved and never noticed).
# _MIGRATIONS run idempotently in init_db so existing local DBs upgrade in
# place (same approach as docker token-saver-postgres for the PG side).
_MIGRATIONS = (
    "ALTER TABLE requests ADD COLUMN l1_tokens_stripped INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE requests ADD COLUMN l1_savings REAL NOT NULL DEFAULT 0",
    # v1.2.1 schema optimization attribution. These are request-level facts:
    # cache hit is 1 only when an already-minified schema was reused; bytes
    # saved is the compact raw-schema delta, never token-estimated savings.
    "ALTER TABLE requests ADD COLUMN schema_cache_hit INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE requests ADD COLUMN schema_bytes_saved INTEGER NOT NULL DEFAULT 0",
    # AC-P6f: live tripwire columns. NULLable — NULL means "the discriminator
    # never ran on this request" (conciseness off / passthrough), which is
    # itself a signal the missed-grounding rule consumes.
    "ALTER TABLE requests ADD COLUMN dose_tier TEXT",
    "ALTER TABLE requests ADD COLUMN grounded_risk TEXT",
    "ALTER TABLE requests ADD COLUMN envelope_shape INTEGER",
    # AC-P6f live-population hygiene: rows written by a measurement (benchmark
    # harness) deployment are stamped and excluded from the tripwire window —
    # the drift rule must never compare the band against its own source rows.
    "ALTER TABLE requests ADD COLUMN measurement_tag TEXT",
)


def _connect() -> sqlite3.Connection:
    path = get_settings().database_path
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # per-connection pragma; pairs with WAL (set in init_db) to reduce
    # reader/writer lock contention under concurrent traffic.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    from pathlib import Path

    Path(get_settings().database_path).parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        # WAL: readers don't block the writer under concurrent /v1/* traffic
        # + /stats polling (avoids 'database is locked').
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
        for stmt in _MIGRATIONS:
            col = stmt.split("ADD COLUMN ")[1].split()[0]
            if col not in existing:
                conn.execute(stmt)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_request(
    *,
    model: str,
    route: str,
    input_tokens_before: int,
    input_tokens_after: int,
    output_tokens: int,
    est_cost_before: float,
    est_cost_after: float,
    latency_ms: float,
    compressed: bool,
    status: int,
    cache_status: str = "miss",
    cache_savings: float = 0.0,
    schema_cache_hit: bool = False,
    schema_bytes_saved: int = 0,
    l1_tokens_stripped: int = 0,
    l1_savings: float = 0.0,
    provider: str | None = None,
    dose_tier: str | None = None,
    grounded_risk: str | None = None,
    envelope_shape: int | None = None,
    measurement_tag: str | None = None,
    embedding_version: str | None = None,
    quality_version: str | None = None,
) -> None:
    """Append to the request ledger.

    Ledger selection is explicit, not reachability-probed:
    - TOKEN_SAVER_PG_DSN set -> Postgres (Phase A production path, includes
      cache columns)
    - otherwise -> local SQLite (single-user mode and the unit-test fixture)

    This keeps the two ledgers deterministic for tests and deployment.
    B3 attribution: l1_tokens_stripped / l1_savings are separate columns,
    never summed with cache_savings on a single request (taxonomy §1).
    schema_cache_hit / schema_bytes_saved separately attribute tool-schema
    minification without claiming a provider-side token or cost reduction.
    AC-P6f: dose_tier / grounded_risk / envelope_shape feed the live
    tripwire loop; NULLs mean the discriminator never ran on the request.
    measurement_tag: explicit value wins; otherwise the deployment-level
    TOKEN_SAVER_MEASUREMENT_TAG stamps every row (benchmark harness
    deployments) so the tripwire can exclude instrument traffic.
    """
    import os

    if measurement_tag is None:
        measurement_tag = get_settings().measurement_tag

    if os.environ.get("TOKEN_SAVER_PG_DSN"):
        _log_postgres(
            model=model, route=route, input_tokens_before=input_tokens_before,
            input_tokens_after=input_tokens_after, output_tokens=output_tokens,
            est_cost_before=est_cost_before, est_cost_after=est_cost_after,
            latency_ms=latency_ms, compressed=compressed, status=status,
            cache_status=cache_status, cache_savings=cache_savings,
            schema_cache_hit=schema_cache_hit,
            schema_bytes_saved=schema_bytes_saved,
            l1_tokens_stripped=l1_tokens_stripped, l1_savings=l1_savings,
            provider=provider,
            dose_tier=dose_tier, grounded_risk=grounded_risk,
            envelope_shape=envelope_shape,
            measurement_tag=measurement_tag,
            embedding_version=embedding_version,
            quality_version=quality_version,
        )
        return
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO requests (ts, model, route, input_tokens_before, "
            "input_tokens_after, output_tokens, est_cost_before, est_cost_after, "
            "latency_ms, compressed, status, l1_tokens_stripped, l1_savings, "
            "schema_cache_hit, schema_bytes_saved, dose_tier, grounded_risk, "
            "envelope_shape, measurement_tag) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                model,
                route,
                input_tokens_before,
                input_tokens_after,
                output_tokens,
                est_cost_before,
                est_cost_after,
                latency_ms,
                int(compressed),
                status,
                l1_tokens_stripped,
                l1_savings,
                int(schema_cache_hit),
                schema_bytes_saved,
                dose_tier,
                grounded_risk,
                envelope_shape,
                measurement_tag,
            ),
        )


def _log_postgres(
    *, model, route, input_tokens_before, input_tokens_after, output_tokens,
    est_cost_before, est_cost_after, latency_ms, compressed, status,
    cache_status, cache_savings, schema_cache_hit=False, schema_bytes_saved=0,
    l1_tokens_stripped=0, l1_savings=0.0,
    provider=None,
    dose_tier=None, grounded_risk=None, envelope_shape=None,
    measurement_tag=None, embedding_version=None, quality_version=None,
) -> None:
    import psycopg

    dsn = os.environ["TOKEN_SAVER_PG_DSN"]
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        # B-24 (AC-A12): the live path passes the adapter-resolved provider
        # name (the registry row's own name), so config-added providers are
        # attributed to themselves instead of falling back to 'legacy'.
        # Direct log_request callers (tests, seeds) without a provider keep
        # the historical prefix-table derivation.
        if provider is None:
            from .providers.registry import PREFIX_ROUTES, DEFAULT_REGISTRY

            lowered = model.lower()
            provider = next(
                (p for pre, p in PREFIX_ROUTES.items() if lowered.startswith(pre)
                 and any(r.name == p for r in DEFAULT_REGISTRY)),
                None,
            )
        version_columns = ", embedding_version, quality_version" if embedding_version is not None or quality_version is not None else ""
        version_values = ", %s, %s" if version_columns else ""
        values = (
            provider or "legacy", model, route,
            input_tokens_before, input_tokens_after, output_tokens,
            str(est_cost_before), str(est_cost_after),
            cache_status, str(cache_savings),
            l1_tokens_stripped, str(l1_savings), schema_cache_hit, schema_bytes_saved,
            latency_ms, compressed, status,
            dose_tier, grounded_risk, envelope_shape, measurement_tag,
            *((embedding_version, quality_version) if version_columns else ()),
        )
        conn.execute(
            f"""
            INSERT INTO requests (tenant_id, provider_id, model, route,
                input_tokens_before, input_tokens_after, output_tokens,
                est_cost_before, est_cost_after, cache_status, cache_savings,
                l1_tokens_stripped, l1_savings, schema_cache_hit, schema_bytes_saved,
                latency_ms, compressed, status,
                dose_tier, grounded_risk, envelope_shape, measurement_tag
                {version_columns})
            SELECT '00000000-0000-0000-0000-000000000000',
                   COALESCE((SELECT id FROM providers WHERE name = %s),
                            (SELECT id FROM providers WHERE name = 'legacy')),
                   %s, %s, %s, %s, %s, %s::numeric, %s::numeric, %s,
                   %s::numeric, %s::numeric, %s, %s::numeric, %s, %s, %s, %s,
                   %s, %s, %s, %s{version_values}
            """,
            values,
        )


def aggregate_stats() -> dict[str, Any]:
    """Totals plus per-day and per-route breakdowns for the /stats endpoint."""
    with get_conn() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(input_tokens_before),0) AS input_before,"
            " COALESCE(SUM(input_tokens_after),0) AS input_after,"
            " COALESCE(SUM(output_tokens),0) AS output_tokens,"
            " COALESCE(SUM(est_cost_before),0) AS cost_before,"
            " COALESCE(SUM(est_cost_after),0) AS cost_after,"
            " COALESCE(AVG(latency_ms),0) AS avg_latency_ms"
            " FROM requests"
        ).fetchone()

        by_route = [
            dict(r)
            for r in conn.execute(
                "SELECT route, COUNT(*) AS requests,"
                " SUM(input_tokens_before) AS input_before,"
                " SUM(input_tokens_after) AS input_after,"
                " SUM(output_tokens) AS output_tokens,"
                " SUM(est_cost_before) AS cost_before,"
                " SUM(est_cost_after) AS cost_after"
                " FROM requests GROUP BY route"
            )
        ]

        by_day = [
            dict(r)
            for r in conn.execute(
                "SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS requests,"
                " SUM(input_tokens_before) AS input_before,"
                " SUM(input_tokens_after) AS input_after,"
                " SUM(output_tokens) AS output_tokens,"
                " SUM(est_cost_before) AS cost_before,"
                " SUM(est_cost_after) AS cost_after"
                " FROM requests GROUP BY day ORDER BY day"
            )
        ]

        by_model = [
            dict(r)
            for r in conn.execute(
                "SELECT model, COUNT(*) AS requests,"
                " SUM(input_tokens_before) AS input_before,"
                " SUM(input_tokens_after) AS input_after,"
                " SUM(output_tokens) AS output_tokens,"
                " SUM(est_cost_before) AS cost_before,"
                " SUM(est_cost_after) AS cost_after"
                " FROM requests GROUP BY model ORDER BY requests DESC"
            )
        ]

    t = dict(totals)
    input_saved = t["input_before"] - t["input_after"]
    t["input_tokens_saved"] = input_saved
    t["input_savings_pct"] = (
        round(100 * input_saved / t["input_before"], 2) if t["input_before"] else 0.0
    )
    t["cost_saved"] = round(t["cost_before"] - t["cost_after"], 6)
    t["cost_before"] = round(t["cost_before"], 6)
    t["cost_after"] = round(t["cost_after"], 6)
    t["avg_latency_ms"] = round(t["avg_latency_ms"], 1)
    return {"totals": t, "by_route": by_route, "by_model": by_model, "by_day": by_day}
