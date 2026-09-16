"""AC-A8 dashboard gate (B-25) — implements §9 of dashboard-ac-a8-spec.md.

Executes the real proxy/static/dashboard.js in Node via the shared
dashboard_render_harness.js harness and asserts the eleven-item AC-A8 matrix:

  1. Four tabs render distinct, complete surfaces from one fixture.
  2. 1:1 fidelity — every displayed money numeral reproduces a fixture field
     byte-exact under the JS formatters; no fixture value is arithmetically
     transformed into a displayed figure (permitted: formatting, the delta,
     bar-width geometry).
  3. No client-side aggregation — static scan of dashboard.js (no .reduce,
     no accumulate-over-fields, no additive money pattern; extends R2).
  4. Decomposition invariant — headline = cost_saved ALONE; no
     cost_saved+l1_cost_saved / +cache_savings sum on any tab (extends R2).
  5. Zero-guards — l1_tokens_stripped=0 → dash + guidance on overview and
     every provider row.
  6. No chart references l1_* fields (R4, extended to every tab).
  7. Donut honesty (F1) — c-models dataset equals exactly ONE by_model field,
     pinned to cost_saved per D1; dataset and title agree.
  8. Latency (F2) — latency tiles render only p50/p95/p99/avg_latency_ms;
     no per-bucket latency chart (chart ids exclude the removed c-lat).
  9. Single source — the only fetch is /api/kpis?…
  10. States — requests=0 → empty panel; fetch failure → error + Retry refires
      (harness-injected).
  11. Redaction — no secret-shaped strings in any rendered tab.

R1–R5 (test_dashboard_render.py) remain the baseline; this gate supersets them.
Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dashboard_render as base  # noqa: E402  (reuse fixture, harness, regex)

ROOT = base.ROOT
DASHBOARD_JS = base.DASHBOARD_JS
_fixture = base._fixture
_render = base._render
_js_money = base._js_money
ADDITIVE_DOUBLE_COUNT = base.ADDITIVE_DOUBLE_COUNT

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")

TABS = ["overview", "traffic", "providers", "keys"]

# fields no tile may arithmetically accumulate (money + token fields)
FIELD_RE = re.compile(
    r"(cost_saved|l1_cost_saved|cache_savings|tokens_saved|"
    r"input_tokens_saved|l1_tokens_stripped|requests|errors)\b"
)
ACCUMULATE_LINE = re.compile(r"\+=|\+\+")
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}|sk-ant-[A-Za-z0-9_\-]+")


def _money_strings(html: str) -> list[str]:
    """All displayed $x.xxxx money numerals (money() always emits toFixed(4))."""
    return re.findall(r"\$\d+\.\d{4}", html)


def _allowed_money(fixture: dict) -> set[str]:
    """Every money string a 1:1 renderer may legitimately display."""
    ov = fixture["overview"]
    allowed = {ov[f] for f in
               ("cost_before", "cost_after", "cost_saved", "cache_savings",
                "l1_cost_saved")}
    allowed |= {s[f] for s in fixture["series"]
                for f in ("cost_saved", "cache_savings", "l1_cost_saved")}
    allowed |= {m["cost_saved"] for m in fixture["by_model"]}
    allowed |= {p[f] for p in fixture["by_provider"]
                for f in ("cost_saved", "l1_cost_saved")}
    allowed = {_js_money(v) for v in allowed}
    # the ONE permitted client arithmetic: headline delta = series[last] −
    # series[prev] (absolute value, formatted)
    s = fixture["series"]
    allowed.add(_js_money(abs(s[-1]["cost_saved"] - s[-2]["cost_saved"])))
    return allowed


# ------------------------------------------------- 1: four tabs, one fixture

@pytest.mark.parametrize("tab", TABS)
def test_m1_tab_renders_expected_surface(tmp_path, tab):
    out = _render(tmp_path, _fixture(), tab)
    assert out["html"].strip(), f"{tab} rendered an empty surface"
    assert out["url"].startswith("/api/kpis?"), out["url"]


def test_m1_four_tabs_are_distinct_and_complete(tmp_path):
    htmls = {}
    for tab in TABS:
        out = _render(tmp_path, _fixture(), tab)
        htmls[tab] = out["html"]
        assert len(out["html"]) > 120, f"{tab} surface incomplete"
    assert len(set(htmls.values())) == 4, "two tabs rendered the same surface"
    assert "Est. cost saved" in htmls["overview"]
    assert "Cost saved per model" in htmls["overview"]      # F1 retitled donut
    assert "Recent buckets" in htmls["overview"]
    assert "Latency p50" in htmls["traffic"] and "Latency p99" in htmls["traffic"]
    assert "Requests per bucket" in htmls["traffic"]
    assert "openrouter" in htmls["providers"] and "anthropic" in htmls["providers"]
    assert "Keys" in htmls["keys"] and "Phase C" in htmls["keys"]
    assert "last-4 only" in htmls["keys"]


# ------------------------------------------------- 2: 1:1 money fidelity

def test_m2_every_displayed_money_value_reproduces_a_fixture_field(tmp_path):
    fx = _fixture()
    allowed = _allowed_money(fx)
    # overview/providers display money; traffic legitimately displays none
    # (its tiles are ms counts, rates, and request/error counts)
    for tab, expect_money in (("overview", True), ("providers", True),
                              ("traffic", False)):
        html = _render(tmp_path, fx, tab)["html"]
        displayed = _money_strings(html)
        if expect_money:
            assert displayed, f"{tab} renders no money numerals"
        for m in displayed:
            assert m in allowed, \
                f"{tab} displays {m} which is NOT a verbatim fixture field"


def test_m2_headline_and_delta_are_exact_fixture_math(tmp_path):
    fx = _fixture()
    out = _render(tmp_path, fx, "overview")
    html = out["html"]
    s = fx["series"]
    assert _js_money(fx["overview"]["cost_saved"]) in html      # headline ALONE
    delta = _js_money(abs(s[-1]["cost_saved"] - s[-2]["cost_saved"]))
    assert delta in html and "vs prior bucket" in html
    assert "▲" in html or "▼" in html


# ------------------------------------------------- 3: no client-side aggregation

def _aggregation_scan(src: str) -> str | None:
    """Return a description of the first aggregation violation, else None."""
    if ".reduce(" in src:
        return "uses .reduce("
    for i, line in enumerate(src.splitlines(), 1):
        if ACCUMULATE_LINE.search(line) and FIELD_RE.search(line):
            return f"line {i} accumulates a contract field: {line.strip()}"
    m = ADDITIVE_DOUBLE_COUNT.search(src)
    if m:
        return f"additive money pattern: {m.group(0)}"
    return None


def test_m3_static_no_aggregation_in_source():
    src = DASHBOARD_JS.read_text()
    assert _aggregation_scan(src) is None, _aggregation_scan(src)


@pytest.mark.parametrize("snippet", [
    "d.series.reduce(function (a, s) { return a + s.cost_saved; }, 0)",
    "d.by_model.reduce(function (a, m) { return a + m.cost_saved; }, 0)",
    "d.by_provider.reduce(function (a, p) { return a + p.tokens_saved; }, 0)",
    "var total = 0; d.series.forEach(function (s) { total += s.cost_saved; });",
    "ov.cost_saved + ov.l1_cost_saved",
    "p.cost_saved + p.l1_cost_saved",
    "s.cost_saved + s.cache_savings",
])
def test_m3_static_scan_catches_aggregation_mutations(snippet):
    """The scan must fire on the aggregation forms the rule exists to prevent —
    proven against a live mutation of the real source, not just synthetic."""
    src = DASHBOARD_JS.read_text()
    mutated = src + "\n//" + snippet
    assert _aggregation_scan(mutated) is not None, \
        f"aggregation scan MISSED: {snippet}"


# ------------------------------------------------- 4: decomposition invariant

def test_m4_headline_cost_saved_alone_no_addends_anywhere(tmp_path):
    fx = _fixture()
    # every (total, portion) pair the contract could be tempted to sum
    forbidden = {
        _js_money(fx["overview"]["cost_saved"] + fx["overview"]["l1_cost_saved"]),
        _js_money(fx["overview"]["cost_saved"] + fx["overview"]["cache_savings"]),
        _js_money(fx["overview"]["l1_cost_saved"] + fx["overview"]["cache_savings"]),
    }
    for s in fx["series"]:
        if s["l1_cost_saved"] > 0:  # a zero portion is indistinguishable
            forbidden.add(_js_money(s["cost_saved"] + s["l1_cost_saved"]))
        if s["cache_savings"] > 0:
            forbidden.add(_js_money(s["cost_saved"] + s["cache_savings"]))
    for p in fx["by_provider"]:
        if p["l1_cost_saved"] > 0:
            forbidden.add(_js_money(p["cost_saved"] + p["l1_cost_saved"]))
    # a summed value that coincides with a LEGAL verbatim field elsewhere in
    # the fixture can't be proven by string inspection — those are covered by
    # the static scan (m3) instead of double-charging the renderer
    forbidden -= _allowed_money(fx)
    for tab in TABS:
        html = _render(tmp_path, fx, tab)["html"]
        for bad in forbidden:
            assert bad not in html, f"{tab} rendered a summed total {bad}"

# ------------------------------------------------- 5: zero-guards

def test_m5_zero_guard_overview_and_every_provider_row(tmp_path):
    fx = _fixture(l1_tokens=0, l1_cost=0.0)
    for p in fx["by_provider"]:
        p["l1_tokens_stripped"] = 0
        p["l1_cost_saved"] = 0.0
    ov_html = _render(tmp_path, fx, "overview")["html"]
    assert "No structural (L1) savings this window" in ov_html
    assert "portion of total" not in ov_html
    prov_html = _render(tmp_path, fx, "providers")["html"]
    assert prov_html.count('zero-dash">—') == len(fx["by_provider"])
    assert "portion of cost saved" not in prov_html
    assert _js_money(0.0) + "</strong>" not in prov_html


# ------------------------------------------------- 6: charts carry no l1

@pytest.mark.parametrize("tab", TABS)
def test_m6_no_chart_references_l1_fields(tmp_path, tab):
    out = _render(tmp_path, _fixture(), tab)
    for chart in out["charts"]:
        cfg = json.dumps(chart["cfg"])
        assert "l1" not in cfg.lower(), \
            f"chart {chart['id']} on {tab} references l1 fields"


# ------------------------------------------------- 7: donut honesty (F1)

def test_m7_donut_plots_exactly_one_by_model_field_pinned_to_cost_saved(tmp_path):
    fx = _fixture()
    out = _render(tmp_path, fx, "overview")
    html, charts = out["html"], out["charts"]
    donuts = [c for c in charts if c["cfg"]["type"] == "doughnut"]
    assert len(donuts) == 1, "expected exactly one donut (c-models)"
    donut = donuts[0]
    assert donut["id"] == "c-models"
    labels = donut["cfg"]["data"]["labels"]
    data = donut["cfg"]["data"]["datasets"][0]["data"]
    assert labels == [m["model"] for m in fx["by_model"]]
    # D1 ratified: pinned to cost_saved — dataset equals exactly ONE field
    assert data == [m["cost_saved"] for m in fx["by_model"]]
    assert data != [m["requests"] for m in fx["by_model"]], \
        "donut plots requests while titled as cost"
    # title and dataset agree; the old lying title is gone
    assert "Cost saved per model" in html
    assert "Spend per model" not in html
    assert "cost before" not in html


# ------------------------------------------------- 8: latency (F2)

def test_m8_no_per_bucket_latency_chart_percentiles_are_kpi_cards(tmp_path):
    fx = _fixture()
    out = _render(tmp_path, fx, "traffic")
    html, charts = out["html"], out["charts"]
    ids = [c["id"] for c in charts]
    assert "c-lat" not in ids, "fabricated per-bucket latency chart still present"
    assert set(ids) <= {"c-req", "c-err"}, f"unexpected charts on traffic: {ids}"
    # latency tiles render ONLY the window-global fields, as KPI cards
    assert "Latency p50" in html and "Latency p95" in html and "Latency p99" in html
    assert _js_numbers(fx["latency"]["p50"]) in html
    assert _js_numbers(fx["latency"]["p95"]) in html
    assert _js_numbers(fx["latency"]["p99"]) in html
    # no latency line dataset anywhere: line charts carry series fields only
    for c in charts:
        assert "p95" not in json.dumps(c["cfg"]).lower()


def _js_numbers(x: float) -> str:
    """fmt()'s output for x via the real formatter (locale-aware grouping)."""
    return base._node(["-e",
                       f"console.log(Number({x!r}).toLocaleString("
                       "undefined, { maximumFractionDigits: 4 }))"])


# ------------------------------------------------- 9: single source

def test_m9_only_fetch_is_api_kpis(tmp_path):
    fx = _fixture()
    for tab in TABS:
        out = _render(tmp_path, fx, tab)
        assert out["url"] == "/api/kpis?bucket=day", out["url"]
        assert len(out["urls"]) == 1, f"{tab} issued {len(out['urls'])} fetches"


# ------------------------------------------------- 10: states

def test_m10_zero_requests_renders_empty_state_no_charts(tmp_path):
    fx = _fixture()
    fx["overview"]["requests"] = 0
    for tab in ("overview", "traffic", "providers"):
        out = _render(tmp_path, fx, tab)
        assert "No traffic yet" in out["html"]
        assert "Send a prompt through the proxy" in out["html"]
        assert out["charts"] == [], f"{tab} padded zeros under empty state"


def test_m10_fetch_failure_shows_error_and_retry_refires(tmp_path):
    out = _render_fail(tmp_path, _fixture(), "overview")
    assert "Couldn't load KPIs" in out["html"]
    assert "ledger may be unavailable" in out["html"]
    assert "Retry" in out["html"]
    # harness clicked Retry: the same request re-fired exactly once
    assert out["urls"] == ["/api/kpis?bucket=day"] * 2, out["urls"]


def _render_fail(tmp_path: Path, fixture: dict, tab: str) -> dict:
    fx = tmp_path / f"fixture_fail_{tab}.json"
    fx.write_text(json.dumps(fixture))
    out = base._node([str(base.HARNESS), str(fx), tab, "fail-fetch"])
    return json.loads(out)


# ------------------------------------------------- 11: redaction

@pytest.mark.parametrize("tab", TABS)
def test_m11_no_secret_material_in_rendered_html(tmp_path, tab):
    out = _render(tmp_path, _fixture(), tab)
    assert not SECRET_RE.search(out["html"]), \
        f"{tab} rendered secret-shaped key material"
