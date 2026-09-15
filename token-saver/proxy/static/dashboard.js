/* token-saver dashboard (PA-3) — reads ONLY /api/kpis; no client-side
 * aggregation of ledger data (AC-A8). Rendering + Chart.js wiring only. */
(function () {
  "use strict";

  var content = document.getElementById("content");
  var state = { bucket: "day", from: null, to: null };
  var charts = [];

  // ---------------- helpers ----------------

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  function money(n) { return n === null || n === undefined ? "—" : "$" + Number(n).toFixed(4); }

  function destroyCharts() {
    charts.forEach(function (c) { try { c.destroy(); } catch (e) {} });
    charts = [];
  }

  function kpiCard(label, value, delta, deltaDir, sparkId) {
    return '<div class="card span3"><h3>' + label + "</h3>" +
      '<div class="kpi-num">' + value + "</div>" +
      (delta ? '<div class="kpi-delta ' + (deltaDir || "") + '">' + delta + "</div>" : "") +
      (sparkId ? '<canvas id="' + sparkId + '" height="36"></canvas>' : "") +
      "</div>";
  }

  function chartCard(title, span, inner) {
    return '<div class="card ' + span + '"><h3>' + title + "</h3>" + inner + "</div>";
  }

  function lineChart(id, labels, data, label, color) {
    var el = document.getElementById(id);
    if (!el) return;
    charts.push(new Chart(el, {
      type: "line",
      data: { labels: labels, datasets: [{ label: label, data: data,
        borderColor: color, backgroundColor: color + "33", fill: true, tension: 0.3, pointRadius: 2 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { color: "#8b93a3" }, grid: { color: "#262b36" } },
                  y: { ticks: { color: "#8b93a3" }, grid: { color: "#262b36" } } } }
    }));
  }

  function donutChart(id, labels, data, colors) {
    var el = document.getElementById(id);
    if (!el) return;
    charts.push(new Chart(el, {
      type: "doughnut",
      data: { labels: labels, datasets: [{ data: data, backgroundColor: colors }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "right", labels: { color: "#8b93a3" } } } }
    }));
  }

  function showError() {
    destroyCharts();
    content.innerHTML = '<div class="error-box"><strong>Couldn\'t load KPIs.</strong>' +
      "<p>The stats ledger may be unavailable.</p>" +
      '<button id="retry" type="button">Retry</button></div>';
    document.getElementById("retry").addEventListener("click", load);
  }

  function emptyState() {
    destroyCharts();
    content.innerHTML = '<div class="card span12"><div class="empty">' +
      "<h2>No traffic yet</h2><p>Send a prompt through the proxy to see your savings.</p></div></div>";
  }

  // ---------------- tab renderers ----------------

  function renderOverview(d) {
    var ov = d.overview;
    var labels = d.series.map(function (s) { return s.bucket; });
    var html = '<div class="grid">' +
      kpiCard("Requests", fmt(ov.requests), null, null, null) +
      kpiCard("Tokens saved", fmt(ov.input_tokens_saved), ov.savings_pct + "% of input", "up", "sp-tokens") +
      kpiCard("Est. cost saved", money(ov.cost_saved), "cache: " + money(ov.cache_savings) + " (separate)", "up", "sp-cost") +
      kpiCard("Effective savings %", ov.savings_pct + "%", "cache hit " + ov.cache_hit_pct + "%", "up", null) +
      chartCard("Savings over time", "span6", '<div class="chart-wrap"><canvas id="c-savings"></canvas></div>') +
      chartCard("Spend per model (cost before)", "span6", '<div class="chart-wrap"><canvas id="c-models"></canvas></div>') +
      '<div class="card span12"><h3>Recent buckets</h3><table><thead><tr><th>Bucket</th><th>Requests</th><th>Tokens saved</th><th>Cost saved</th><th>Cache savings</th><th>Errors</th></tr></thead><tbody>' +
      d.series.slice(-25).map(function (s) {
        return "<tr><td>" + s.bucket + "</td><td>" + fmt(s.requests) + "</td><td>" +
          fmt(s.tokens_saved) + "</td><td>" + money(s.cost_saved) + "</td><td>" +
          money(s.cache_savings) + "</td><td>" + (s.errors ? '<span class="badge red">' + s.errors + "</span>" : "0") + "</td></tr>";
      }).join("") + "</tbody></table></div></div>";
    content.innerHTML = html;
    lineChart("c-savings", labels, d.series.map(function (s) { return s.cost_saved; }), "cost saved", "#4f8cff");
    donutChart("c-models", d.by_model.map(function (m) { return m.model; }),
      d.by_model.map(function (m) { return m.requests; }),
      ["#4f8cff", "#35c28f", "#d9a53f", "#e5484d", "#9b7bff", "#5ac8fa"]);
  }

  function renderTraffic(d) {
    var ov = d.overview;
    var labels = d.series.map(function (s) { return s.bucket; });
    var errBadge = ov.error_rate_pct > 0 ? '<span class="badge red">' + ov.error_rate_pct + "%</span>"
                                          : '<span class="badge green">0%</span>';
    var html = '<div class="grid">' +
      chartCard("Latency percentiles (ms)", "span12", '<div class="chart-wrap"><canvas id="c-lat"></canvas></div>') +
      kpiCard("Avg latency", fmt(ov.avg_latency_ms) + " ms", null, null, null) +
      kpiCard("p95", fmt(d.latency.p95) + " ms", null, null, null) +
      kpiCard("Error rate", errBadge, null, null, null) +
      kpiCard("Cache hit rate", ov.cache_hit_pct + "%", null, null, null) +
      chartCard("Requests per bucket", "span6", '<div class="chart-wrap"><canvas id="c-req"></canvas></div>') +
      chartCard("Errors per bucket", "span6", '<div class="chart-wrap"><canvas id="c-err"></canvas></div>') +
      "</div>";
    content.innerHTML = html;
    lineChart("c-lat", labels, d.series.map(function () { return d.latency.p95; }), "p95", "#d9a53f");
    lineChart("c-req", labels, d.series.map(function (s) { return s.requests; }), "requests", "#4f8cff");
    lineChart("c-err", labels, d.series.map(function (s) { return s.errors; }), "errors", "#e5484d");
  }

  function renderProviders(d) {
    var rows = d.by_provider.map(function (p) {
      var errBadge = p.error_pct > 0 ? '<span class="badge red">' + p.error_pct + "%</span>"
                                     : '<span class="badge green">0%</span>';
      return '<div class="card span6"><h3>' + p.provider + "</h3>" +
        '<div class="kpi-num">' + fmt(p.requests) + ' <span style="font-size:.9rem;color:var(--muted)">requests</span></div>' +
        "<p>Tokens saved: <strong>" + fmt(p.tokens_saved) + "</strong><br>" +
        "Cost saved: <strong>" + money(p.cost_saved) + "</strong><br>" +
        "Cache hits: <strong>" + p.cache_hits + "</strong> (" + p.cache_hit_pct + "%) " +
        "Errors: " + errBadge +
        "</p></div>";
    });
    content.innerHTML = '<div class="grid">' +
      (rows.length ? rows.join("") : '<div class="card span12"><div class="empty">No provider traffic yet.</div></div>') +
      "</div>";
  }

  function renderKeys() {
    content.innerHTML = '<div class="grid"><div class="card span12"><div class="empty">' +
      "<h2>Keys &amp; Tenants</h2>" +
      "<p>Key management arrives with multi-tenant support (Phase C).</p>" +
      "<p style='font-size:.8rem'>No secret values are ever displayed — keys show last-4 only.</p>" +
      "</div></div></div>";
  }

  // ---------------- data + routing ----------------

  function fetchKpis() {
    var url = "/api/kpis?bucket=" + state.bucket;
    if (state.from) url += "&from=" + encodeURIComponent(state.from);
    if (state.to) url += "&to=" + encodeURIComponent(state.to);
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("bad status " + r.status);
      return r.json();
    });
  }

  function render(d) {
    destroyCharts();
    if (!d.overview || !d.overview.requests) { emptyState(); return; }
    var tab = location.hash.replace("#", "") || "overview";
    if (tab === "overview") renderOverview(d);
    else if (tab === "traffic") renderTraffic(d);
    else if (tab === "providers") renderProviders(d);
    else if (tab === "keys") renderKeys();
    else renderOverview(d);
  }

  function load() {
    content.innerHTML = '<div class="skel-row"></div><div class="skel-row"></div><div class="skel-row short" style="width:60%"></div>';
    fetchKpis().then(render).catch(showError);
  }

  window.addEventListener("hashchange", load);

  // bucket selector (client-side control, not aggregation)
  var sel = document.createElement("select");
  sel.innerHTML = '<option value="minute">Minute</option><option value="hour">Hour</option><option value="day" selected>Day</option>';
  sel.style.cssText = "background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:.3rem";
  sel.addEventListener("change", function () { state.bucket = sel.value; load(); });
  document.querySelector("nav").insertBefore(sel, document.getElementById("prov-status"));

  load();
})();
