"""PA-2 automated tests: /api/kpis ledger reconciliation (AC-A5/A12).

Uses a dedicated throwaway Postgres database seeded with hand-computable
ledger rows; every assertion reconciles the API response against values
computed independently in the test (never against the SQL output itself).
Requires a running Postgres (dev default: localhost:5433, docker container
`token-saver-postgres`); skipped when unavailable so unit suites stay green
in offline environments.
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PSYCHOPG = None
try:
    import psycopg
    PSYCHOPG = psycopg
except ImportError:  # pragma: no cover
    pytest.skip("psycopg not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE  # noqa: E402
# database name lives in the path component; keep base + name separately so
# patching kpis._dsn never turns the DB name into a hostname
_DB_NAME = "ts_kpi_test"


def _base_dsn() -> str:
    """DSN to the admin database (for CREATE/DROP DATABASE)."""
    return PG_BASE


def _test_dsn() -> str:
    return f"{PG_BASE}/{_DB_NAME}"

SCHEMA = (Path(__file__).resolve().parent.parent.parent
          / "postgres-schema-v2.sql").read_text()

ROWS = [  # ts, provider, model, route, in_b, in_a, out, cb, ca, cache, cache_sav, lat, comp, status, l1_tok, l1_sav
    ("2026-09-14 10:00:00+00", "openrouter", "z-ai/glm-5.3-flash", "compress", 1000, 600, 50, 0.001, 0.0006, "miss", 0, 100.0, True, 200, 120, 0.0001),
    ("2026-09-14 10:30:00+00", "openrouter", "z-ai/glm-5.3-flash", "compress", 2000, 1000, 80, 0.002, 0.001, "exact_hit", 0.0004, 120.0, True, 200, 200, 0.0003),
    ("2026-09-14 11:15:00+00", "openrouter", "openai/gpt-4o", "passthrough", 500, 500, 200, 0.0025, 0.0025, "miss", 0, 300.0, False, 200, 0, 0.0),
    ("2026-09-14 12:00:00+00", "anthropic", "claude-sonnet-5", "compress", 800, 400, 60, 0.0024, 0.0012, "miss", 0, 200.0, True, 200, 80, 0.0004),
    ("2026-09-15 09:00:00+00", "openrouter", "z-ai/glm-5.3-flash", "compress", 100, 50, 10, 0.0001, 0.00005, "miss", 0, 80.0, True, 500, 10, 0.00002),
    ("2026-09-15 09:05:00+00", "anthropic", "claude-sonnet-5", "passthrough", 300, 300, 40, 0.0009, 0.0009, "miss", 0, 150.0, False, 200, 0, 0.0),
]

# Hand-computed expectations (independent of the SQL under test)
EXPECTED = {
    "requests": 6,
    "input_tokens_before": sum(r[4] for r in ROWS),        # 4700
    "input_tokens_after": sum(r[5] for r in ROWS),         # 2850
    "input_tokens_saved": sum(r[4] - r[5] for r in ROWS),  # 1850
    "cache_hits": sum(1 for r in ROWS if r[9] == "exact_hit"),   # 1
    "errors": sum(1 for r in ROWS if r[13] >= 400),              # 1
    "cache_savings": sum(Decimal(str(r[10])) for r in ROWS),     # 0.0004
    "cost_saved": sum(Decimal(str(r[7])) - Decimal(str(r[8])) for r in ROWS),
}


@pytest.fixture(scope="module")
def kpi_env():
    """Fresh throwaway DB with the schema + seeded rows; patched kpis._dsn."""
    try:
        with psycopg.connect(_base_dsn(), autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")
            pg.execute(f"CREATE DATABASE {_DB_NAME}")
    except psycopg.OperationalError:
        pytest.skip("TOKEN_SAVER_PG_BASE is unavailable for Postgres acceptance tests", allow_module_level=False)

    dsn = _test_dsn()
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(SCHEMA)
        pg.execute("INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000000','default')")
        pg.execute("""INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES
            ('openrouter','https://openrouter.ai/api/v1','OpenAICompatAdapter','bearer'),
            ('anthropic','https://api.anthropic.com','AnthropicAdapter','x-api-key')""")
        for r in ROWS:
            pg.execute(
                """INSERT INTO requests (tenant_id, provider_id, ts, model, route,
                   input_tokens_before, input_tokens_after, output_tokens,
                   est_cost_before, est_cost_after, cache_status, cache_savings,
                   latency_ms, compressed, status, l1_tokens_stripped, l1_savings)
                   SELECT '00000000-0000-0000-0000-000000000000', p.id, %s, %s, %s, %s, %s, %s,
                   %s::numeric, %s::numeric, %s, %s::numeric, %s::numeric, %s, %s,
                   %s, %s::numeric
                   FROM providers p WHERE p.name = %s""",
                (r[0], r[2], r[3], r[4], r[5], r[6], Decimal(str(r[7])),
                 Decimal(str(r[8])), r[9], Decimal(str(r[10])), r[11], r[12], r[13],
                 r[14], Decimal(str(r[15])), r[1]),
            )

    from proxy import kpis
    # B-9d: restore the real _dsn on teardown — an unconditional overwrite
    # here left the global pointing at the torn-down throwaway DB, so any
    # module running after this one 503'd on "database ts_kpi_test does not
    # exist" (same leak class as B-9b; suite was green only by alphabet).
    _real_dsn = kpis._dsn
    kpis._dsn = lambda: dsn
    yield kpis
    kpis._dsn = _real_dsn

    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")


# ------------------------------------------------------------ overview reconciliation

def test_overview_reconciles_to_ledger(kpi_env):
    kpis = kpi_env
    ov = kpis._fetch_kpis("hour", None, None)["overview"]
    assert ov["requests"] == EXPECTED["requests"]
    assert ov["input_tokens_before"] == EXPECTED["input_tokens_before"]
    assert ov["input_tokens_after"] == EXPECTED["input_tokens_after"]
    assert ov["input_tokens_saved"] == EXPECTED["input_tokens_saved"]
    assert ov["savings_pct"] == round(100 * EXPECTED["input_tokens_saved"]
                                      / EXPECTED["input_tokens_before"], 2)
    assert ov["cache_hits"] == EXPECTED["cache_hits"]
    assert ov["errors"] == EXPECTED["errors"]
    assert abs(ov["cost_saved"] - float(EXPECTED["cost_saved"])) < 1e-9
    assert abs(ov["cache_savings"] - float(EXPECTED["cache_savings"])) < 1e-9


def test_cache_reported_separately_from_compression(kpi_env):
    """AC-A6: cache_savings is its own field, never merged into cost_saved."""
    kpis = kpi_env
    ov = kpis._fetch_kpis("hour", None, None)["overview"]
    assert "cache_savings" in ov and "cost_saved" in ov
    assert ov["cache_savings"] == pytest.approx(0.0004, abs=1e-9)
    # cost_saved must not include the cache savings
    assert ov["cost_saved"] == pytest.approx(float(EXPECTED["cost_saved"]), abs=1e-9)
    assert ov["cost_saved"] != ov["cost_saved"] + ov["cache_savings"]


# ------------------------------------------------------------ L1 reconciliation

# Hand-computed L1 expectations, independent of the SQL under test.
EXPECTED_L1_TOKENS = sum(r[14] for r in ROWS)          # 410
EXPECTED_L1_SAVINGS = sum(Decimal(str(r[15])) for r in ROWS)  # 0.00082


def test_l1_savings_never_exceed_cost_saved(kpi_env):
    """AC (PM-requested): l1_cost_saved is a PORTION of cost_saved on every
    aggregation level (spec a1eae90) — never an addend, never exceeding it.
    UI L1 tiles are blocked on this reconciliation existing."""
    kpis = kpi_env
    data = kpis._fetch_kpis("hour", None, None)
    ov = data["overview"]
    # overview level
    assert ov["l1_cost_saved"] <= ov["cost_saved"]
    assert ov["l1_tokens_stripped"] == EXPECTED_L1_TOKENS
    assert ov["l1_cost_saved"] == pytest.approx(float(EXPECTED_L1_SAVINGS), abs=1e-12)
    # bucket level (hourly): rows 0+1 share the 10:00 hour; compute the
    # expected l1 totals per bucket from the fixture itself
    import collections
    by_hour = collections.defaultdict(lambda: [0, Decimal("0")])
    for r in ROWS:
        acc = by_hour[r[0][:13]]
        acc[0] += r[14]
        acc[1] += Decimal(str(r[15]))
    for s in data["series"]:
        exp_tok, exp_sav = by_hour[s["bucket"][:13].replace("T", " ")]
        assert 0 <= s["l1_cost_saved"] <= s["cost_saved"]
        assert s["l1_tokens_stripped"] == exp_tok
        assert s["l1_cost_saved"] == pytest.approx(float(exp_sav), abs=1e-12)
    # every breakdown row must respect the invariant too (by_provider carries
    # the l1_* fields; by_model currently does not expose them — noted gap,
    # not asserted here)
    for row in data["by_provider"]:
        assert 0 <= row["l1_cost_saved"] <= row["cost_saved"]


def test_l1_attribution_absent_on_passthrough_rows(kpi_env):
    """Passthrough routes must carry zero L1 attribution (taxonomy v1.1 §5:
    L1 is off for passthrough) — guards the P0 pipeline gate at the KPI level."""
    kpis = kpi_env
    passthrough_rows = [r for r in ROWS if r[3] == "passthrough"]
    assert passthrough_rows, "fixture must contain passthrough rows"
    expected_l1 = sum(Decimal(str(r[15])) for r in passthrough_rows)
    assert expected_l1 == 0  # fixture itself honors the contract
    # decompose: l1_savings over rows whose route is passthrough must be 0,
    # so overview l1_cost_saved equals the compress-route contribution alone.
    compress_only = sum(Decimal(str(r[15])) for r in ROWS if r[3] == "compress")
    ov = kpis._fetch_kpis("hour", None, None)["overview"]
    assert ov["l1_cost_saved"] == pytest.approx(float(compress_only), abs=1e-12)


def test_error_rate_and_cache_hit_pct(kpi_env):
    kpis = kpi_env
    ov = kpis._fetch_kpis("hour", None, None)["overview"]
    assert ov["cache_hit_pct"] == round(100 * EXPECTED["cache_hits"] / EXPECTED["requests"], 2)
    assert ov["error_rate_pct"] == round(100 * EXPECTED["errors"] / EXPECTED["requests"], 2)


# ------------------------------------------------------------ buckets & ranges

def test_bucket_granularity(kpi_env):
    kpis = kpi_env
    # 6 rows fall in 6 distinct minutes
    assert len(kpis._fetch_kpis("minute", None, None)["series"]) == 6
    # 4 distinct hours: 14th 10:00, 11:00, 12:00 + 15th 09:00
    hour_series = kpis._fetch_kpis("hour", None, None)["series"]
    assert len(hour_series) == 4
    assert hour_series[0]["requests"] == 2  # 10:00 and 10:30
    # 2 distinct days
    assert len(kpis._fetch_kpis("day", None, None)["series"]) == 2


def test_bucket_series_reconciles_to_overview(kpi_env):
    kpis = kpi_env
    data = kpis._fetch_kpis("hour", None, None)
    series_reqs = sum(s["requests"] for s in data["series"])
    series_saved = sum(s["tokens_saved"] for s in data["series"])
    assert series_reqs == data["overview"]["requests"]
    assert series_saved == data["overview"]["input_tokens_saved"]


def test_from_to_range_filter(kpi_env):
    kpis = kpi_env
    sub = kpis._fetch_kpis("hour", "2026-09-14T00:00:00", "2026-09-14T23:59:59")
    expected_rows = [r for r in ROWS if r[0].startswith("2026-09-14")]
    assert sub["overview"]["requests"] == len(expected_rows)  # 4
    assert sub["overview"]["input_tokens_saved"] == sum(r[4] - r[5] for r in expected_rows)
    # the 15th's error row is excluded
    assert sub["overview"]["errors"] == 0


# ------------------------------------------------------------ percentiles

def test_latency_percentiles_ordered_and_correct(kpi_env):
    kpis = kpi_env
    lat = kpis._fetch_kpis("hour", None, None)["latency"]
    lats = sorted(r[11] for r in ROWS)
    assert lat["p50"] == pytest.approx(lats[2] if False else 135.0, abs=1e-6)
    assert lat["p50"] <= lat["p95"] <= lat["p99"]
    # bounds: within min/max of the actual latencies
    assert lats[0] <= lat["p50"] <= lats[-1]
    assert lats[0] <= lat["p99"] <= lats[-1]


# ------------------------------------------------------------ breakdowns

def test_by_model_breakdown(kpi_env):
    kpis = kpi_env
    by_model = {m["model"]: m for m in kpis._fetch_kpis("hour", None, None)["by_model"]}
    assert set(by_model) == {r[2] for r in ROWS}
    glm = by_model["z-ai/glm-5.3-flash"]
    glm_rows = [r for r in ROWS if r[2] == "z-ai/glm-5.3-flash"]
    assert glm["requests"] == len(glm_rows)  # 3
    assert glm["tokens_saved"] == sum(r[4] - r[5] for r in glm_rows)  # 1450


def test_by_provider_breakdown(kpi_env):
    kpis = kpi_env
    by_prov = {p["provider"]: p for p in kpis._fetch_kpis("hour", None, None)["by_provider"]}
    assert by_prov["openrouter"]["requests"] == 4
    assert by_prov["anthropic"]["requests"] == 2
    or_rows = [r for r in ROWS if r[1] == "openrouter"]
    assert by_prov["openrouter"]["errors"] == 1  # the 15th 500
    assert by_prov["anthropic"]["errors"] == 0


# ------------------------------------------------------------ input validation & failure paths

def test_invalid_bucket_400(kpi_env):
    import asyncio
    kpis = kpi_env
    from proxy.main import app  # endpoint wiring present
    resp = asyncio.run(kpis.kpis_endpoint(bucket="week", from_ts=None, to_ts=None))
    assert resp.status_code == 400


# ------------------------------------------------------------ B-10: param validation (400, field-named)

# (param, malformed value, field the error message must name)
# NOTE: from/to are validated under their own names, but the direct-call
# signature only carries from_ts/to_ts (the wrapper merges the aliases) —
# so the direct-call probe passes the value via the legacy kwarg.
MALFORMED_PROBES = [
    ("bucket", "bogus", "bucket", "bucket"),             # pre-existing contract, kept as regression
    ("from", "not-a-date", "from", "from_ts"),
    ("to", "garbage", "to", "to_ts"),
    ("from_ts", "not-a-date", "from", "from_ts"),        # legacy alias — validated too
    ("to_ts", "garbage", "to", "to_ts"),
    ("tenant_id", "not-a-uuid", "tenant_id", "tenant_id"),
    ("api_key_id", "nope", "api_key_id", "api_key_id"),
    ("from", "2026-01-01'; DROP TABLE requests;--", "from", "from_ts"),  # SQL-ish: 400 before PG
]


@pytest.mark.parametrize("param,value,field,kwarg", MALFORMED_PROBES)
def test_malformed_param_400_not_500(kpi_env, param, value, field, kwarg):
    """B-10: every malformed filter param is a client error (400) with a
    field-named message — matching ?bucket= — never a bare 500."""
    import asyncio
    kpis = kpi_env
    resp = asyncio.run(kpis.kpis_endpoint(**{"bucket": "day", kwarg: value}))
    assert resp.status_code == 400, (param, resp.body.decode())
    assert field in resp.body.decode()


def test_wire_malformed_params_400(kpi_env):
    """B-10 acceptance over the wire: probes through the real ASGI app (the
    api_kpis wrapper — the single binding site), the way the 500s were found.
    A valid selector must still pass validation and return 200."""
    from fastapi.testclient import TestClient
    from proxy.main import app
    kpis = kpi_env
    client = TestClient(app)
    for url, field in [("/api/kpis?bucket=bogus", "bucket"),
                       ("/api/kpis?from=not-a-date", "from"),
                       ("/api/kpis?to=garbage", "to"),
                       ("/api/kpis?from_ts=not-a-date", "from"),
                       ("/api/kpis?tenant_id=not-a-uuid", "tenant_id"),
                       ("/api/kpis?api_key_id=nope", "api_key_id"),
                       ("/api/kpis?from=2026-01-01'; DROP TABLE requests;--", "from")]:
        resp = client.get(url)
        assert resp.status_code == 400, (url, resp.status_code, resp.text)
        assert field in resp.json()["error"], (url, resp.json())
    # positive controls: valid values pass validation and reach the ledger
    ok_range = client.get("/api/kpis?from=2026-09-14&to=2026-09-15")
    assert ok_range.status_code == 200, ok_range.text
    assert ok_range.json()["overview"]["requests"] == 4
    ok_tenant = client.get(
        "/api/kpis?tenant_id=00000000-0000-0000-0000-000000000000")
    assert ok_tenant.status_code == 200, ok_tenant.text
    assert ok_tenant.json()["overview"]["requests"] == 6


def test_ledger_unavailable_503(kpi_env, monkeypatch):
    import asyncio
    kpis = kpi_env
    # monkeypatch restores the real _dsn — an unconditional overwrite here
    # poisoned every later module-scoped test (same defect class as B-9b)
    monkeypatch.setattr(kpis, "_dsn",
                        lambda: "postgresql://invalid-host:59999/none")
    resp = asyncio.run(kpis.kpis_endpoint(bucket="day", from_ts=None, to_ts=None))
    assert resp.status_code == 503
    assert "error" in resp.body.decode()


def test_endpoint_registered_in_app():
    from proxy.main import app
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/api/kpis" in paths
