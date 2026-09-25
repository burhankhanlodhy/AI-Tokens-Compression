"""V2.2 dashboard render gates (docs/v2.2-ui-ux-design-system.md §11.2).

Executes the real proxy/static/dashboard.js in Node via the shared
dashboard_render_harness.js harness and pins the 15 named render gates:

  1  settings_runtime_env_default_sources
  2  settings_put_happy_path
  3  settings_put_401_preserves
  4  settings_put_400_reverts
  5  settings_revert_flow
  6  settings_semantic_locked
  7  settings_deployment_inventory_redaction
  8  settings_strategies_401_prompt
  9  settings_tripwire_states
  10 settings_independent_degradation
  11 settings_zero_traffic_reachable
  12 traffic_by_route
  13 providers_registry_and_native_cache
  14 hosted_mode_banner
  15 a11y_focus_and_motion

Skipped when Node is unavailable.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dashboard_render as base  # noqa: E402  (reuse fixture + harness)

ROOT = base.ROOT
pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")


def _settings_fixture(runtime, deploy=None, extra_routes=None):
    routes = {
        "/api/settings": {"body": {"settings": list(runtime) + list(deploy or [])}},
        "/api/strategies": {"body": {"strategies": [
            {"strategy": "deferred_tools", "version": "v2.1", "flag_enabled": True,
             "default_off": True, "fallback": "existing_behavior"},
            {"strategy": "atba", "version": "v2.1", "flag_enabled": False,
             "default_off": True, "fallback": "tool_only",
             "enforcement_enabled": True},
            {"strategy": "l1", "version": "v2.0", "flag_enabled": False,
             "default_off": False, "fallback": "existing_behavior"},
        ]}},
        "/api/tripwire": {"body": {"status": "green",
            "dose_drift": {"status": "clear", "flagged": []},
            "missed_grounding": {"status": "alert", "flagged": [{"id": 1}, {"id": 2}]},
            "rows_scanned": 42, "calibration_artifact": None}},
        "/api/tenants": {"body": [{"name": "default", "plan": "self_host",
                                   "created_at": "2026-09-01"}]},
        "/api/kpis": {"body": base._fixture()},
    }
    if extra_routes:
        routes.update(extra_routes)
    return {"_routes": routes}


def _run(tmp_path: Path, fixture: dict, script=None, mode="", tab="settings"):
    if script is not None:
        fixture = dict(fixture)
        fixture["_script"] = script
    fx = tmp_path / f"fixture_v22_{tab}_{abs(hash(json.dumps(fixture, sort_keys=True)))}.json"
    fx.write_text(json.dumps(fixture))
    args = ["node", str(ROOT / "test" / "dashboard_render_harness.js"), str(fx), tab]
    if mode:
        args.append(mode)
    out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    d = json.loads(out.stdout.strip())
    assert d["script_error"] is None, d["script_error"]
    return d


def _settings_items():
    return [
        {"name": "l1_enabled", "value": True, "source": "runtime",
         "category": "runtime_configurable", "updated_at": "2026-09-25T10:00:00Z"},
        {"name": "tool_schema_minify", "value": False, "source": "env",
         "category": "runtime_configurable"},
        {"name": "semantic_cache_enabled", "value": False, "source": "default",
         "category": "runtime_configurable",
         "locked_reason": "Semantic cache stays locked until the AC-PC4/PC5 calibration gate is green: no ratified cosine threshold is configured for this deployment."},
    ]


def _deploy_items():
    return [
        {"name": "admin_token", "value": "••••", "source": "env",
         "category": "deployment_only"},
        {"name": "compression_enabled", "value": True, "source": "env",
         "category": "deployment_only"},
        {"name": "provider_base_urls",
         "value": {"openrouter": "https://openrouter.ai/api/v1"},
         "source": "default", "category": "deployment_only"},
        {"name": "tripwire_deep_cut_pct", "value": 25.0, "source": "default",
         "category": "deployment_only"},
    ]


# ---------------------------------------------------------------- gate 1
def test_settings_runtime_env_default_sources(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    out = _run(tmp_path, fx)
    html = out["html"]
    assert html.count('class="chip source-runtime"') == 1
    assert html.count('class="chip source-env"') == 1
    assert html.count('class="chip source-default"') == 1
    assert "overridden 2026-09-25" in html          # runtime item's updated_at
    env_row = html.split("Tool-schema minification", 1)[1].split("setting-row", 1)[0]
    assert "overridden" not in env_row


# ---------------------------------------------------------------- gate 2
def test_settings_put_happy_path(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    fx["_routes"]["PUT /api/settings/l1_enabled*"] = {"body": {
        "name": "l1_enabled", "value": False, "source": "runtime",
        "category": "runtime_configurable", "updated_at": "2026-09-25T11:00:00Z"}}
    out = _run(tmp_path, fx, script=[
        {"click": "setsw-l1_enabled"},
        {"wait": 40},
    ])
    html = out["html"]
    writes = out["writes"]
    assert len(writes) == 1 and writes[0]["method"] == "PUT"
    assert writes[0]["url"] == "/api/settings/l1_enabled"
    l1 = html.split("L1 structural cleanup", 1)[1].split("setting-row", 1)[0]
    assert "badge neutral\">off<" in l1
    assert 'class="chip source-runtime">runtime<' in l1
    assert "Saved — effective immediately" in l1


# ---------------------------------------------------------------- gate 3
def test_settings_put_401_preserves(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    fx["_routes"]["PUT /api/settings/l1_enabled*"] = [
        {"status": 401, "body": {"detail": "unauthorized"}},
        {"body": {"name": "l1_enabled", "value": True, "source": "runtime",
                  "category": "runtime_configurable"}},
    ]
    out = _run(tmp_path, fx, script=[
        {"click": "setsw-l1_enabled"},
        {"wait": 40},
        {"set": ["token-input", "tok_admin1"]},
        {"click": "token-save"},
        {"wait": 40},
    ])
    writes = out["writes"]
    assert [w["method"] for w in writes] == ["PUT", "PUT"]
    assert "Authorization" not in writes[0]["headers"]
    assert writes[1]["headers"].get("Authorization") == "Bearer tok_admin1"
    assert "tok_admin1" not in out["html"]
    assert "Saved — effective immediately" in out["html"]


# ---------------------------------------------------------------- gate 4
def test_settings_put_400_reverts(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    fx["_routes"]["PUT /api/settings/l1_enabled*"] = {
        "status": 400,
        "body": {"detail": "name: l1_enabled is deployment-only; set it via environment at boot."}}
    out = _run(tmp_path, fx, script=[{"click": "setsw-l1_enabled"}, {"wait": 40}])
    html = out["html"]
    # switch reverted to the prior value (the 400 body did not carry a new
    # value) — the input stays `checked` and the chip stays `runtime`.
    assert 'setsw-l1_enabled" checked' in html
    assert 'class="chip source-runtime">runtime<' in html
    assert out["texts"].get("seterr-l1_enabled") == \
        "name: l1_enabled is deployment-only; set it via environment at boot."


# ---------------------------------------------------------------- gate 5
def test_settings_revert_flow(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    fx["_routes"]["DELETE /api/settings/l1_enabled*"] = {"body": {
        "name": "l1_enabled", "value": False, "source": "env",
        "category": "runtime_configurable"}}
    out = _run(tmp_path, fx, script=[
        {"click": "setrevert-l1_enabled"},
        {"wait": 20},
        {"click": "setrevert-l1_enabled"},
        {"wait": 40},
    ])
    writes = out["writes"]
    assert len(writes) == 1 and writes[0]["method"] == "DELETE"
    assert writes[0]["url"] == "/api/settings/l1_enabled"
    html = out["html"]
    l1 = html.split("L1 structural cleanup", 1)[1].split("setting-row", 1)[0]
    assert 'class="chip source-env">env<' in l1
    assert ">Revert</button>" not in l1
    out2 = _run(tmp_path, fx, script=[
        {"click": "setrevert-l1_enabled"}, {"wait": 20},
        {"click": "setrevertcancel-l1_enabled"}, {"wait": 20},
    ])
    assert out2["writes"] == []
    # the confirm state cleared — the plain Revert button is back
    assert "Confirm revert?" not in out2["html"]
    assert ">Revert</button>" in out2["html"]


# ---------------------------------------------------------------- gate 6
def test_settings_semantic_locked(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    out = _run(tmp_path, fx)
    html = out["html"]
    sem = html.split("Semantic cache", 1)[1].split("setting-row", 1)[0]
    assert 'setsw-semantic_cache_enabled" disabled' in html
    assert '<span class="badge neutral">locked</span>' in sem
    assert ("Semantic cache stays locked until the AC-PC4/PC5 calibration gate is green: "
            "no ratified cosine threshold is configured for this deployment.") in sem
    assert "Gate status: see Pipeline health below." in sem


# ---------------------------------------------------------------- gate 7
def test_settings_deployment_inventory_redaction(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    out = _run(tmp_path, fx)
    html = out["html"]
    deploy = html.split("Deployment configuration (read-only)", 1)[1].split("Pipeline health", 1)[0]
    assert "••••" in deploy
    assert "managed at boot" in deploy
    assert "supersecretvalue" not in html
    assert '<span class="badge neutral onoff">on</span>' in deploy
    assert "&quot;openrouter&quot;:&quot;https://openrouter.ai/api/v1&quot;" in deploy
    assert ">25<" in deploy


# ---------------------------------------------------------------- gate 8
def test_settings_strategies_401_prompt(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    fx["_routes"]["/api/strategies"] = [
        {"status": 401, "body": {"detail": "unauthorized"}},
        {"body": {"strategies": [
            {"strategy": "deferred_tools", "version": "v2.1", "flag_enabled": True,
             "default_off": True, "fallback": "existing_behavior"}]}},
    ]
    out = _run(tmp_path, fx, script=[
        {"set": ["token-input", "tok_admin2"]},
        {"click": "token-save"},
        {"wait": 40},
    ])
    html = out["html"]
    # gate 8: the 401 path — card-local prompt rendered BEFORE token entry.
    pre = _run(tmp_path, fx)
    assert "Enter the admin token to view strategy flags." in pre["html"]
    assert "V2.1 strategy lanes (deferred_tools, tocp, idcp, atba, mtcc)" in pre["html"]
    assert "live traffic is unaffected" in pre["html"]
    # after saving the token the card re-fetches and renders the table
    assert "Admin token entered for this session." in html
    assert "V2.1 strategy lanes (deferred_tools, tocp, idcp, atba, mtcc)" in html
    assert "live traffic is unaffected" in html
    strat = html.split("Strategy flags", 1)[1].split("Tripwire", 1)[0]
    assert "deferred_tools" in strat and "existing_behavior" in strat
    assert '<span class="badge green">on</span>' in strat
    strat_urls = [u for u in out["urls"] if u == "/api/strategies"]
    assert len(strat_urls) == 2


# ---------------------------------------------------------------- gate 9
def test_settings_tripwire_states(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    out = _run(tmp_path, fx)
    tw = out["html"].split("Tripwire", 1)[1]
    assert '<span class="badge green">green</span>' in tw
    assert '<span class="badge green">clear</span>' in tw
    assert '<span class="badge red">alert</span>' in tw
    assert "2 flagged rows" in tw
    assert "42 rows scanned" in tw

    fx2 = _settings_fixture(_settings_items(), _deploy_items())
    fx2["_routes"]["/api/tripwire"] = {"body": {"status": "pending",
        "dose_drift": {"status": "pending_calibration", "flagged": [{}]},
        "missed_grounding": {"status": "insufficient_live_rows", "flagged": []},
        "rows_scanned": 3, "calibration_artifact": "cal-2026-09-01.json"}}
    out2 = _run(tmp_path, fx2)
    tw2 = out2["html"].split("Tripwire", 1)[1]
    assert '<span class="badge gold">pending</span>' in tw2
    assert '<span class="badge gold">pending_calibration</span>' in tw2
    assert '<span class="badge gold">insufficient_live_rows</span>' in tw2
    assert 'calibration: <span class="mono">cal-2026-09-01.json</span>' in tw2

    fx3 = _settings_fixture(_settings_items(), _deploy_items())
    fx3["_routes"]["/api/tripwire"] = {"body": {"status": "green",
        "dose_drift": {"status": "not_applicable_grounded_off", "flagged": []},
        "missed_grounding": {"status": "pending_metric_ruling", "flagged": []},
        "rows_scanned": 0, "calibration_artifact": None}}
    out3 = _run(tmp_path, fx3)
    tw3 = out3["html"].split("Tripwire", 1)[1]
    assert '<span class="badge neutral">not_applicable_grounded_off</span>' in tw3
    assert '<span class="badge gold">pending_metric_ruling</span>' in tw3


# ---------------------------------------------------------------- gate 10
def test_settings_independent_degradation(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    out = _run(tmp_path, fx, mode="fail-route:/api/tripwire")
    html = out["html"]
    tw = html.split("Tripwire", 1)[1]
    assert "Couldn't load tripwire status." in tw
    assert 'id="tripwire-retry"' in tw
    assert "Couldn't load strategy flags." not in html
    assert "L1 structural cleanup" in html

    out2 = _run(tmp_path, fx, mode="fail-route:/api/strategies")
    strat = out2["html"].split("Strategy flags", 1)[1].split("Tripwire", 1)[0]
    assert "Couldn't load strategy flags." in strat
    assert 'id="strategies-retry"' in strat
    assert "Couldn't load tripwire status." not in out2["html"]


# ---------------------------------------------------------------- gate 11
def test_settings_zero_traffic_reachable(tmp_path):
    fx = _settings_fixture(_settings_items(), _deploy_items())
    fx["_routes"]["/api/kpis"] = {"body": {"overview": {"requests": 0}, "series": []}}
    out = _run(tmp_path, fx)
    html = out["html"]
    assert "No traffic yet" not in html
    assert "Runtime controls" in html
    assert "Pipeline health" in html
    assert all(u != "/api/kpis?bucket=day" for u in out["urls"])


# ---------------------------------------------------------------- gate 12
def test_traffic_by_route(tmp_path):
    fx = {"_routes": {"/api/kpis": {"body": dict(base._fixture(), by_route=[
        {"route": "compress", "requests": 5, "cost_saved": 0.002},
        {"route": "passthrough", "requests": 3, "cost_saved": 0.0001}])}}}
    out = _run(tmp_path, fx, tab="traffic")
    html = out["html"]
    assert "Route mix" in html
    assert "compress" in html and "passthrough" in html
    assert ">5<" in html and ">3<" in html
    out2 = _run(tmp_path, {"_routes": {"/api/kpis": {"body": base._fixture()}}},
                tab="traffic")
    assert "Route mix" not in out2["html"]
    out3 = _run(tmp_path, {"_routes": {"/api/kpis": {"body": dict(base._fixture(), by_route=[
        {"route": "passthrough", "requests": 2, "cost_saved": 0.0}])}}},
                tab="traffic")
    assert "Route mix" in out3["html"]
    assert "compress" not in out3["html"].split("Route mix", 1)[1]


# ---------------------------------------------------------------- gate 13
def test_providers_registry_and_native_cache(tmp_path):
    kpis = base._fixture()
    kpis["by_provider"][0]["provider_cache_read_tokens"] = 512
    kpis["by_provider"][0]["provider_cache_write_tokens"] = 128
    fx = {"_routes": {
        "/api/providers": {"body": {"providers": [
            {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1",
             "adapter_class": "openai_compat", "enabled": True},
            {"name": "anthropic", "base_url": "https://api.anthropic.com",
             "adapter_class": "anthropic_messages", "enabled": False},
        ]}},
        "/api/kpis": {"body": kpis},
    }}
    out = _run(tmp_path, fx, tab="providers")
    html = out["html"]
    assert "https://openrouter.ai/api/v1" in html
    assert '<span class="chip">openai_compat</span>' in html
    assert '<span class="badge green">enabled</span>' in html
    assert "provider-native cache — measured evidence, never merged into savings" in html
    assert "512" in html and "128" in html
    anth = html.split("anthropic", 1)[1].split("</div>", 1)[0]
    assert "Provider-native cache" not in anth
    assert '<span class="badge neutral">disabled</span>' in html
    assert "upstream-key" not in html


# ---------------------------------------------------------------- gate 14
def test_hosted_mode_banner(tmp_path):
    hosted_tenant = [{"name": "default", "plan": "hosted", "created_at": "2026-09-01"}]
    fx = _settings_fixture(_settings_items(), _deploy_items(),
                           extra_routes={"/api/tenants": {"body": hosted_tenant}})
    out = _run(tmp_path, fx)
    html = out["html"]
    assert ("Management and KPI reads are unauthenticated (self-host threat "
            "model). This deployment has a non-self-host tenant plan") in html
    fx2 = _settings_fixture(_settings_items(), _deploy_items())
    out2 = _run(tmp_path, fx2)
    assert "non-self-host tenant plan" not in out2["html"]


# ---------------------------------------------------------------- gate 15
def test_a11y_focus_and_motion():
    shell = (ROOT / "proxy" / "dashboard_v2.py").read_text()
    assert ":focus-visible" in shell
    assert "outline: 2px solid var(--accent)" in shell
    assert "outline: none" not in shell and "outline:none" not in shell
    js = (ROOT / "proxy" / "static" / "dashboard.js").read_text()
    assert "focusContent(content)" in js
    assert "el.tabIndex = -1" in js
    assert "prefers-reduced-motion" in shell
    assert "animation: none" in shell
    assert 'tabindex="0" role="button"' in js
    assert 'type="checkbox" role="switch"' in js


def test_shell_has_settings_nav_anchor():
    shell = (ROOT / "proxy" / "dashboard_v2.py").read_text()
    assert 'data-tab="settings"' in shell
    assert 'id="bucket-slot"' in shell
