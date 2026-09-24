"""V2.1 strategy-attribution reconciliation (t_254f9f59, DBA audit package).

Reconciliation fixture for the new strategy-evidence surface introduced by
migration 7 (``strategy_telemetry``, t_cfccc06c) plus the retained V2.0
ledger decomposition it must not disturb:

- ``strategy_telemetry`` is EVIDENCE ONLY: its column set is pinned by
  assertion so a future "savings" column cannot silently appear in the
  telemetry table instead of its own migration-before-writer ledger change
  (ratified V2.1 data-contract ruling: non-overlapping decomposition);
- no-double-counting (AC-V2-4 extended to V2.1): one ledger row carrying the
  full V2.0 attribution payload (cache_savings, l1_savings,
  tool_compression_saved, provider_cache_read/write_tokens) plus per-lane
  strategy telemetry is aggregated at ROW, day BUCKET, PROVIDER, TENANT, and
  KEY levels; every addend is independently recomputed by hand and each
  attribution lane is proven non-derivable from the others;
- telemetry rows never bend ledger reconciliation: the /api/kpis overview and
  bucket series totals are identical with and without V2.1 telemetry present.

All expectations are hand-computed constants (the test_kpis.py convention:
never assert against the SQL output itself). Requires ``TOKEN_SAVER_PG_BASE``;
skips cleanly without it.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE, unique_db_name, require_pg_base  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_SCHEMA = REPO_ROOT / "postgres-schema-v2.sql"
V21_MIGRATION = (
    REPO_ROOT / "token-saver/migrations/20260924_v21_session_stores.sql"
)

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
KEY_A = "33333333-3333-3333-3333-333333333333"
KEY_B = "44444444-4444-4444-4444-444444444444"

STRATEGY_TELEMETRY_COLUMNS = (
    "id", "tenant_id", "api_key_id", "session_id", "ts", "strategy",
    "decision", "reason", "strategy_version", "flag_enabled", "latency_ms",
    "metadata",
)
LEDGER_ATTRIBUTION_COLUMNS = (
    "cache_savings",            # proxy-side cache attribution (exact/semantic)
    "l1_savings",               # L1 structural-clean attribution
    "tool_compression_saved",   # T1 component-local token estimate
    "provider_cache_read_tokens",   # V2.0 measured provider usage
    "provider_cache_write_tokens",  # V2.0 measured provider usage
)

# ---- hand-computed fixture expectations (independent of any SQL) ----
# Two ledger rows: one compress row carrying the full attribution payload,
# one passthrough row carrying only measured provider cache usage.
ROW1 = dict(cache_savings="0.50", l1_savings="0.25", tool_compression_saved=3,
            provider_cache_read_tokens=640, provider_cache_write_tokens=1000,
            in_before=1000, in_after=800, cost_before="0.30", cost_after="0.20",
            tenant=TENANT_A, key=KEY_A, provider="anthropic", ts="2026-09-24 10:00:00+00")
ROW2 = dict(cache_savings="0.00", l1_savings="0.00", tool_compression_saved=0,
            provider_cache_read_tokens=120, provider_cache_write_tokens=None,
            in_before=500, in_after=500, cost_before="0.10", cost_after="0.10",
            tenant=TENANT_B, key=KEY_B, provider="openrouter", ts="2026-09-24 10:10:00+00")

EXPECT = {
    "row1_cache_savings": Decimal(ROW1["cache_savings"]),
    "row1_l1_savings": Decimal(ROW1["l1_savings"]),
    "row1_tool_compression_saved": ROW1["tool_compression_saved"],
    "row1_provider_cache_read": ROW1["provider_cache_read_tokens"],
    "row1_provider_cache_write": ROW1["provider_cache_write_tokens"],
    "sum_cache_savings": Decimal(ROW1["cache_savings"]) + Decimal(ROW2["cache_savings"]),
    "sum_l1_savings": Decimal(ROW1["l1_savings"]) + Decimal(ROW2["l1_savings"]),
    "sum_tool_compression_saved": ROW1["tool_compression_saved"] + ROW2["tool_compression_saved"],
    "sum_provider_cache_read": ROW1["provider_cache_read_tokens"] + ROW2["provider_cache_read_tokens"],
    "sum_provider_cache_write": ROW1["provider_cache_write_tokens"],  # row2 None
    "bucket_10h_cache_savings": Decimal(ROW1["cache_savings"]) + Decimal(ROW2["cache_savings"]),
    "bucket_10h_l1_savings": Decimal(ROW1["l1_savings"]) + Decimal(ROW2["l1_savings"]),
}


@pytest.fixture(scope="module")
def recon_env():
    require_pg_base()
    name = unique_db_name("v21_attr_recon")
    dsn = f"{PG_BASE}/{name}"
    try:
        with psycopg.connect(PG_BASE, autocommit=True) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {name}")
            pg.execute(f"CREATE DATABASE {name}")
    except psycopg.OperationalError:
        pytest.skip(
            "TOKEN_SAVER_PG_BASE is unavailable for Postgres acceptance tests",
            allow_module_level=False,
        )
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(BASE_SCHEMA.read_text())
        pg.execute(V21_MIGRATION.read_text())
        for tid in (TENANT_A, TENANT_B):
            pg.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s)", (tid, tid[:8])
            )
        for kid, tid in ((KEY_A, TENANT_A), (KEY_B, TENANT_B)):
            pg.execute(
                """INSERT INTO api_keys (id, tenant_id, key_hash, key_last4)
                   VALUES (%s, %s, %s, 'last4')""",
                (kid, tid, "x" + kid[:8]),
            )
        pg.execute(
            """INSERT INTO providers (name, base_url, adapter_class, auth_style)
               VALUES ('anthropic', 'https://api.anthropic.com',
                       'AnthropicAdapter', 'x-api-key'),
                      ('openrouter', 'https://openrouter.ai/api/v1',
                       'OpenAICompatAdapter', 'bearer')"""
        )
        for r in (ROW1, ROW2):
            is_compress = Decimal(r["cache_savings"]) != 0
            pg.execute(
                """INSERT INTO requests
                       (tenant_id, api_key_id, provider_id, ts, model, route,
                        input_tokens_before, input_tokens_after, output_tokens,
                        est_cost_before, est_cost_after, cache_status,
                        cache_savings, l1_tokens_stripped, l1_savings,
                        tool_compression_saved,
                        provider_cache_read_tokens,
                        provider_cache_write_tokens, latency_ms, status)
                   SELECT %s, %s, p.id, %s, 'test-model', %s,
                          %s, %s, 10, %s::numeric, %s::numeric, %s,
                          %s::numeric, %s, %s::numeric, %s, %s, %s, 5.0, 200
                     FROM providers p WHERE p.name = %s""",
                (r["tenant"], r["key"], r["ts"],
                 "compress" if is_compress else "passthrough",
                 r["in_before"], r["in_after"], r["cost_before"],
                 r["cost_after"],
                 "exact_hit" if is_compress else "miss",
                 r["cache_savings"],
                 ROW1["tool_compression_saved"] if r is ROW1 else 0,
                 r["l1_savings"], r["tool_compression_saved"],
                 r["provider_cache_read_tokens"],
                 r["provider_cache_write_tokens"], r["provider"]),
            )
        # V2.1 strategy telemetry: per-lane evidence rows for the SAME two
        # requests. Deliberately many-to-one with the ledger: telemetry is a
        # decision log, not a second ledger.
        for strategy, decision in (("l1", "applied"), ("tocp", "applied"),
                                   ("atba", "shadow"), ("semantic_cache", "skipped")):
            pg.execute(
                """INSERT INTO strategy_telemetry
                       (tenant_id, api_key_id, session_id, strategy, decision,
                        reason, strategy_version, flag_enabled, latency_ms,
                        metadata)
                   VALUES (%s, %s, 'sess-1', %s, %s, 'probe', 'v1', false,
                           1.0, '{"k": "v"}'::jsonb)""",
                (TENANT_A, KEY_A, strategy, decision),
            )
        for strategy in ("l1", "tocp"):
            pg.execute(
                """INSERT INTO strategy_telemetry
                       (tenant_id, api_key_id, session_id, strategy, decision,
                        reason, strategy_version, flag_enabled, latency_ms,
                        metadata)
                   VALUES (%s, %s, 'sess-2', %s, 'applied', 'probe', 'v1',
                           false, 1.0, NULL)""",
                (TENANT_B, KEY_B, strategy),
            )
    yield dsn
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")


# ------------------------------------------------- evidence-only telemetry

def test_strategy_telemetry_column_set_is_pinned_evidence_only(recon_env):
    """The telemetry table must carry decision evidence and NOTHING that can
    be summed into savings. A savings field added here instead of the ledger
    (its own migration-before-writer change) fails this pin."""
    with psycopg.connect(recon_env, autocommit=True) as pg:
        columns = [
            r[0] for r in pg.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'strategy_telemetry' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        numeric = [
            r[0] for r in pg.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'strategy_telemetry' "
                "AND data_type IN ('integer', 'numeric', 'bigint') "
                "AND column_name NOT IN ('id', 'latency_ms')"
            ).fetchall()
        ]
        money_like = [
            r[0] for r in pg.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'strategy_telemetry' "
                "AND (column_name LIKE '%savings%' OR column_name LIKE '%saved%' "
                "     OR column_name LIKE '%cost%' OR column_name LIKE '%tokens%')"
            ).fetchall()
        ]
    assert tuple(columns) == STRATEGY_TELEMETRY_COLUMNS
    assert numeric == [], f"unexpected summable columns: {numeric}"
    assert money_like == [], f"unexpected savings-like columns: {money_like}"


def test_every_ratified_lane_decision_combination_is_writable(recon_env):
    """All ratified lanes (plan §4 A-F, plus the V2.0 lanes the registry must
    report) can record every decision value without a taxonomy migration."""
    lanes = ("tool_search", "tocp", "idcp", "atba", "mtcc", "registry",
             "l1", "output_conciseness", "provider_cache", "semantic_cache",
             "routing")
    decisions = ("eligible", "applied", "skipped", "fallback", "shadow")
    dsn = recon_env
    with psycopg.connect(dsn, autocommit=True) as pg:
        for lane in lanes:
            for decision in decisions:
                pg.execute(
                    """INSERT INTO strategy_telemetry
                           (tenant_id, session_id, strategy, decision,
                            reason, flag_enabled)
                       VALUES (%s, 'tax-probe', %s, %s, 'pin', false)""",
                    (TENANT_A, lane, decision),
                )
        count = pg.execute(
            "SELECT count(*) FROM strategy_telemetry WHERE session_id = 'tax-probe'"
        ).fetchone()[0]
    assert count == len(lanes) * len(decisions)


# --------------------------------------------- decomposition (no double count)

def _attribution_totals(dsn: str, where: str, params: tuple) -> dict:
    """SUM each attribution lane independently over the ledger."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(
            f"""SELECT COALESCE(SUM(cache_savings), 0),
                       COALESCE(SUM(l1_savings), 0),
                       COALESCE(SUM(tool_compression_saved), 0),
                       COALESCE(SUM(provider_cache_read_tokens), 0),
                       COALESCE(SUM(provider_cache_write_tokens), 0),
                       COALESCE(SUM(est_cost_before) - SUM(est_cost_after), 0),
                       COUNT(*)
                  FROM requests WHERE TRUE {where}""",
            params,
        ).fetchone()
    return {
        "cache_savings": row[0],
        "l1_savings": row[1],
        "tool_compression_saved": int(row[2]),
        "provider_cache_read": int(row[3]),
        "provider_cache_write": int(row[4]),
        "cost_saved": row[5],
        "rows": int(row[6]),
    }


def test_row_level_attribution_lanes_stay_independently_settable(recon_env):
    dsn = recon_env
    row = _attribution_totals(
        dsn, "AND model = 'test-model' AND input_tokens_before = %s",
        (ROW1["in_before"],),
    )
    assert row["rows"] == 1
    assert row["cache_savings"] == EXPECT["row1_cache_savings"]
    assert row["l1_savings"] == EXPECT["row1_l1_savings"]
    assert row["tool_compression_saved"] == EXPECT["row1_tool_compression_saved"]
    assert row["provider_cache_read"] == EXPECT["row1_provider_cache_read"]
    assert row["provider_cache_write"] == EXPECT["row1_provider_cache_write"]
    # No lane is derivable from another: each is its own measured field.
    lanes = (row["cache_savings"], row["l1_savings"], row["cost_saved"])
    assert len({str(v) for v in lanes}) == len(lanes), (
        "distinct attribution lanes must hold distinct measured values in the "
        "fixture — a merge would be undetectable"
    )


def test_bucket_level_decomposition_matches_hand_computation(recon_env):
    dsn = recon_env
    with psycopg.connect(dsn, autocommit=True) as pg:
        buckets = pg.execute(
            """SELECT (ts AT TIME ZONE 'UTC')::date,
                      COALESCE(SUM(cache_savings), 0),
                      COALESCE(SUM(l1_savings), 0),
                      COALESCE(SUM(tool_compression_saved), 0),
                      COALESCE(SUM(provider_cache_read_tokens), 0)
                 FROM requests GROUP BY 1 ORDER BY 1"""
        ).fetchall()
    assert len(buckets) == 1  # both rows share the 2026-09-24 day bucket
    day = buckets[0]
    assert str(day[0]) == "2026-09-24"
    assert day[1] == EXPECT["bucket_10h_cache_savings"]
    assert day[2] == EXPECT["bucket_10h_l1_savings"]
    assert day[3] == EXPECT["sum_tool_compression_saved"]
    assert day[4] == EXPECT["sum_provider_cache_read"]


def test_provider_tenant_and_key_levels_decompose_without_double_count(recon_env):
    dsn = recon_env
    per_provider = _attribution_totals(
        dsn, "AND provider_id = (SELECT id FROM providers WHERE name = %s)",
        ("anthropic",),
    )
    assert per_provider["rows"] == 1
    assert per_provider["cache_savings"] == EXPECT["row1_cache_savings"]
    assert per_provider["provider_cache_read"] == EXPECT["row1_provider_cache_read"]

    per_tenant = _attribution_totals(dsn, "AND tenant_id = %s", (TENANT_A,))
    assert per_tenant["rows"] == 1
    assert per_tenant["l1_savings"] == EXPECT["row1_l1_savings"]
    per_tenant_b = _attribution_totals(dsn, "AND tenant_id = %s", (TENANT_B,))
    assert per_tenant_b["provider_cache_read"] == ROW2["provider_cache_read_tokens"]
    assert per_tenant_b["cache_savings"] == Decimal("0")

    per_key = _attribution_totals(dsn, "AND api_key_id = %s", (KEY_A,))
    assert per_key["rows"] == 1
    assert per_key["tool_compression_saved"] == EXPECT["row1_tool_compression_saved"]

    # Whole-ledger sums equal the sum of the per-partition sums at each level
    # (no addend is counted twice by joining telemetry or any other surface).
    whole = _attribution_totals(dsn, "", ())
    assert whole["rows"] == 2
    assert whole["cache_savings"] == EXPECT["sum_cache_savings"]
    assert whole["cache_savings"] == per_provider["cache_savings"] + per_tenant_b["cache_savings"]
    assert whole["l1_savings"] == EXPECT["sum_l1_savings"] == per_tenant["l1_savings"]
    assert whole["tool_compression_saved"] == EXPECT["sum_tool_compression_saved"]
    assert whole["provider_cache_read"] == EXPECT["sum_provider_cache_read"]


def test_strategy_telemetry_never_alters_ledger_reconciliation(recon_env):
    """The KPI overview must be byte-identical whether or not V2.1 telemetry
    rows exist: telemetry is a parallel evidence surface, not a ledger input."""
    dsn = recon_env
    with psycopg.connect(dsn, autocommit=True) as pg:
        telemetry_rows = pg.execute(
            "SELECT count(*) FROM strategy_telemetry"
        ).fetchone()[0]
        assert telemetry_rows > 0
        # kpis._fetch_kpis runs SUM-over-ledger only; replicate its overview
        # query shape exactly (without importing the module so the fixture
        # stays independent of app wiring).
        def _overview() -> tuple:
            with psycopg.connect(dsn, autocommit=True) as conn:
                return conn.execute(
                    """SELECT COUNT(*),
                              COALESCE(SUM(input_tokens_before), 0),
                              COALESCE(SUM(input_tokens_after), 0),
                              COALESCE(SUM(est_cost_before)
                                       - SUM(est_cost_after), 0),
                              COALESCE(SUM(cache_savings), 0),
                              COALESCE(SUM(l1_tokens_stripped), 0),
                              COALESCE(SUM(l1_savings), 0)
                         FROM requests"""
                ).fetchone()

        before = _overview()
        pg.execute("DELETE FROM strategy_telemetry")
        after = _overview()
        # Restore the evidence surface.
        for strategy, decision in (("l1", "applied"), ("tocp", "applied"),
                                   ("atba", "shadow"), ("semantic_cache", "skipped")):
            pg.execute(
                """INSERT INTO strategy_telemetry
                       (tenant_id, api_key_id, session_id, strategy, decision,
                        reason, strategy_version, flag_enabled, latency_ms,
                        metadata)
                   VALUES (%s, %s, 'sess-1', %s, %s, 'probe', 'v1', false,
                           1.0, '{"k": "v"}'::jsonb)""",
                (TENANT_A, KEY_A, strategy, decision),
            )
        for strategy in ("l1", "tocp"):
            pg.execute(
                """INSERT INTO strategy_telemetry
                       (tenant_id, api_key_id, session_id, strategy, decision,
                        reason, strategy_version, flag_enabled, latency_ms,
                        metadata)
                   VALUES (%s, %s, 'sess-2', %s, 'applied', 'probe', 'v1',
                           false, 1.0, NULL)""",
                (TENANT_B, KEY_B, strategy),
            )
    assert before == after
    # And the reconciled overview totals still equal the hand computation.
    assert before[0] == 2
    assert before[1] == ROW1["in_before"] + ROW2["in_before"]
    assert before[2] == ROW1["in_after"] + ROW2["in_after"]
    assert before[4] == EXPECT["sum_cache_savings"]
    assert before[6] == EXPECT["sum_l1_savings"]
