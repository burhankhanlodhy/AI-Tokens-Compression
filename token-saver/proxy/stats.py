"""SQLite logging of token counts and estimated costs per request."""
from __future__ import annotations

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
    status INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
"""


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
) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO requests (ts, model, route, input_tokens_before, "
            "input_tokens_after, output_tokens, est_cost_before, est_cost_after, "
            "latency_ms, compressed, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
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
