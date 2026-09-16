"""Savings-breakdown panel render tests (B2 follow-up, a1eae90 decomposition).

Executes the real proxy/static/dashboard.js in Node (minimal DOM stub) against
fixtures modeled on the pinned test_kpis.py rows (l1_tokens_stripped == 410,
l1_cost_saved == 0.00082, cost_saved == 0.00265) and asserts the UI/UX render
rules:

  R1. Headline card = cost_saved ALONE, exact fixture reproduction, delta vs
      prior bucket, sparkline present.
  R2. No render path sums cost_saved with l1_cost_saved or cache_savings
      (dynamic: summed money string absent; static: no additive pattern in JS).
  R3. Zero-guard: l1_tokens_stripped == 0 renders a dash + guidance line, no
      implied $0.00 L1 value.
  R4. No Chart.js surface references l1_* fields (by_model carries none — the
      endpoint can't reproduce them, so no chart may fabricate them).
  R5. Providers tab shows per-provider L1 (by_provider carries it) with the
      same zero-guard.

Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "test" / "dashboard_render_harness.js"
DASHBOARD_JS = ROOT / "proxy" / "static" / "dashboard.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")


def _node(args: list[str]) -> str:
    proc = subprocess.run(["node", *args], capture_output=True, text=True,
                          timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout.strip()


def _js_money(x: float) -> str:
    """The exact string dashboard.js money() produces for x (JS toFixed(4))."""
    return "$" + _node(["-e", f"console.log(({x!r}).toFixed(4))"])


def _render(tmp_path: Path, fixture: dict, tab: str) -> dict:
    fx = tmp_path / f"fixture_{tab}.json"
    fx.write_text(json.dumps(fixture))
    out = _node([str(HARNESS), str(fx), tab])
    return json.loads(out)


def _fixture(l1_tokens: int = 410, l1_cost: float = 0.00082) -> dict:
    """Mirrors the pinned test_kpis.py aggregates (hand-computed there)."""
    return {
        "bucket": "day",
        "overview": {
            "requests": 6,
            "input_tokens_before": 4700,
            "input_tokens_after": 2850,
            "input_tokens_saved": 1850,
            "savings_pct": 39.36,
            "output_tokens": 440,
            "cost_before": 0.0089,
            "cost_after": 0.00625,
            "cost_saved": 0.00265,
            "cache_savings": 0.0004,
            "l1_tokens_stripped": l1_tokens,
            "l1_cost_saved": l1_cost,
            "cache_hits": 1,
            "cache_hit_pct": 16.67,
            "errors": 1,
            "error_rate_pct": 16.67,
            "avg_latency_ms": 158.33,
        },
        "series": [
            {"bucket": "2026-09-14T00:00:00", "requests": 4, "tokens_saved": 1450,
             "cost_saved": 0.0016, "cache_savings": 0.0004,
             "l1_tokens_stripped": 400, "l1_cost_saved": 0.0008, "errors": 0},
            {"bucket": "2026-09-15T00:00:00", "requests": 2, "tokens_saved": 400,
             "cost_saved": 0.00105, "cache_savings": 0,
             "l1_tokens_stripped": 10, "l1_cost_saved": 0.00002, "errors": 1},
        ],
        "by_model": [
            {"model": "z-ai/glm-5.3-flash", "requests": 3, "tokens_saved": 1450,
             "cost_saved": 0.00145},
            {"model": "openai/gpt-4o", "requests": 1, "tokens_saved": 0,
             "cost_saved": 0.0},
            {"model": "claude-sonnet-5", "requests": 2, "tokens_saved": 400,
             "cost_saved": 0.0012},
        ],
        "by_provider": [
            {"provider": "openrouter", "requests": 4, "tokens_saved": 1450,
             "cost_saved": 0.00145, "cache_hits": 1, "cache_hit_pct": 25.0,
             "l1_tokens_stripped": 330, "l1_cost_saved": 0.00042,
             "errors": 1, "error_pct": 25.0},
            {"provider": "anthropic", "requests": 2, "tokens_saved": 400,
             "cost_saved": 0.0012, "cache_hits": 0, "cache_hit_pct": 0.0,
             "l1_tokens_stripped": 80, "l1_cost_saved": 0.0004,
             "errors": 0, "error_pct": 0.0},
        ],
        "latency": {"p50": 135.0, "p95": 280.0, "p99": 296.0},
    }


# ------------------------------------------------------------------ R1: headline

def test_r1_headline_reproduces_pinned_fixture_numbers(tmp_path):
    out = _render(tmp_path, _fixture(), "overview")
    html = out["html"]
    # headline numeral is cost_saved ALONE, byte-exact with JS formatting
    assert _js_money(0.00265) in html
    # L1 sub-tile reproduces the pinned 410 / 0.00082 as a PORTION
    assert "410" in html and _js_money(0.00082) in html
    assert "of which L1 structural" in html
    assert "portion of total" in html
    # delta arrow vs prior bucket + sparkline canvas present
    assert "vs prior bucket" in html and "sp-cost" in html
    assert "▲" in html or "▼" in html


# ------------------------------------------------------------ R2: no double-count

def test_r2_no_render_path_sums_l1_with_total(tmp_path):
    out = _render(tmp_path, _fixture(), "overview")
    html = out["html"]
    # neither the L1 sum (0.00265+0.00082=0.00347) nor the cache sum
    # (0.00265+0.0004=0.00305) may appear anywhere in the rendered surface
    assert _js_money(0.00265 + 0.00082) not in html
    assert _js_money(0.00265 + 0.0004) not in html


_FORBIDDEN_FIELD = r"(cost_saved|l1_cost_saved|cache_savings)"
# operand = optional qualifier (dotted `ov.` or bracketed `ov["`...`"]`) + field
_OPERAND = (r"(?:\w+\s*(?:\.\s*|\[\s*[\"']\s*))?" + _FORBIDDEN_FIELD +
            r"\b(?:[\"']\s*\])?")
# any additive pairing of the total with a portion (or portion with portion):
# cost_saved + l1_cost_saved, cost_saved + cache_savings, l1 + cache
ADDITIVE_DOUBLE_COUNT = re.compile(_OPERAND + r"\s*\+\s*" + _OPERAND)

# The exact spellings this codebase writes — PM mutation report, all must fire.
_DOUBLE_COUNT_FORMS = [
    "ov.cost_saved + ov.l1_cost_saved",
    "p.cost_saved + p.l1_cost_saved",
    "s.cost_saved + s.cache_savings",
    'cost_saved+ov["l1_cost_saved"]',
    "cost_saved + l1_cost_saved",
    'money(ov.cache_savings + ov["l1_cost_saved"])',
]


@pytest.mark.parametrize("snippet", _DOUBLE_COUNT_FORMS,
                         ids=lambda s: s[:34])
def test_r2_static_catches_known_double_count_forms(snippet):
    """The PM's line-119 mutation (`ov.cost_saved + ov.l1_cost_saved`) slipped
    the original identifier-adjacent regex. Each real-world spelling must turn
    the static check red — the mutation proof is baked in, not hand-applied."""
    assert ADDITIVE_DOUBLE_COUNT.search(snippet), \
        f"static double-count check MISSED: {snippet}"


def test_r2_static_no_additive_pattern_in_source():
    """Static pin: no source line may add cost_saved to an l1/cache portion
    (belt-and-braces alongside the dynamic check). Also proven against a live
    mutation of the real source, not just synthetic snippets."""
    src = DASHBOARD_JS.read_text()
    assert not ADDITIVE_DOUBLE_COUNT.search(src), \
        "additive cost_saved/l1/cache pattern found in dashboard.js"
    # live mutation: the exact P0 the rule exists to prevent, injected into the
    # real file contents — the static check MUST fire on it
    mutated = src.replace("money(ov.cost_saved)",
                          "money(ov.cost_saved + ov.l1_cost_saved)", 1)
    assert mutated != src, "mutation anchor not found in dashboard.js"
    assert ADDITIVE_DOUBLE_COUNT.search(mutated), \
        "static check failed to catch the live double-count mutation"


# ------------------------------------------------------------------ R3: zero-guard

def test_r3_zero_guard_dash_on_passthrough_window(tmp_path):
    fx = _fixture(l1_tokens=0, l1_cost=0.0)
    out = _render(tmp_path, fx, "overview")
    html = out["html"]
    assert "No structural (L1) savings this window" in html
    assert "—</span> No structural (L1)" in html
    # zero-guard must NOT imply a $0.00 L1 value: no "portion of total" note,
    # no L1 money figure next to the dash
    assert "portion of total" not in html
    assert _js_money(0.0) + "</strong>" not in html


# ------------------------------------------------------- R4: charts carry no L1

def test_r4_no_chart_references_l1_fields(tmp_path):
    out = _render(tmp_path, _fixture(), "overview")
    assert out["charts"], "expected at least one Chart.js surface"
    for chart in out["charts"]:
        cfg = json.dumps(chart["cfg"])
        assert "l1" not in cfg.lower(), f"chart {chart['id']} references l1 fields"


# ------------------------------------------------------------------ R5: providers

def test_r5_providers_tab_shows_per_provider_l1(tmp_path):
    out = _render(tmp_path, _fixture(), "providers")
    html = out["html"]
    # openrouter (l1_tokens 330) and anthropic (80) both render L1 rows
    assert "330" in html and "80" in html
    assert _js_money(0.00042) in html and _js_money(0.0004) in html
    assert html.count("L1 structural") == 2
    # and no per-provider sum sneaks in (0.00145+0.00042=0.00187,
    # 0.0012+0.0004=0.0016)
    assert _js_money(0.00145 + 0.00042) not in html
    assert _js_money(0.0012 + 0.0004) not in html


def test_r5_providers_zero_guard(tmp_path):
    fx = _fixture()
    for p in fx["by_provider"]:
        p["l1_tokens_stripped"] = 0
        p["l1_cost_saved"] = 0.0
    out = _render(tmp_path, fx, "providers")
    html = out["html"]
    assert html.count("L1 structural") == 2  # dash lines, one per provider
    assert html.count('zero-dash">—') == 2
    assert "portion of cost saved" not in html


# ------------------------------------------------------------- routing sanity

def test_dashboard_reads_only_api_kpis(tmp_path):
    out = _render(tmp_path, _fixture(), "overview")
    assert out["url"].startswith("/api/kpis?"), out["url"]
