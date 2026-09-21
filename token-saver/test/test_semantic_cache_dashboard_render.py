"""AC-PC-UI render gate — v1.1 semantic-cache dashboard surface.

Executes the real proxy/static/dashboard.js in Node (shared
dashboard_render_harness.js) against fixtures seeded with the FROZEN
`/api/kpis` top-level `cache` contract (cache-status-dashboard-spec.md §3,
product-spec AC-PC8) and asserts the locked behavior:

  S1. Mode states are distinct: flag off => neutral `off` + `—` (never 0%);
      enabled with no hits => `warming`; enabled with hits => `live`.
  S2. Poison case: flag off WITH historical semantic rows in the window still
      renders off/`—` (I-4 trumps history; `enabled` is a mode, not data).
  S3. Only-threshold-misses window: warming body + visible threshold-miss
      pressure, `—` hit rate (spec §7b).
  S4. Live window: every displayed number reproduces the seeded API field 1:1
      (counts, rates, savings, version array math — I-1/I-6); the combined
      `hit_rate` renders nowhere (§3).
  S5. Mixed multi-version window: version line = primary values + disagreement
      parenthetical whose count equals the array math byte-for-byte (§3.1).
  S6. Traffic tab: cache badges use the raw ledger status strings
      (exact_hit / semantic_hit / semantic_threshold_miss / miss) with
      verbatim counts; flag-off shows neutral `off` for semantic statuses.
  S7. No client aggregation: static scan — no .reduce, no accumulation of
      contract fields, and NO additive pairing of cost_saved / l1 / exact /
      semantic savings; `hit_rate` (combined) never read anywhere.
  S8. Loading / fetch-error / retry stay the shared AC-A8 pattern; single
      /api/kpis fetch; no secret-shaped material; version strings escaped.

The legacy no-`cache`-key fixture (v1.0 backend) must render unchanged
backward-compatible surfaces. Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dashboard_render as base  # noqa: E402  (reuse fixture, harness, formatters)

ROOT = base.ROOT
DASHBOARD_JS = base.DASHBOARD_JS
_fixture = base._fixture
_render = base._render
_js_money = base._js_money
ADDITIVE_DOUBLE_COUNT = base.ADDITIVE_DOUBLE_COUNT

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")


# ------------------------------------------------------------------ seeding

def _cache(**overrides) -> dict:
    """The frozen §3 contract shape, seeded with the spec's example numbers."""
    cache = {
        "enabled": True,
        "total_requests": 6,
        "exact_hit_count": 1,
        "semantic_hit_count": 2,
        "semantic_threshold_miss_count": 1,
        "miss_count": 2,
        "hit_rate": 50.0,
        "semantic_hit_rate": 33.33,
        "semantic_threshold_miss_rate": 16.67,
        "exact_hit_savings": 0.0004,
        "semantic_hit_savings": 0.0009,
        "embedding_versions": [
            {"version": "openai:text-embedding-3-small@1536", "hit_count": 2},
        ],
        "quality_versions": [{"version": "1.1.0", "hit_count": 2}],
    }
    cache.update(overrides)
    return cache


def _seeded_fixture(cache: dict | None) -> dict:
    fx = _fixture()
    if cache is not None:
        fx["cache"] = cache
    return fx


# ------------------------------------------------------- S1: distinct modes

def test_s1_flag_off_renders_off_mode_never_zero(tmp_path):
    fx = _seeded_fixture(_cache(enabled=False, total_requests=0,
                                exact_hit_count=0, semantic_hit_count=0,
                                semantic_threshold_miss_count=0, miss_count=0,
                                hit_rate=None, semantic_hit_rate=None,
                                semantic_threshold_miss_rate=None,
                                exact_hit_savings=0.0, semantic_hit_savings=0.0,
                                embedding_versions=[], quality_versions=[]))
    html = _render(tmp_path, fx, "overview")["html"]
    # KPI card: neutral `off` badge + em dash numeral, never 0%
    assert "Semantic cache hit rate" in html
    assert '<span class="badge neutral">off</span>' in html
    assert '<div class="kpi-num">—</div>' in html
    # semantic sub-tile: off badge + disabled line; no money, no rate, no 0%
    assert 'Semantic cache <span class="badge neutral">off</span>' in html
    assert "Semantic cache is disabled — no lookups are running." in html
    assert "warming" not in html and '<span class="badge green">live</span>' not in html
    # no numeric hit rate may render in the off state (— is the mode, not 0%)
    assert re.search(r"hit rate [\d.]", html) is None
    # the off sub-tile itself carries no measurement: no $, no rate, no count
    tile = html.split('Semantic cache <span class="badge neutral">off', 1)[1] \
               .split("</div></div>", 1)[0]
    assert "$" not in tile and "%" not in tile


def test_s1_warming_when_enabled_without_hits(tmp_path):
    fx = _seeded_fixture(_cache(semantic_hit_count=0, semantic_hit_rate=0.0,
                                semantic_hit_savings=0.0,
                                embedding_versions=[], quality_versions=[]))
    html = _render(tmp_path, fx, "overview")["html"]
    assert '<span class="badge gold">warming</span>' in html
    assert "No semantic hits yet — cache is warming." in html
    assert '<span class="badge green">live</span>' not in html
    assert "hit rate —" in html and "hit rate 0%" not in html


def test_s1_live_when_enabled_with_hits(tmp_path):
    html = _render(tmp_path, _seeded_fixture(_cache()), "overview")["html"]
    assert '<span class="badge green">live</span>' in html
    assert '<span class="badge gold">warming</span>' not in html


# ------------------------------------------------- S2: poison case (a)

def test_s2_flag_off_with_historical_semantic_rows_still_off(tmp_path):
    fx = _seeded_fixture(_cache(enabled=False))   # rows + versions still seeded
    html = _render(tmp_path, fx, "overview")["html"]
    assert '<span class="badge neutral">off</span>' in html
    assert '<span class="badge gold">warming</span>' not in html
    assert '<span class="badge green">live</span>' not in html
    assert "Semantic cache is disabled — no lookups are running." in html
    # history must not leak: no semantic rates, savings, or version line
    assert "33.33%" not in html
    assert _js_money(0.0009) not in html
    assert "embeddings:" not in html


# ------------------------------------------- S3: only-threshold-misses (b)

def test_s3_only_threshold_misses_warming_with_pressure(tmp_path):
    # contract-consistent warming seed: no semantic hits => no version arrays
    fx = _seeded_fixture(_cache(semantic_hit_count=0, semantic_hit_rate=0.0,
                                semantic_hit_savings=0.0,
                                semantic_threshold_miss_count=3,
                                semantic_threshold_miss_rate=50.0,
                                embedding_versions=[], quality_versions=[]))
    html = _render(tmp_path, fx, "overview")["html"]
    assert "No semantic hits yet — cache is warming." in html
    assert "3 threshold-misses" in html          # pressure visible
    assert "hit rate —" in html                  # warming mode, not 0%
    assert "hit rate 0%" not in html
    assert '<span class="badge gold">warming</span>' in html
    assert "embeddings:" not in html             # version line only when data
    # the hit-rate CARD is also — in warming (server field is 0.0; §5.2/§7b)
    card = html.split("Semantic cache hit rate", 1)[1].split("</div></div>", 1)[0]
    assert '"kpi-num">—' in card and "0%" not in card


# ----------------------------------------- S4: live verbatim 1:1 (I-1, §3)

def test_s4_live_numbers_reproduce_seeded_fields_verbatim(tmp_path):
    html = _render(tmp_path, _seeded_fixture(_cache()), "overview")["html"]
    # sub-tile: count + savings verbatim; rate line + threshold pressure
    assert _js_money(0.0009) in html              # semantic_hit_savings
    assert _js_money(0.0004) in html              # exact_hit_savings
    assert "hit rate 33.33% · 1 threshold-misses" in html
    # KPI card reads semantic_hit_rate ONLY — the combined hit_rate (50.0)
    # renders nowhere (§3)
    assert "33.33%" in html
    assert "50%" not in html and "50.0%" not in html and "50.00%" not in html
    # exact tile: verbatim count + savings from the cache fields
    assert "Exact cache" in html and "1 requests" in html


def test_s4_zero_threshold_misses_plain_rate_line(tmp_path):
    fx = _seeded_fixture(_cache(semantic_threshold_miss_count=0,
                                semantic_threshold_miss_rate=0.0))
    html = _render(tmp_path, fx, "overview")["html"]
    assert "hit rate 33.33%" in html
    assert "threshold-misses" not in html


# --------------------------------------------- S5: multi-version line (§3.1)

def test_s5_mixed_versions_parenthetical_matches_array_math(tmp_path):
    fx = _seeded_fixture(_cache(
        embedding_versions=[
            {"version": "openai:text-embedding-3-small@1536", "hit_count": 2},
            {"version": "openai:text-embedding-3-large@3072", "hit_count": 1},
        ],
        quality_versions=[
            {"version": "1.1.0", "hit_count": 2},
            {"version": "1.0.1", "hit_count": 1},
        ],
    ))
    html = _render(tmp_path, fx, "overview")["html"]
    primary_ev = "openai:text-embedding-3-small@1536"
    primary_qv = "1.1.0"
    # §3.1 parenthetical = sum of NON-primary hit_counts across both arrays,
    # byte-for-byte: (1) + (1) = 2
    expected = f"embeddings: {primary_ev} / {primary_qv} (+2 rows on other versions)"
    assert expected in html


def test_s5_single_version_line_without_parenthetical(tmp_path):
    html = _render(tmp_path, _seeded_fixture(_cache()), "overview")["html"]
    assert ("embeddings: openai:text-embedding-3-small@1536 / 1.1.0") in html
    assert "rows on other versions" not in html


def test_s5_version_line_escaped_never_raw_html(tmp_path):
    fx = _seeded_fixture(_cache(
        embedding_versions=[{"version": "openai:te<x>&\"bad\"@1536", "hit_count": 2}],
        quality_versions=[{"version": "1.1.0", "hit_count": 2}],
    ))
    html = _render(tmp_path, fx, "overview")["html"]
    assert "openai:te&lt;x&gt;&amp;&quot;bad&quot;@1536" in html
    assert "<x>" not in html


def test_s5_version_arrays_never_render_as_config_when_absent(tmp_path):
    fx = _seeded_fixture(_cache(embedding_versions=[], quality_versions=[]))
    html = _render(tmp_path, fx, "overview")["html"]
    assert "embeddings:" not in html


# ---------------------------------------------- S6: traffic raw-status badges

def test_s6_traffic_badges_use_raw_ledger_status(tmp_path):
    html = _render(tmp_path, _seeded_fixture(_cache()), "traffic")["html"]
    assert "Cache status (ledger taxonomy)" in html
    # badge text IS the raw ledger string — no renaming layer
    assert '<span class="badge green">exact_hit</span>' in html
    assert '<span class="badge green">semantic_hit</span>' in html
    assert '<span class="badge gold">semantic_threshold_miss</span>' in html
    assert '<span class="badge neutral">miss</span>' in html
    # counts verbatim: 1 / 2 / 1 / 2
    for count in ("1", "2"):
        assert f"<strong>{count}</strong>" in html


def test_s6_traffic_flag_off_semantic_statuses_show_off(tmp_path):
    fx = _seeded_fixture(_cache(enabled=False))
    html = _render(tmp_path, fx, "traffic")["html"]
    # exact + miss still count (unaffected by the flag)...
    assert '<span class="badge green">exact_hit</span>' in html
    assert '<span class="badge neutral">miss</span>' in html
    # ...semantic statuses are a mode: `off`, no count
    assert html.count('<span class="badge neutral">off</span>') >= 2
    assert '<span class="badge green">semantic_hit</span>' not in html
    assert '<span class="badge gold">semantic_threshold_miss</span>' not in html


def test_s6_traffic_no_cache_key_no_status_card(tmp_path):
    html = _render(tmp_path, _seeded_fixture(None), "traffic")["html"]
    assert "Cache status (ledger taxonomy)" not in html


# --------------------------------- S7: static — no aggregation, no hit_rate

_OPERAND = (r"(?:\w+\s*(?:\.\s*|\[\s*[\"']\s*))?"
            r"(cost_saved|l1_cost_saved|cache_savings|exact_hit_savings|"
            r"semantic_hit_savings)\b(?:[\"']\s*\])?")
ADDITIVE_CACHE = re.compile(_OPERAND + r"\s*\+\s*" + _OPERAND)
COMBINED_RATE = re.compile(r"(?<!semantic_)hit_rate")
FIELD_ACCUM = re.compile(
    r"(cost_saved|l1_cost_saved|cache_savings|exact_hit_savings|"
    r"semantic_hit_savings|total_requests|exact_hit_count|semantic_hit_count|"
    r"semantic_threshold_miss_count|miss_count)\b")


def test_s7_static_no_additive_savings_pattern_in_source():
    src = DASHBOARD_JS.read_text()
    assert not ADDITIVE_CACHE.search(src), ADDITIVE_CACHE.search(src).group(0)
    assert not ADDITIVE_DOUBLE_COUNT.search(src)


def test_s7_static_mutation_catches_cache_savings_sum():
    """Live mutation proof: summing exact+semantic savings must turn the
    static scan red — the P0 the rule exists to prevent."""
    src = DASHBOARD_JS.read_text()
    mutated = src + "\n//var x = cache.exact_hit_savings + cache.semantic_hit_savings;"
    assert ADDITIVE_CACHE.search(mutated), "static check MISSED cache-savings sum"


def test_s7_static_no_reduce_no_field_accumulation_no_combined_rate():
    src = DASHBOARD_JS.read_text()
    assert ".reduce(" not in src
    for i, line in enumerate(src.splitlines(), 1):
        if (re.search(r"\+=|\+\+", line) and FIELD_ACCUM.search(line)) or \
           (COMBINED_RATE.search(line)):
            assert False, f"line {i} violates the contract: {line.strip()}"


# ------------------------------------------------- S8: states, fetch, secrets

def test_s8_fetch_failure_shows_error_and_retry_refires(tmp_path):
    fx = tmp_path / "fixture_fail_cache.json"
    fx.write_text(json.dumps(_seeded_fixture(_cache())))
    proc = subprocess.run(
        ["node", str(base.HARNESS), str(fx), "overview", "fail-fetch"],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert "Couldn't load KPIs" in out["html"]
    assert "Retry" in out["html"]
    # the retry refires the same single /api/kpis request — no stale fallback
    assert out["urls"] == ["/api/kpis?bucket=day"] * 2, out["urls"]


def test_s8_single_fetch_is_api_kpis_with_cache_contract(tmp_path):
    for tab in ("overview", "traffic"):
        out = _render(tmp_path, _seeded_fixture(_cache()), tab)
        assert out["url"] == "/api/kpis?bucket=day"
        assert out["urls"] == ["/api/kpis?bucket=day"], f"{tab} fetched extra"


def test_s8_no_secret_material_with_versions_seeded(tmp_path):
    for tab in ("overview", "traffic"):
        out = _render(tmp_path, _seeded_fixture(_cache()), tab)
        assert not re.search(r"sk-[A-Za-z0-9_\-]{8,}|sk-ant-[A-Za-z0-9_\-]+",
                             out["html"]), f"{tab} rendered secret-shaped material"


def test_s8_savings_decomposition_note_names_all_three_categories(tmp_path):
    html = _render(tmp_path, _seeded_fixture(_cache()), "overview")["html"]
    assert ("Savings decomposition: exact, semantic, and L1 are per-request "
            "categories — never summed into the headline.") in html


# --------------------------------------------------- money fidelity (m2-style)

def _allowed_money(fx: dict) -> set[str]:
    """Every money string a 1:1 renderer may display for this fixture —
    base decomposition fields plus the frozen cache contract fields, and the
    ONE permitted client arithmetic (headline delta vs prior bucket)."""
    ov = fx["overview"]
    allowed = {ov[f] for f in ("cost_before", "cost_after", "cost_saved",
                               "cache_savings", "l1_cost_saved")}
    allowed |= {s[f] for s in fx["series"]
                for f in ("cost_saved", "cache_savings", "l1_cost_saved")}
    allowed |= {m["cost_saved"] for m in fx["by_model"]}
    allowed |= {p[f] for p in fx["by_provider"]
                for f in ("cost_saved", "l1_cost_saved")}
    if "cache" in fx:
        allowed |= {fx["cache"]["exact_hit_savings"],
                    fx["cache"]["semantic_hit_savings"]}
    s = fx["series"]
    allowed.add(abs(s[-1]["cost_saved"] - s[-2]["cost_saved"]))
    return {_js_money(v) for v in allowed}


def test_money_fidelity_with_cache_fields(tmp_path):
    fx = _seeded_fixture(_cache())
    allowed = _allowed_money(fx)
    out = _render(tmp_path, fx, "overview")
    displayed = re.findall(r"\$\d+\.\d{4}", out["html"])
    assert displayed, "overview rendered no money numerals"
    for m in displayed:
        assert m in allowed, f"{m} is NOT a verbatim fixture field"


# ------------------------------------------------------- legacy compat

def test_legacy_fixture_without_cache_key_renders_v1_surface(tmp_path):
    html = _render(tmp_path, _seeded_fixture(None), "overview")["html"]
    # v1.0 exact-prefix tile keeps its legacy shape
    assert "of which exact-prefix cache" in html
    assert "reported separately (AC-A6)" in html
    # semantic surfaces degrade to the off mode (feature ships disabled)
    assert '<span class="badge neutral">off</span>' in html
    assert "Semantic cache is disabled — no lookups are running." in html
    assert "embeddings:" not in html
    assert "Cache status (ledger taxonomy)" not in \
        _render(tmp_path, _seeded_fixture(None), "traffic")["html"]
