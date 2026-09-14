"""
Prometheus-compatible metrics + health endpoints.

This module is a no-op if prometheus_format is disabled in config.
It exposes:
  GET /metrics — Prometheus expose endpoint (text format)
  GET /health — simple health check

These are intended for use with Prometheus exporters or Grafana dashboards.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

import httpx
from fastapi import FastAPI

from . import stats
from .config import get_settings

logger = logging.getLogger("token-saver.prometheus")


app = FastAPI(title="token-saver prometheus metrics")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Simple health check endpoint."""
    s = get_settings()
    upstream_base = s.upstream_base_url or "unknown"

    # Try to reach upstream; if it's unreachable, report soft healthy.
    try:
        client = httpx.AsyncClient(base_url=upstream_base, timeout=3.0)
        resp = await client.get("/v1/models", timeout=3)
        await client.aclose()
    except httpx.ConnectError:
        # No upstream configured yet; still say we are OK.
        return {
            "status": "soft",
            "reason": f"upstream {upstream_base} not reachable",
        }
    except Exception:
        return {
            "status": "soft",
            "reason": "upstream request failed",
        }

    return {"status": "healthy", "upstream": upstream_base}


def by_route(routes: list[dict[str, Any]]) -> list[tuple]:
    """Build per-route counter values."""
    counters: dict[str, int] = {}
    for r in routes:
        name = f'route={r["route"]}'
        if name not in counters:
            counters[name] = r["requests"]
    lines: list[str] = []
    for name, value in counters.items():
        lines.append(f'# HELP token_saver_request_count{{route="{name}"}} '
                     'request count by route')
        lines.append(f'# TYPE token_saver_request_count{{route="{name}"}} counter')
        lines.append(f'token_saver_request_count{{route="{name}"}} {value}')
    return lines


def by_day(days: list[dict[str, Any]]) -> list[tuple]:
    """Build per-day counter values."""
    counters: dict[str, int] = {}
    for d in days:
        name = f'day={d["day"]}'
        if name not in counters:
            counters[name] = d["requests"]
    lines: list[str] = []
    for name, value in counters.items():
        lines.append(f'# HELP token_saver_requests{{day="{name}"}} '
                     'requests by day')
        lines.append(f'# TYPE token_saver_requests{{day="{name}"}} counter')
        lines.append(f'token_saver_requests{{day="{name}"}} {value}')
    return lines


@app.get("/metrics")
async def prometheus_metrics(format: str = "text") -> str:
    """
    Prometheus metrics text format.

    If `format` is not "text", fall back to a simple JSON summary.
    """
    if format != "text":
        # Fallback JSON summary.
        data = stats.aggregate_stats()
        t = data["totals"]
        return (
            f'{{"requests":{t["requests"]},'
            f'"cost_saved":"$%.4f",'
            f'"tokens_saved":{t["input_tokens_saved"]}}}'
            % t["cost_saved"],
        )

    # Prometheus text format.
    lines: list[str] = []
    lines.append('# HELP token_saver_requests_total total requests logged')
    lines.append('# TYPE token_saver_requests_total counter')
    lines.append(f'token_saver_requests_total {t["requests"]}')

    lines.append('# HELP token_saver_tokens_saved tokens saved via compression')
    lines.append('# TYPE token_saver_tokens_saved counter')
    lines.append(f'token_saver_tokens_saved {t["input_tokens_saved"]}')

    lines.append('# HELP token_saver_cost_saved estimated dollar cost saved')
    lines.append('# TYPE token_saver_cost_saved gauge')
    lines.append(f'token_saver_cost_saved {t["cost_saved"]:.4f}')

    lines.append('# HELP token_saver_latency_ms average latency ms')
    lines.append('# TYPE token_saver_latency_ms gauge')
    lines.append(f'token_saver_latency_ms {t["avg_latency_ms"]:.1f}')

    # Per-route breakdown.
    r_lines = by_route(data["by_route"])
    lines.extend(r_lines)

    # Per-day breakdown.
    d_lines = by_day(data["by_day"] if data.get("by_day") else [])
    lines.extend(d_lines)

    return '\n'.join(lines)
