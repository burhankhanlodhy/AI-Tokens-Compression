"""V2.2 KPI additions (t_d86fe22b, PM §5.3 / D-2): by_route series and
provider-native cache usage on by_provider rows.

Extends the hand-computed fixture discipline of test_kpis.py: every
assertion reconciles the API response against values computed
independently from the fixture rows, never against the SQL output.

Pinned contracts:

- by_route is a WINDOW-GLOBAL array (F2 window-global-percentile
  precedent): identical numbers for every bucket granularity, reconciled
  to the fixture, ordered by route.
- provider-native cache usage appears only on providers with measured
  evidence (NULL rows drop out entirely — the field is absent, not 0),
  and is NEVER merged into cache_savings or l1_savings (no double
  count).
- The route GROUP BY is additive: /api/kpis keeps every pre-existing
  field (overview/series/by_model/by_provider/latency reconcile exactly
  as before — regression guard against the D-2 wire change).

Requires TOKEN_SAVER_PG_BASE (skips cleanly without).
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import psycopg
except ImportError:  # pragma: no cover
    pytest.skip("psycopg not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE, unique_db_name, require_pg_base  # noqa: E402
import psycopg.conninfo  # noqa: E402

_DB_NAME = unique_db_name("ts_v22_kpi_additions")

SCHEMA = (Path(__file__).resolve().parent.parent.parent
          / "postgres-schema-v2.sql").read_text()

# ts, provider, model, route, in_b, in_a, out, cb, ca, cache, cache_sav, lat,
# comp, status, l1_tok, l1_sav, pc_read, pc_write
ROWS = [
    ("2026-09-20 10:00:00+00", "openrouter", "z-ai/glm-5.3-flash", "compress",
     1000, 600, 50, 0.001, 0.0006, "miss", 0, 100.0, True, 200, 120, 0.0001,
     512, 1024),
    ("2026-09-20 10:30:00+00", "openrouter", "z-ai/glm-5.3-flash", "compress",
     2000, 1000, 80, 0.002, 0.001, "exact_hit", 0.0004, 120.0, True, 200, 200,
     0.0003, None, None),
    ("2026-09-20 11:15:00+00", "openrouter", "openai/gpt-4o", "passthrough",
     500, 500, 200, 0.0025, 0.0025, "miss", 0, 300.0, False, 200, 0, 0.0,
     300, None),
    ("2026-09-20 12:00:00+00", "anthropic", "claude-sonnet-5", "compress",
     800, 400, 60, 0.0024, 0.0012, "miss", 0, 200.0, True, 200, 80, 0.0004,
     None, None),
]

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def v22_kpi_env():
    base = require_pg_base()
    try:
        with psycopg.connect(base, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")
            pg.execute(f"CREATE DATABASE {_DB_NAME}")
    except psycopg.OperationalError:
        pytest.skip("TOKEN_SAVER_PG_BASE is unavailable", allow_module_level=False)
    info = psycopg.conninfo.conninfo_to_dict(PG_BASE)
    info["dbname"] = _DB_NAME
    dsn = psycopg.conninfo.make_conninfo(**info)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(SCHEMA)
        pg.execute(f"INSERT INTO tenants (id, name) VALUES ('{DEFAULT_TENANT}','default')")
        pg.execute("""INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES
            ('openrouter','https://openrouter.ai/api/v1','OpenAICompatAdapter','bearer'),
            ('anthropic','https://api.anthropic.com','AnthropicAdapter','x-api-key')""")
        for r in ROWS:
            pg.execute(
                """INSERT INTO requests (tenant_id, provider_id, ts, model, route,
                   input_tokens_before, input_tokens_after, output_tokens,
                   est_cost_before, est_cost_after, cache_status, cache_savings,
                   latency_ms, compressed, status, l1_tokens_stripped, l1_savings,
                   provider_cache_read_tokens, provider_cache_write_tokens)
                   SELECT %s::uuid, p.id, %s, %s, %s, %s, %s, %s,
                   %s::numeric, %s::numeric, %s, %s::numeric, %s::numeric, %s, %s,
                   %s, %s::numeric, %s, %s
                   FROM providers p WHERE p.name = %s""",
                (DEFAULT_TENANT, r[0], r[2], r[3], r[4], r[5], r[6],
                 Decimal(str(r[7])), Decimal(str(r[8])), r[9],
                 Decimal(str(r[10])), r[11], r[12], r[13], r[14],
                 Decimal(str(r[15])), r[16], r[17], r[1]),
            )

    from proxy import kpis
    _real_dsn = kpis._dsn
    kpis._dsn = lambda: dsn
    yield kpis
    kpis._dsn = _real_dsn
    with psycopg.connect(base, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")


# ------------------------------------------------------------ by_route

def test_by_route_reconciles_to_fixture(v22_kpi_env):
    kpis = v22_kpi_env
    by_route = {r["route"]: r for r in kpis._fetch_kpis("hour", None, None)["by_route"]}
    assert set(by_route) == {r[3] for r in ROWS}
    for route in ("compress", "passthrough"):
        rows = [r for r in ROWS if r[3] == route]
        expected_cost = sum(Decimal(str(r[7])) - Decimal(str(r[8])) for r in rows)
        assert by_route[route]["requests"] == len(rows)
        assert by_route[route]["cost_saved"] == pytest.approx(float(expected_cost), abs=1e-9)


def test_by_route_is_window_global_across_buckets(v22_kpi_env):
    """F2 precedent: the route series does not fragment per bucket — every
    granularity returns the identical window-global array."""
    kpis = v22_kpi_env
    per_bucket = kpis._fetch_kpis("minute", None, None)["by_route"]
    per_day = kpis._fetch_kpis("day", None, None)["by_route"]
    per_hour = kpis._fetch_kpis("hour", None, None)["by_route"]
    assert per_bucket == per_hour == per_day
    assert [r["route"] for r in per_hour] == sorted(r["route"] for r in per_hour)


def test_by_route_respects_window_filter(v22_kpi_env):
    kpis = v22_kpi_env
    sub = kpis._fetch_kpis("hour", "2026-09-20T10:00:00", "2026-09-20T10:59:59")
    routes = {r["route"] for r in sub["by_route"]}
    assert routes == {"compress"}  # only the two 10:xx compress rows are in-window
    compress_rows = [r for r in ROWS if r[3] == "compress" and r[0].startswith("2026-09-20 10")]
    expected = sum(Decimal(str(r[7])) - Decimal(str(r[8])) for r in compress_rows)
    got = sub["by_route"][0]["cost_saved"]
    assert got == pytest.approx(float(expected), abs=1e-9)


# ------------------------------------------- provider-native cache usage

def test_provider_native_cache_only_on_measured_evidence(v22_kpi_env):
    """AC-V2-6: providers without evidence are ABSENT from the payload
    (field omitted), never zero-filled — the UI omits the row."""
    kpis = v22_kpi_env
    by_prov = {p["provider"]: p for p in kpis._fetch_kpis("hour", None, None)["by_provider"]}
    assert by_prov["openrouter"]["provider_cache_read_tokens"] == 512 + 300
    assert by_prov["openrouter"]["provider_cache_write_tokens"] == 1024
    assert "provider_cache_read_tokens" not in by_prov["anthropic"]
    assert "provider_cache_write_tokens" not in by_prov["anthropic"]


def test_provider_native_cache_never_merged_into_savings(v22_kpi_env):
    """Measured cache evidence is attribution evidence only: it must not
    inflate cache_savings, l1_savings, or cost_saved."""
    kpis = v22_kpi_env
    data = kpis._fetch_kpis("hour", None, None)
    ov = data["overview"]
    assert ov["cache_savings"] == pytest.approx(0.0004, abs=1e-9)  # exact_hit row only
    assert ov["l1_cost_saved"] == pytest.approx(0.0008, abs=1e-12)  # l1_savings sum
    by_prov = {p["provider"]: p for p in data["by_provider"]}
    assert by_prov["openrouter"]["cache_hits"] == 1
    # the native-cache provider rows still reconcile cost_saved independently
    or_rows = [r for r in ROWS if r[1] == "openrouter"]
    expected = sum(Decimal(str(r[7])) - Decimal(str(r[8])) for r in or_rows)
    assert by_prov["openrouter"]["cost_saved"] == pytest.approx(float(expected), abs=1e-9)


# ------------------------------------------------- D-2 additive wire shape

def test_kpis_wire_response_keeps_all_previous_fields(v22_kpi_env):
    """D-2 is additive: /api/kpis still carries overview, series, by_model,
    by_provider, latency — by_route and the native-cache fields join them."""
    from fastapi.testclient import TestClient
    from proxy.main import app

    client = TestClient(app)
    resp = client.get("/api/kpis?bucket=hour")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    for field in ("overview", "series", "by_model", "by_provider", "by_route", "latency"):
        assert field in data, field
    assert {r["route"] for r in data["by_route"]} == {"compress", "passthrough"}
