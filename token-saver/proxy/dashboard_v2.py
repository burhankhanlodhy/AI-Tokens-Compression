"""V2.2: five-tab dashboard (Overview / Traffic / Providers / Keys / Settings).

Server-rendered shell + Chart.js (CDN) + one dashboard.js. Every card and
chart reads ONLY from the documented APIs (no client-side aggregation —
AC-A8). Layout per the V2.2 design system (docs/v2.2-ui-ux-design-system.md):
12-col grid, one component set across tabs, skeletons/error/empty states shared;
responsive = scrollable top tab strip (§4.3, no hamburger/bottom bar).
"""
from __future__ import annotations

SHELL = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>token-saver dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root {
  --bg: #0f1115; --panel: #171a21; --panel-raised: #1d222c; --border: #262b36;
  --text: #e6e9ef; --muted: #8b93a3; --accent: #4f8cff; --green: #35c28f;
  --red: #e5484d; --gold: #d9a53f; --violet: #9b7bff; --cyan: #5ac8fa;
  --overlay: rgba(0,0,0,.55);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 system-ui, sans-serif; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
nav { display: flex; align-items: center; gap: 1.5rem; padding: .8rem 1.5rem;
      border-bottom: 1px solid var(--border); background: var(--panel);
      flex-wrap: nowrap; overflow-x: auto; }
nav .brand { font-weight: 700; white-space: nowrap; }
nav .brand-short { display: none; font-weight: 700; white-space: nowrap; }
nav .spacer { flex: 1; }
nav a { color: var(--muted); text-decoration: none; padding: .35rem .8rem;
        border-radius: 6px; white-space: nowrap; min-height: 32px; }
nav a.active, nav a:hover { color: var(--text); background: var(--border); }
nav select { background: var(--panel); color: var(--text);
             border: 1px solid var(--border); border-radius: 6px;
             padding: .3rem; white-space: nowrap; }
main { padding: 1.5rem; max-width: 1200px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 10px; padding: 1rem; }
.card h3 { margin: 0 0 .5rem; font-size: .8rem; font-weight: 600;
           color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.section-title { font-size: 1.25rem; font-weight: 600; margin: 1.5rem 0 .25rem; }
.section-note { color: var(--muted); font-size: .8rem; margin: 0 0 .75rem; }
.span3 { grid-column: span 3; } .span6 { grid-column: span 6; }
.span12 { grid-column: span 12; }
.kpi-num { font-size: 1.9rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.kpi-delta { font-size: .8rem; color: var(--muted); }
.kpi-delta.up { color: var(--green); } .kpi-delta.down { color: var(--red); }
.chart-wrap { position: relative; height: 260px; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-size: .75rem; text-transform: uppercase; }
tr.click-row { cursor: pointer; }
tr.click-row:hover { background: var(--panel-raised); }
.skel { display: inline-block; border-radius: 6px; height: 1.6rem; min-width: 5rem;
        background: linear-gradient(90deg, #1d222c 25%, #262b36 50%, #1d222c 75%);
        background-size: 200% 100%; animation: shimmer 1.2s infinite; }
.skel-row { height: 2.4rem; margin: .6rem 0; border-radius: 8px;
            background: linear-gradient(90deg, #1d222c 25%, #262b36 50%, #1d222c 75%);
            background-size: 200% 100%; animation: shimmer 1.2s infinite; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) {
  .skel, .skel-row { animation: none; }
}
.error-box { border: 1px solid var(--red); border-radius: 10px; padding: 1rem;
             background: rgba(229,72,77,.08); }
.error-box button { margin-top: .5rem; background: var(--accent); color: white;
                    border: none; padding: .4rem .9rem; border-radius: 6px; cursor: pointer; }
.empty { text-align: center; padding: 3rem 1rem; color: var(--muted); }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px;
         font-size: .75rem; }
.badge.red { background: rgba(229,72,77,.15); color: var(--red); }
.badge.green { background: rgba(53,194,143,.15); color: var(--green); }
.badge.neutral { background: rgba(139,147,163,.15); color: var(--muted); }
/* AC-PC-UI §4.3: threshold-miss pressure badge (gold) and the v1.1 semantic
   tint — badges keep raw ledger status text (no renaming layer). */
.badge.gold { background: rgba(217,165,63,.15); color: var(--gold); }
/* Keys & Tenants tab (C6, keys-tenants-tab-spec.md) — shared component set. */
.chip { display: inline-block; padding: .05rem .45rem; margin: 0 .15rem .15rem 0;
        border: 1px solid var(--border); border-radius: 999px;
        font-size: .72rem; color: var(--muted); }
/* V2.2 source chips (design-system §3.6 / AC-D3): runtime is accent-outlined. */
.chip.source-runtime { border-color: var(--accent); color: var(--accent); }
.chip.source-env, .chip.source-default { border-color: var(--border); color: var(--muted); }
.btn { background: var(--accent); color: #fff; border: none;
       padding: .3rem .7rem; border-radius: 6px; cursor: pointer; font-size: .8rem;
       min-height: 32px; }
.btn.danger { background: var(--red); }
.btn.ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
.btn:disabled { opacity: .5; cursor: default; }
.hdr-action { float: right; }
.key-dot { color: var(--muted); letter-spacing: .15em; }
.form-row { margin: .5rem 0; }
.form-label { display: block; color: var(--muted); font-size: .75rem;
              text-transform: uppercase; margin-bottom: .2rem; }
.form-input { width: 100%; box-sizing: border-box; background: var(--panel);
              color: var(--text); border: 1px solid var(--border);
              border-radius: 6px; padding: .4rem .5rem; }
.form-hint { color: var(--muted); font-size: .75rem; }
.inline-err { color: var(--red); font-size: .78rem; }
.reveal-code { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
               background: #0d1117; padding: .5rem; border-radius: 6px;
               word-break: break-all; margin: .4rem 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
/* Savings breakdown panel (a1eae90 decomposition contract): L1 and cache
   render as contained sub-tiles of the cost_saved total — never summed. */
.breakdown-note { color: var(--muted); font-size: .8rem; margin: 0 0 .6rem; }
.subtile { border: 1px solid var(--border); border-radius: 8px;
           padding: .6rem .8rem; margin-top: .6rem; }
.subtile.l1-zero { color: var(--muted); }
.subtile .lead { color: var(--muted); font-size: .72rem; font-weight: 600;
                 text-transform: uppercase; letter-spacing: .04em; margin-bottom: .35rem; }
.subrow { display: flex; align-items: baseline; gap: .5rem; padding: .1rem 0; }
.subrow span:first-child { color: var(--muted); flex: 0 0 9rem; }
.muted-note { color: var(--muted); font-size: .75rem; }
.zero-dash { font-weight: 700; }
.bar-total { position: relative; height: 10px; border-radius: 999px;
             background: var(--accent); overflow: hidden; margin-top: .55rem; }
.bar-l1 { position: absolute; left: 0; top: 0; bottom: 0;
          background: var(--gold); }
/* V2.2 Settings surface (design-system §3/§6.5). */
.switch { position: relative; display: inline-block; width: 36px; height: 20px;
          flex: 0 0 auto; }
.switch input { position: absolute; opacity: 0; inset: 0; margin: 0;
                width: 100%; height: 100%; cursor: pointer; }
.switch .track { position: absolute; inset: 0; border-radius: 999px;
                 border: 1px solid var(--border); background: var(--border);
                 transition: background .15s; pointer-events: none; }
.switch .thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px;
                 border-radius: 999px; background: var(--muted);
                 transition: left .15s, background .15s; pointer-events: none; }
.switch input:checked ~ .track { background: var(--accent); border-color: var(--accent); }
.switch input:checked ~ .thumb { left: 18px; background: var(--text); }
.switch input:focus-visible ~ .track { outline: 2px solid var(--accent);
                                       outline-offset: 2px; }
.switch input:disabled { cursor: default; }
.switch input:disabled ~ .track, .switch input:disabled ~ .thumb { opacity: .5; }
.setting-row { display: flex; gap: .75rem; align-items: flex-start;
               padding: .8rem 0; border-bottom: 1px solid var(--border); }
.setting-row:last-child { border-bottom: none; }
.setting-body { flex: 1; min-width: 0; }
.setting-label { font-weight: 600; display: flex; flex-wrap: wrap;
                 align-items: center; gap: .4rem; }
.setting-desc { color: var(--muted); font-size: .8rem; margin-top: .15rem; }
.setting-impact { color: var(--muted); font-size: .75rem; margin-top: .15rem; }
.setting-side { display: flex; align-items: center; gap: .4rem;
                 flex-wrap: wrap; margin-top: .4rem; }
.save-note { color: var(--muted); font-size: .75rem; min-height: 1rem; }
.banner.warn { border: 1px solid var(--gold); border-radius: 10px;
               padding: .7rem 1rem; color: var(--gold);
               background: rgba(217,165,63,.07); margin-bottom: 1rem; }
.banner.warn .btn { margin-left: .6rem; }
.badge.onoff { margin: 0 .15rem; }
.value-scroll { overflow-x: auto; white-space: nowrap; }
/* V2.2 drawer (side-panel variant at >=901px, inline under that). */
.drawer-scrim { position: fixed; inset: 0; background: var(--overlay); z-index: 40; }
.drawer-panel { position: fixed; top: 0; right: 0; bottom: 0; width: min(420px, 100%);
                background: var(--panel-raised); border-left: 1px solid var(--border);
                padding: 1rem; z-index: 41; overflow-y: auto; }
@media (max-width: 900px) {
  .span3 { grid-column: span 6; } .span6 { grid-column: span 12; }
  nav a, .btn, .switch { min-height: 44px; }
}
@media (max-width: 560px) {
  .span3 { grid-column: span 12; }
  .brand { display: none; }
  nav .brand-short { display: inline; }
  .kpi-num { font-size: 1.5rem; }
  td .btn { display: block; width: 100%; margin: .15rem 0; }
}
</style>
</head>
<body>
<nav>
  <span class="brand">token-saver</span>
  <span class="brand-short" aria-hidden="true">ts</span>
  <a href="#overview" data-tab="overview" class="active">Overview</a>
  <a href="#traffic" data-tab="traffic">Traffic</a>
  <a href="#providers" data-tab="providers">Providers</a>
  <a href="#keys" data-tab="keys">Keys &amp; Tenants</a>
  <a href="#settings" data-tab="settings">Settings</a>
  <span class="spacer"></span>
  <span id="bucket-slot"></span>
  <span class="muted" id="prov-status"></span>
</nav>
<main>
  <div id="content"><div class="skel-row"></div><div class="skel-row"></div><div class="skel-row short" style="width:60%"></div></div>
</main>
<script src="/static/dashboard.js"></script>
</body>
</html>"""


def render_shell() -> str:
    return SHELL
