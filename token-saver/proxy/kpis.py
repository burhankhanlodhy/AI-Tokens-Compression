"""PA-2: /api/kpis — time-bucketed KPI aggregation over the Postgres ledger.

Single source of truth for both the dashboard and the Prometheus path
(product-spec-v2.md PA-2): all numbers are computed by SQL SUM()/percentile
over `requests` — no rollup table (AC-A12), no client-side aggregation.

Contract: GET /api/kpis?bucket=minute|hour|day&from=<iso>&to=<iso>
          -> {overview, series, by_model, by_provider, latency}
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from fastapi import Query
from fastapi.responses import JSONResponse

BUCKETS = {"minute": "minute", "hour": "hour", "day": "day"}


def _dsn() -> str:
    return os.environ.get(
        "TOKEN_SAVER_PG_DSN",
        "postgresql://postgres:REDACTED@localhost:5433/token_saver",
    )


def _fetch_kpis(bucket: str, from_iso: str | None, to_iso: str | None) -> dict[str, Any]:
    """All KPI math happens in Postgres over the ledger (SUM-over-ledger only)."""
    params: list[Any] = []
    where = ""
    if from_iso:
        params.append(from_iso)
        where += f" AND ts >= to_timestamp(%s, 'YYYY-MM-DD\"T\"HH24:MI:SS')"
    if to_iso:
        params.append(to_iso)
        where += f" AND ts <= to_timestamp(%s, 'YYYY-MM-DD\"T\"HH24:MI:SS')"

    bucket_expr = f"date_trunc('{BUCKETS[bucket]}', ts)"

    with psycopg.connect(_dsn()) as pg, pg.cursor() as cur:
        # ---- overview ----
        cur.execute(
            f"""
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens_before), 0),
                   COALESCE(SUM(input_tokens_after), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(est_cost_before), 0),
                   COALESCE(SUM(est_cost_after), 0),
                   COALESCE(SUM(cache_savings), 0),
                   COALESCE(SUM(CASE WHEN cache_status = 'exact_hit' THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END), 0),
                   COALESCE(AVG(latency_ms), 0)
            FROM requests WHERE TRUE {where}
            """,
            params,
        )
        (n, tin, tout_after, out, cb, ca, cache_sav, cache_hits, errors,
         avg_lat) = cur.fetchone()
        tin = int(tin)
        tout_after = int(tout_after)
        saved = tin - tout_after
        overview = {
            "requests": int(n),
            "input_tokens_before": tin,
            "input_tokens_after": tout_after,
            "input_tokens_saved": saved,
            "savings_pct": round(100 * saved / tin, 2) if tin else 0.0,
            "output_tokens": int(out),
            "cost_before": float(cb),
            "cost_after": float(ca),
            "cost_saved": float(cb) - float(ca),
            "cache_savings": float(cache_sav),   # reported separately (AC-A6)
            "cache_hits": int(cache_hits),
            "cache_hit_pct": round(100 * cache_hits / n, 2) if n else 0.0,
            "errors": int(errors),
            "error_rate_pct": round(100 * errors / n, 2) if n else 0.0,
            "avg_latency_ms": float(avg_lat),
        }

        # ---- time-bucketed series ----
        cur.execute(
            f"""
            SELECT {bucket_expr} AS b, COUNT(*),
                   SUM(input_tokens_before) - SUM(input_tokens_after),
                   SUM(est_cost_before) - SUM(est_cost_after),
                   SUM(cache_savings),
                   SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END)
            FROM requests WHERE TRUE {where}
            GROUP BY b ORDER BY b
            """,
            params,
        )
        series = [
            {
                "bucket": r[0].isoformat(),
                "requests": int(r[1]),
                "tokens_saved": int(r[2] or 0),
                "cost_saved": float(r[3] or 0),
                "cache_savings": float(r[4] or 0),
                "errors": int(r[5] or 0),
            }
            for r in cur.fetchall()
        ]

        # ---- by_model / by_provider ----
        cur.execute(
            f"""
            SELECT model, COUNT(*),
                   SUM(input_tokens_before) - SUM(input_tokens_after),
                   SUM(est_cost_before) - SUM(est_cost_after)
            FROM requests WHERE TRUE {where}
            GROUP BY model ORDER BY COUNT(*) DESC
            """,
            params,
        )
        by_model = [
            {"model": r[0], "requests": int(r[1]),
             "tokens_saved": int(r[2] or 0), "cost_saved": float(r[3] or 0)}
            for r in cur.fetchall()
        ]

        cur.execute(
            f"""
            SELECT p.name, COUNT(*),
                   SUM(r.input_tokens_before) - SUM(r.input_tokens_after),
                   SUM(r.est_cost_before) - SUM(r.est_cost_after),
                   SUM(CASE WHEN r.cache_status = 'exact_hit' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.status >= 400 THEN 1 ELSE 0 END)
            FROM requests r JOIN providers p ON p.id = r.provider_id
            WHERE TRUE {where}
            GROUP BY p.name ORDER BY COUNT(*) DESC
            """,
            params,
        )
        by_provider = [
            {"provider": r[0], "requests": int(r[1]),
             "tokens_saved": int(r[2] or 0), "cost_saved": float(r[3] or 0),
             "cache_hits": int(r[4] or 0), "errors": int(r[5] or 0)}
            for r in cur.fetchall()
        ]

        # ---- latency percentiles (continuous percentiles, Postgres-native) ----
        cur.execute(
            f"""
            SELECT
                COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms), 0),
                COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0),
                COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms), 0)
            FROM requests WHERE TRUE {where}
            """,
            params,
        )
        p50, p95, p99 = cur.fetchone()
        latency = {"p50": float(p50), "p95": float(p95), "p99": float(p99)}

    return {
        "bucket": bucket,
        "overview": overview,
        "series": series,
        "by_model": by_model,
        "by_provider": by_provider,
        "latency": latency,
    }


async def kpis_endpoint(
    bucket: str = Query("day"),
    from_ts: str | None = Query(None, alias="from"),
    to_ts: str | None = Query(None, alias="to"),
):
    if bucket not in BUCKETS:
        return JSONResponse({"error": f"bucket must be one of {sorted(BUCKETS)}"},
                            status_code=400)
    try:
        data = _fetch_kpis(bucket, from_ts, to_ts)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        return JSONResponse({"error": "ledger unavailable", "detail": str(exc)},
                            status_code=503)
    return JSONResponse(data)
