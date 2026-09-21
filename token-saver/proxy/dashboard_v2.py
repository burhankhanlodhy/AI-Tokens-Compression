"""PA-3: four-tab dashboard (Overview / Traffic / Providers / Keys).

Server-rendered shell + Chart.js (CDN) + one dashboard.js. Every card and
chart reads ONLY from /api/kpis (no client-side aggregation — AC-A8).
Layout per @ui-ux-engineer's PA-3 spec: 12-col grid, KPI cards span 3,
charts span 6; dark theme; empty/loading/error states shared.
"""
from __future__ import annotations

from typing import Any


SHELL = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>token-saver dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root {
  --bg: #0f1115; --panel: #171a21; --border: #262b36; --text: #e6e9ef;
  --muted: #8b93a3; --accent: #4f8cff; --green: #35c28f; --red: #e5484d;
  --gold: #d9a53f;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 system-ui, sans-serif; }
nav { display: flex; align-items: center; gap: 1.5rem; padding: .8rem 1.5rem;
      border-bottom: 1px solid var(--border); background: var(--panel); }
nav .brand { font-weight: 700; }
nav .spacer { flex: 1; }
nav a { color: var(--muted); text-decoration: none; padding: .35rem .8rem;
        border-radius: 6px; }
nav a.active, nav a:hover { color: var(--text); background: var(--border); }
main { padding: 1.5rem; max-width: 1200px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 10px; padding: 1rem; }
.card h3 { margin: 0 0 .5rem; font-size: .8rem; font-weight: 600;
           color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.span3 { grid-column: span 3; } .span6 { grid-column: span 6; }
.span12 { grid-column: span 12; }
.kpi-num { font-size: 1.9rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.kpi-delta { font-size: .8rem; color: var(--muted); }
.kpi-delta.up { color: var(--green); } .kpi-delta.down { color: var(--red); }
.chart-wrap { position: relative; height: 260px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-size: .75rem; text-transform: uppercase; }
.skel { display: inline-block; border-radius: 6px; height: 1.6rem; min-width: 5rem;
        background: linear-gradient(90deg, #1d222c 25%, #262b36 50%, #1d222c 75%);
        background-size: 200% 100%; animation: shimmer 1.2s infinite; }
.skel-row { height: 2.4rem; margin: .6rem 0; border-radius: 8px;
            background: linear-gradient(90deg, #1d222c 25%, #262b36 50%, #1d222c 75%);
            background-size: 200% 100%; animation: shimmer 1.2s infinite; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
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
.btn { background: var(--accent); color: #fff; border: none;
       padding: .3rem .7rem; border-radius: 6px; cursor: pointer; font-size: .8rem; }
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
@media (max-width: 900px) { .span3 { grid-column: span 6; } .span6 { grid-column: span 12; } }
</style>
</head>
<body>
<nav>
  <span class="brand">token-saver</span>
  <a href="#overview" data-tab="overview" class="active">Overview</a>
  <a href="#traffic" data-tab="traffic">Traffic</a>
  <a href="#providers" data-tab="providers">Providers</a>
  <a href="#keys" data-tab="keys">Keys &amp; Tenants</a>
  <span class="spacer"></span>
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
