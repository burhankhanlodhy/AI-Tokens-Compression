"""K-4a: /metrics is bound to the Postgres KPI path (spec §41 single contract).

QA/PM finding: /metrics read SQLite aggregate_stats() while the ledger lived
in Postgres — requests_total was pinned at 0 on any live stack, which made
"no traffic" indistinguishable from "every ledger write silently dropped"
(the exact failure token_saver_ledger_write_failures exists to catch).

These tests pin:
  - requests_total (text AND ?format=json) == the Postgres ledger row count,
    cross-checked against independent SQL GROUP BYs (never the endpoint's own
    arithmetic);
  - the deliberate label mapping: requests_by_day (from `series`),
    requests_by_model, requests_by_provider; requests_by_route is DROPPED
    (the KPI contract has no route series);
  - ledger_write_failures still present;
  - a ledger-less deployment FAILS the scrape (503) instead of serving zeros;
  - /api/kpis without a DSN is 503 "ledger unavailable", not a bare 500.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_test_support import PG_BASE, unique_db_name  # noqa: E402

SCHEMA = (Path(__file__).resolve().parent.parent.parent
          / "postgres-schema-v2.sql").read_text()

_DB_NAME = unique_db_name("ts_k4a_metrics")

# ts, provider, model, route, in_b, in_a, out, cache_status, status
# Hand-computed expectations below stay independent of the endpoint's SQL.
ROWS = [
    ("2026-09-15 10:00:00+00", "openrouter", "z-ai/glm-5.3-flash",
     "compress", 1000, 600, 50, "miss", 200),
    ("2026-09-15 11:00:00+00", "openrouter", 'say "hi" \\ back',
     "passthrough", 500, 500, 20, "miss", 200),
    ("2026-09-15 12:00:00+00", "openrouter", "z-ai/glm-5.3-flash",
     "compress", 200, 100, 30, "miss", 200),
    ("2026-09-16 09:00:00+00", "anthropic", "claude-sonnet-5",
     "passthrough", 300, 300, 40, "miss", 200),
    ("2026-09-16 09:30:00+00", "anthropic", "claude-sonnet-5",
     "compress", 100, 50, 10, "miss", 200),
]

EXPECTED_TOTAL = 5
EXPECTED_TOKENS_SAVED = sum(r[4] - r[5] for r in ROWS)  # 550
EXPECTED_BY_MODEL = {"z-ai/glm-5.3-flash": 2, "claude-sonnet-5": 2,
                     'say \\"hi\\" \\\\ back': 1}
EXPECTED_BY_PROVIDER = {"openrouter": 3, "anthropic": 2}
EXPECTED_BY_DAY = {"2026-09-15": 3, "2026-09-16": 2}


@pytest.fixture(scope="module")
def metrics_env():
    """Throwaway PG DB + kpis._dsn patch; TestClient over the real app."""
    try:
        with psycopg.connect(PG_BASE, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")
            pg.execute(f"CREATE DATABASE {_DB_NAME}")
    except psycopg.OperationalError:
        pytest.skip("TOKEN_SAVER_PG_BASE is unavailable for Postgres tests",
                    allow_module_level=False)

    dsn = f"{PG_BASE}/{_DB_NAME}"
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(SCHEMA)
        pg.execute(
            "INSERT INTO tenants (id, name)"
            " VALUES ('00000000-0000-0000-0000-000000000000', 'default')")
        pg.execute(
            "INSERT INTO providers (name, base_url, adapter_class, auth_style)"
            " VALUES ('openrouter','https://openrouter.ai/api/v1',"
            " 'OpenAICompatAdapter','bearer'),"
            " ('anthropic','https://api.anthropic.com','AnthropicAdapter','x-api-key')"
        )
        for r in ROWS:
            pg.execute(
                """INSERT INTO requests (tenant_id, provider_id, ts, model, route,
                   input_tokens_before, input_tokens_after, output_tokens,
                   est_cost_before, est_cost_after, cache_status, cache_savings,
                   latency_ms, compressed, status, l1_tokens_stripped, l1_savings)
                   SELECT '00000000-0000-0000-0000-000000000000', p.id, %s, %s, %s,
                   %s, %s, %s, 0, 0, 'miss', 0, 100.0, false, %s, 0, 0
                   FROM providers p WHERE p.name = %s""",
                (r[0], r[2], r[3], r[4], r[5], r[6], r[8], r[1]),
            )

    from proxy import kpis
    _real_dsn = kpis._dsn
    kpis._dsn = lambda: dsn
    _real_env = os.environ.get("TOKEN_SAVER_PG_DSN")
    os.environ["TOKEN_SAVER_PG_DSN"] = dsn

    from fastapi.testclient import TestClient
    from proxy.main import app
    try:
        with TestClient(app) as client:
            yield client, dsn
    finally:
        kpis._dsn = _real_dsn
        if _real_env is None:
            os.environ.pop("TOKEN_SAVER_PG_DSN", None)
        else:
            os.environ["TOKEN_SAVER_PG_DSN"] = _real_env
        with psycopg.connect(PG_BASE, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_DB_NAME}")


def _series(text: str, name: str) -> dict[str, float]:
    """Parse `{label="v"} value` series lines for one metric name.

    Label values keep their ESCAPED form (\\", \\\\, \\n) so callers compare
    against the escaped rendering, not the raw stored string.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line:
            continue
        metric, _, rest = line.partition("{")
        if metric != name:
            continue
        # scan the quoted label value, honouring backslash escapes
        start = rest.find('="')
        assert start != -1, rest
        chars: list[str] = []
        i = start + 2
        while i < len(rest):
            if rest[i] == "\\" and i + 1 < len(rest):
                chars.append(rest[i:i + 2])
                i += 2
            elif rest[i] == '"':
                break
            else:
                chars.append(rest[i])
                i += 1
        value = rest[i + 1:].strip().lstrip("} ")
        out["".join(chars)] = float(value)
    return out


def test_metrics_requests_total_matches_ledger_count(metrics_env):
    """K-4a acceptance: token_saver_requests_total == Postgres ledger count."""
    client, dsn = metrics_env
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    with psycopg.connect(dsn) as pg:
        ledger_count = int(pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    # headline counter: value line, not a label series
    total_lines = [l for l in r.text.splitlines()
                   if l.startswith("token_saver_requests_total ")]
    assert len(total_lines) == 1
    assert float(total_lines[0].rsplit(" ", 1)[1]) == ledger_count == EXPECTED_TOTAL

    # independent GROUP BY reconciliation of the label series
    by_model = _series(r.text, "token_saver_requests_by_model")
    assert {k: int(v) for k, v in by_model.items()} == EXPECTED_BY_MODEL
    by_provider = _series(r.text, "token_saver_requests_by_provider")
    assert {k: int(v) for k, v in by_provider.items()} == EXPECTED_BY_PROVIDER
    by_day = _series(r.text, "token_saver_requests_by_day")
    assert {k: int(v) for k, v in by_day.items()} == EXPECTED_BY_DAY
    assert sum(by_day.values()) == ledger_count

    # deliberate mapping: by_route is DROPPED (KPI contract has no route series)
    assert not [l for l in r.text.splitlines() if "requests_by_route" in l]
    # the one module-global series survives (compare against the LIVE counter —
    # reverse-order suites run ledger-failure-path tests before this module,
    # so the process-global value is not guaranteed to be 0)
    import proxy.main as main_mod
    assert f"token_saver_ledger_write_failures {main_mod.LEDGER_WRITE_FAILURES}" in r.text
    assert "# TYPE token_saver_requests_total counter" in r.text


def test_metrics_json_summary_matches_ledger_count(metrics_env):
    client, dsn = metrics_env
    r = client.get("/metrics?format=json")
    assert r.status_code == 200
    body = r.json()
    with psycopg.connect(dsn) as pg:
        ledger_count = int(pg.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        saved = int(pg.execute(
            "SELECT COALESCE(SUM(input_tokens_before - input_tokens_after), 0)"
            " FROM requests").fetchone()[0])
    assert body["requests"] == ledger_count == EXPECTED_TOTAL
    assert body["tokens_saved"] == saved == EXPECTED_TOKENS_SAVED
    import proxy.main as main_mod
    assert body["ledger_write_failures"] == main_mod.LEDGER_WRITE_FAILURES


def test_metrics_label_values_are_prometheus_escaped(metrics_env):
    """A client-controlled model name cannot break the text exposition."""
    client, _ = metrics_env
    r = client.get("/metrics")
    by_model = _series(r.text, "token_saver_requests_by_model")
    assert 'say \\"hi\\" \\\\ back' in by_model
    assert by_model['say \\"hi\\" \\\\ back'] == 1.0


def test_metrics_fails_loudly_without_ledger(monkeypatch):
    """No DSN configured: the scrape must FAIL (503), never report zeros —
    zeros are indistinguishable from silently dropped ledger writes.
    _dsn is patched to raise the exact exception get_pg_dsn() raises when
    TOKEN_SAVER_PG_DSN is unset (the module-scope fixture has its own patch
    still installed, so the env name alone is not sufficient here)."""
    from proxy import kpis

    def _no_dsn():
        raise RuntimeError("TOKEN_SAVER_PG_DSN must be set")

    monkeypatch.setattr(kpis, "_dsn", _no_dsn)
    from fastapi.testclient import TestClient
    from proxy.main import app
    client = TestClient(app)
    r_text = client.get("/metrics")
    assert r_text.status_code == 503
    assert "ledger unavailable" in r_text.text
    r_json = client.get("/metrics?format=json")
    assert r_json.status_code == 503


def test_kpis_503_not_500_without_dsn(monkeypatch):
    """K-4a companion fix: /api/kpis with no DSN configured is 503
    'ledger unavailable' (was an unhandled RuntimeError -> bare 500)."""
    from proxy import kpis

    def _no_dsn():
        raise RuntimeError("TOKEN_SAVER_PG_DSN must be set")

    monkeypatch.setattr(kpis, "_dsn", _no_dsn)
    from fastapi.testclient import TestClient
    from proxy.main import app
    client = TestClient(app)
    r = client.get("/api/kpis?bucket=day")
    assert r.status_code == 503
    assert "ledger unavailable" in r.text
