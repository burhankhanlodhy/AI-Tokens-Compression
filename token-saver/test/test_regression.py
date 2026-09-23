"""Regression tests for sprint fixes T1-T10 and gaps T11-T15.

Covers: embeddings passthrough (T3), route uniqueness (T4), metrics format
and content-type (T5), startup cleanliness / no rate-limiter code path (T2),
stats HTML dashboard states (T10, T14, T15), /v1/models logging (T12).
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_optional_support import require_pg_dsn  # noqa: E402

os.environ.setdefault("DATABASE_PATH", tempfile.mktemp(suffix=".db"))

from fastapi.testclient import TestClient  # noqa: E402

from proxy import stats  # noqa: E402
from proxy.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    # fresh temp DB per test so counts/empty-states are deterministic
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
    stats.get_settings.cache_clear()
    stats.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def postgres_stats():
    """Require PG availability, while regression assertions use SQLite."""
    return require_pg_dsn()


# ---------------------------------------------------------------- T2: clean startup

def test_startup_has_no_rate_limiter_code_path():
    """T2: rate_limit module is gone; app lifespan starts without NameError."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("proxy.rate_limit")
    assert not hasattr(app.state, "rate_limiter")


# --------------------------------------------------------- release version

def test_v1_release_version_is_exposed_by_health(client):
    """The proxy identifies its source-controlled version at runtime."""
    from proxy.version import __version__

    assert app.version == __version__
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- T3: embeddings

def test_embeddings_forwards_upstream_error_body(client):
    """T3: a valid embeddings POST reaches upstream; upstream error body is
    relayed cleanly (no NameError / 500 from the proxy itself)."""
    r = client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
    # No real key configured -> upstream responds with its own 401 error JSON.
    assert r.status_code in (200, 401)
    if r.status_code == 401:
        assert "error" in r.json()


# ---------------------------------------------------------------- T4: route uniqueness

def test_routes_are_unique():
    """T4: every method/path handler is registered exactly once.

    GET and POST legitimately share /api/keys; FastAPI's duplicate-decorator
    hazard is two handlers claiming the same method/path pair.
    """
    pairs = []
    for route in app.routes:
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", set()) or set():
            if path and method not in {"HEAD", "OPTIONS"}:
                pairs.append((method, path))
    dupes = {pair for pair in pairs if pairs.count(pair) > 1}
    assert dupes == set(), f"duplicate method/path routes: {dupes}"


# ---------------------------------------------------------------- T5: metrics

def test_metrics_fails_without_ledger(client):
    """T5/K-4a: /metrics is bound to the Postgres KPI path (spec §41 —
    /api/kpis is the single contract for the dashboard AND the Prometheus
    path). With no DSN configured (this SQLite-mode client) the scrape FAILS
    with 503 instead of reporting zeros that are indistinguishable from
    silently dropped ledger writes. The PG-mode Prometheus text/JSON shape,
    ledger-count reconciliation, and label mapping are pinned by
    test_k4a_metrics_kpi_bound.py."""
    assert client.get("/metrics").status_code == 503
    assert client.get("/metrics?format=json").status_code == 503


# ---------------------------------------------------------------- T12: /v1/models logging

def test_models_request_is_logged(postgres_stats, client):
    """T12: proxied /v1/models requests are logged with zero token/cost."""
    before = stats.aggregate_stats()["totals"]["requests"]
    client.get("/v1/models")
    after = stats.aggregate_stats()["totals"]["requests"]
    assert after == before + 1
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        row = conn.execute(
            "SELECT route, input_tokens_before, output_tokens, est_cost_before"
            " FROM requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    # B-1: /v1/models logs under the 'passthrough' taxonomy value (the
    # requests.route CHECK only allows 'compress'|'passthrough').
    assert row[0] == "passthrough"
    assert row[1:] == (0, 0, 0)


# ---------------------------------------------------------------- T10/T14/T15: dashboard

def _log_one():
    stats.log_request(model="m", route="compress", input_tokens_before=1000,
                      input_tokens_after=600, output_tokens=50,
                      est_cost_before=0.0001, est_cost_after=0.00006,
                      latency_ms=150.0, compressed=True, status=200)


def test_stats_html_empty_state(client):
    r = client.get("/stats?format=html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "No requests yet" in r.text


def test_stats_html_first_run_flip(postgres_stats, client):
    """Empty state -> first request -> non-zero savings render (first-run AC)."""
    assert client.get("/stats?format=html").text.count("Tokens saved (") == 0
    _log_one()
    r = client.get("/stats?format=html")
    assert r.text.count("Tokens saved (40.0%)") == 1  # rendered band, not the JS template
    assert "Est. cost saved" in r.text


def test_stats_html_has_tabs_and_chips(postgres_stats, client):
    _log_one()
    r = client.get("/stats?format=html")
    assert 'data-tab="by_day"' in r.text
    assert 'data-tab="by_route"' in r.text
    assert 'data-tab="by_model"' in r.text
    # T14: route rows render clickable filter chips
    assert 'class="chip" data-chip="compress"' in r.text
    assert "setChipFilter" in r.text


def test_stats_html_has_skeleton_loader(client):
    """T15: loading state renders skeleton rows, not spinner-in-a-void."""
    r = client.get("/stats?format=html")
    assert "showSkeleton" in r.text
    assert "skel-row" in r.text
    assert "shimmer" in r.text


def test_stats_html_error_state(client):
    """Error state: inline message + retry button, no blank page."""
    r = client.get("/stats?format=html")
    assert "showError" in r.text
    assert "Couldn" in r.text and "Retry" in r.text


def test_stats_by_model_breakdown(postgres_stats, client):
    _log_one()
    data = client.get("/stats").json()
    assert data["by_model"][0]["model"] == "m"
    assert data["by_model"][0]["requests"] == 1


# ---------------------------------------------------------------- suite still green

def test_original_suite_still_passes(postgres_stats):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(root / "test_proxy.py"), "-q"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
