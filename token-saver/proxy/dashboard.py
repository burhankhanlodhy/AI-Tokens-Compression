"""Server-rendered HTML dashboard for the /stats page.

Renders per the UI/UX flow spec:
- header band: total requests, tokens saved (raw + %), est. cost saved
- tabs: By day (default) / By route / By model
- empty state: hero copy + CTA when no requests are logged yet

Error and loading states are handled client-side by dashboard.js: the page
ships a minimal shell and the JS re-fetches /stats (format=json) to refresh;
if that fetch fails it swaps in an inline error + retry button.
"""
from __future__ import annotations

import html
from typing import Any


def _esc(v: Any) -> str:
    return html.escape(str(v))


def _render_breakdown(rows: list[dict[str, Any]], label_key: str) -> str:
    if not rows:
        return '<p class="muted">No data for this view yet.</p>'
    head = (
        "<tr><th>{}</th><th>Requests</th><th>Tokens in</th>"
        "<th>Tokens in (compressed)</th><th>Saved</th><th>Savings %</th>"
        "<th>Est. cost saved</th></tr>"
    ).format("Day" if label_key == "day" else label_key.capitalize())
    body_rows = []
    for r in rows:
        saved = r["input_before"] - r["input_after"]
        pct = round(100 * saved / r["input_before"], 1) if r["input_before"] else 0.0
        cost_saved = r["cost_before"] - r["cost_after"]
        label_html = _esc(r[label_key])
        if label_key == "route":
            # clickable chip: click to filter the table to this route
            label_html = (
                f'<button type="button" class="chip" data-chip="{_esc(r[label_key])}" '
                f'title="Click to filter to this route">{label_html}</button>'
            )
        body_rows.append(
            f"<tr><td>{label_html}</td><td>{r['requests']}</td>"
            f"<td>{r['input_before']:,}</td><td>{r['input_after']:,}</td>"
            f"<td>{saved:,}</td><td>{pct}%</td><td>${cost_saved:.4f}</td></tr>"
        )
    return (
        '<div class="tablewrap"><table><thead>'
        + head
        + "</thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _render_stats_html(data: dict[str, Any]) -> str:
    t = data["totals"]
    empty = t["requests"] == 0

    if empty:
        body = """
        <section class="hero">
          <h2>No requests yet</h2>
          <p>Send your first prompt through the proxy to see your savings.</p>
          <pre>curl http://localhost:8000/v1/chat/completions \\
  -H "Authorization: Bearer $YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"z-ai/glm-5.3-flash","messages":[{"role":"user","content":"Hello"}]}'</pre>
        </section>"""
    else:
        body = f"""
        <section class="band">
          <div class="stat"><span class="num">{t['requests']:,}</span>
            <span class="label">Requests</span></div>
          <div class="stat"><span class="num">{t['input_tokens_saved']:,}</span>
            <span class="label">Tokens saved ({t['input_savings_pct']}%)</span></div>
          <div class="stat"><span class="num">${t['cost_saved']:.4f}</span>
            <span class="label">Est. cost saved</span></div>
        </section>
        <nav class="tabs" role="tablist">
          <button class="tab active" data-tab="by_day" role="tab">By day</button>
          <button class="tab" data-tab="by_route" role="tab">By route</button>
          <button class="tab" data-tab="by_model" role="tab">By model</button>
        </nav>
        <section id="panel_by_day" class="panel">{_render_breakdown(data['by_day'], 'day')}</section>
        <section id="panel_by_route" class="panel" hidden>{_render_breakdown(data['by_route'], 'route')}</section>
        <section id="panel_by_model" class="panel" hidden>{_render_breakdown(data['by_model'], 'model')}</section>
        <p class="muted">Avg latency: {t['avg_latency_ms']} ms &middot;
          Output tokens: {t['output_tokens']:,}</p>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>token-saver &mdash; stats</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 880px; margin: 0 auto; padding: 1.5rem; }}
  h1 {{ font-size: 1.3rem; }}
  .band {{ display: flex; gap: 2rem; flex-wrap: wrap; margin: 1rem 0 2rem; }}
  .stat {{ display: flex; flex-direction: column; }}
  .num {{ font-size: 2rem; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .label {{ color: #666; }}
  .tabs {{ display: flex; gap: .5rem; border-bottom: 1px solid #ccc; }}
  .tab {{ background: none; border: none; padding: .5rem .9rem; cursor: pointer;
          font: inherit; border-bottom: 2px solid transparent; }}
  .tab.active {{ border-bottom-color: currentColor; font-weight: 600; }}
  .tablewrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: .45rem .7rem; border-bottom: 1px solid #ddd;
            font-variant-numeric: tabular-nums; }}
  .muted {{ color: #666; }}
  .chip {{ background: #eef; border: 1px solid #ccd; border-radius: 999px;
           padding: .15rem .7rem; cursor: pointer; font: inherit; }}
  .chip-active {{ background: #3355cc; color: #fff; border-color: #3355cc; }}
  .skel {{ display: inline-block; min-width: 6rem; border-radius: 6px;
           background: linear-gradient(90deg, #e8e8e8 25%, #f5f5f5 50%, #e8e8e8 75%);
           background-size: 200% 100%; animation: shimmer 1.2s infinite; }}
  .skel-line {{ display: inline-block; min-width: 5rem; border-radius: 4px;
                background: linear-gradient(90deg, #eee 25%, #f7f7f7 50%, #eee 75%);
                background-size: 200% 100%; animation: shimmer 1.2s infinite; }}
  .skel-row {{ height: 1.6rem; margin: .7rem 0; border-radius: 6px;
               background: linear-gradient(90deg, #e8e8e8 25%, #f5f5f5 50%, #e8e8e8 75%);
               background-size: 200% 100%; animation: shimmer 1.2s infinite; }}
  .skel-row.short {{ width: 60%; }}
  @keyframes shimmer {{ 0% {{ background-position: 200% 0; }} 100% {{ background-position: -200% 0; }} }}
  .hero {{ text-align: center; padding: 3rem 1rem; }}
  .hero pre {{ display: inline-block; text-align: left; background: #f4f4f4;
               padding: 1rem; border-radius: 8px; overflow-x: auto; }}
  .error {{ background: #fee; border: 1px solid #c99; padding: 1rem; border-radius: 8px; }}
  .error button {{ margin-top: .5rem; }}
</style>
</head>
<body>
<h1>token-saver stats</h1>
<div id="content">{body}</div>
<script>
(function () {{
  var content = document.getElementById('content');

  // Tab switching (delegated; survives refresh re-render).
  content.addEventListener('click', function (e) {{
    var tab = e.target.closest('.tab');
    if (tab) {{
      content.querySelectorAll('.tab').forEach(function (b) {{ b.classList.remove('active'); }});
      tab.classList.add('active');
      content.querySelectorAll('.panel').forEach(function (p) {{ p.hidden = p.id !== 'panel_' + tab.dataset.tab; }});
      setChipFilter(activeChip);  // re-apply current filter to the new tab
      return;
    }}
    // Route-chip filtering: click a chip in the By-route table to filter
    // the detail rows to that route; click again (or "All") to clear.
    var chip = e.target.closest('.chip');
    if (chip) {{
      var name = chip.dataset.chip;
      activeChip = (activeChip === name) ? null : name;
      setChipFilter(activeChip);
    }}
  }});

  var activeChip = null;

  function setChipFilter(name) {{
    var routePanel = document.getElementById('panel_by_route');
    if (!routePanel) return;
    var rows = routePanel.querySelectorAll('tbody tr');
    var anyChips = routePanel.querySelector('.chip');
    if (!anyChips) return;
    rows.forEach(function (tr) {{
      var chipCell = tr.querySelector('.chip');
      var match = !name || (chipCell && chipCell.dataset.chip === name);
      tr.style.display = match ? '' : 'none';
    }});
    routePanel.querySelectorAll('.chip').forEach(function (c) {{
      c.classList.toggle('chip-active', c.dataset.chip === name);
    }});
  }}

  // Retry button for the error state.
  content.addEventListener('click', function (e) {{
    if (e.target.id === 'retry') load();
  }});

  function showSkeleton() {{
    content.innerHTML =
      '<section class="band">' +
      '<div class="stat"><span class="num skel">&nbsp;</span><span class="label skel-line">&nbsp;</span></div>'.repeat(3) +
      '</section>' +
      '<div class="skel-row"></div>'.repeat(4) +
      '<div class="skel-row short"></div>';
  }}

  function showError() {{
    content.innerHTML = '<div class="error"><strong>Couldn\\'t load stats.</strong>' +
      '<p>The proxy may be busy or the stats database is unavailable.</p>' +
      '<button id="retry" type="button">Retry</button></div>';
  }}

  function render(data) {{
    var t = data.totals;
    if (!t.requests) {{
      content.innerHTML = '<section class="hero"><h2>No requests yet</h2>' +
        '<p>Send your first prompt through the proxy to see your savings.</p></section>';
      return;
    }}
    // Re-fetch the fully rendered page for simplicity; the tab layout is
    // server-rendered so we just swap the whole content shell in.
    fetch('/stats?format=html').then(function (r) {{
      if (!r.ok) throw new Error('bad status');
      return r.text();
    }}).then(function (htmlText) {{
      var doc = new DOMParser().parseFromString(htmlText, 'text/html');
      content.innerHTML = doc.getElementById('content').innerHTML;
    }}).catch(showError);
  }}

  function load() {{
    showSkeleton();
    fetch('/stats').then(function (r) {{
      if (!r.ok) throw new Error('bad status');
      return r.json();
    }}).then(render).catch(showError);
  }}
  window.__tsReloadStats = load;
}})();
</script>
</body>
</html>"""
