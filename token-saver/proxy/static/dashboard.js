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

  function currentTab() {
    return location.hash.replace("#", "") || "overview";
  }

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

  /* ---------------- Savings breakdown (a1eae90 decomposition contract) ------
   * Headline = cost_saved ALONE. L1 and cache render only as contained
   * annotations OF that total — separate tiles, never merged, never summed.
   * Every value is read 1:1 from /api/kpis (AC-A8); the only client-side
   * computation is the bar width (geometry), never a displayed number. */
  function l1SubTile(ov) {
    if (!ov.l1_tokens_stripped) {
      return '<div class="subtile l1-zero"><span class="zero-dash">—</span> ' +
        "No structural (L1) savings this window</div>";
    }
    var width = ov.cost_saved > 0 ? Math.min(100, 100 * ov.l1_cost_saved / ov.cost_saved) : 0;
    return '<div class="subtile">' +
      '<div class="lead">of which L1 structural</div>' +
      '<div class="subrow"><span>Tokens stripped</span><strong>' + fmt(ov.l1_tokens_stripped) + " tokens</strong></div>" +
      '<div class="subrow"><span>Cost saved</span><strong>' + money(ov.l1_cost_saved) + '</strong> <span class="muted-note">portion of total</span></div>' +
      '<div class="bar-total" title="L1 portion of total cost saved"><div class="bar-l1" style="width:' + width + '%"></div></div>' +
      "</div>";
  }

  function cacheSubTile(ov) {
    if (!ov.cache_savings) {
      return '<div class="subtile l1-zero"><span class="zero-dash">—</span> ' +
        "No exact-prefix cache savings this window</div>";
    }
    return '<div class="subtile">' +
      '<div class="lead">of which exact-prefix cache</div>' +
      '<div class="subrow"><span>Cache savings</span><strong>' + money(ov.cache_savings) + '</strong> <span class="muted-note">reported separately (AC-A6)</span></div>' +
      "</div>";
  }

  /* ------------- v1.1 semantic cache (AC-PC-UI, cache-status-dashboard-spec) -
   * Every number is a verbatim copy of a /api/kpis `cache` field computed in
   * SQL (I-1); the decomposition is never summed into the headline (I-2);
   * `enabled` is a mode, never a measurement (I-4): flag-off renders `off`/—,
   * never 0/0%. Mode states: off / warming (enabled, no semantic hits in the
   * window) / live (enabled, ≥1 semantic hit). Legacy backends that predate
   * the `cache` key fall back to the v1.0 tiles (exact-only, off state) —
   * the exact cache is unaffected by the semantic flag. */
  function cacheOf(d) { return d && d.cache ? d.cache : null; }

  function semanticMode(cache) {
    if (!cache || !cache.enabled) return "off";
    return cache.semantic_hit_count > 0 ? "live" : "warming";
  }

  function modeBadge(mode) {
    var cls = mode === "live" ? "green" : (mode === "warming" ? "gold" : "neutral");
    return '<span class="badge ' + cls + '">' + mode + "</span>";
  }

  /* §3.1: primary value = first element (hit_count DESC, server-ordered);
   * disagreement parenthetical counts non-primary rows exactly when either
   * array spans >1 version. Array math only — never config, never a scalar. */
  function versionLine(cache) {
    var ev = cache.embedding_versions || [];
    if (!ev.length) return "";
    var qv = cache.quality_versions || [];
    var extra = 0, i;
    for (i = 1; i < ev.length; i++) extra = extra + (ev[i].hit_count || 0);
    for (i = 1; i < qv.length; i++) extra = extra + (qv[i].hit_count || 0);
    var qv0 = qv.length ? qv[0].version : "—";
    var line = "embeddings: " + escapeHtml(ev[0].version) + " / " + escapeHtml(qv0);
    if (ev.length > 1 || qv.length > 1) {
      line += " (+" + extra + " rows on other versions)";
    }
    return '<div class="muted-note">' + line + "</div>";
  }

  function exactSubTile(cache, ov) {
    if (!cache) return cacheSubTile(ov);   // legacy backend: v1.0 tile
    if (!cache.exact_hit_count) {
      return '<div class="subtile l1-zero"><span class="zero-dash">—</span> ' +
        "No exact cache hits this window</div>";
    }
    return '<div class="subtile">' +
      '<div class="lead">Exact cache</div>' +
      '<div class="subrow"><span>Requests served</span><strong>' +
      fmt(cache.exact_hit_count) + " requests</strong></div>" +
      '<div class="subrow"><span>Cost saved</span><strong>' +
      money(cache.exact_hit_savings) + "</strong></div>" +
      "</div>";
  }

  function semanticSubTile(cache) {
    if (!cache) cache = { enabled: false };   // legacy backend: off by definition
    var mode = semanticMode(cache);
    var html = '<div class="subtile">' +
      '<div class="lead">Semantic cache ' + modeBadge(mode) + "</div>";
    if (mode === "off") {
      // I-4: a mode, not a measurement — no rates, no $0.00, no version line
      // (a window can hold historical semantic rows while the flag is off).
      return html +
        '<div class="muted-note">Semantic cache is disabled — no lookups are running.</div>' +
        "</div>";
    }
    if (cache.semantic_hit_count > 0) {
      html = html +
        '<div class="subrow"><span>Requests served</span><strong>' +
        fmt(cache.semantic_hit_count) + " requests</strong></div>" +
        '<div class="subrow"><span>Cost saved</span><strong>' +
        money(cache.semantic_hit_savings) + '</strong> <span class="muted-note">portion of total</span></div>';
    } else {
      html = html + '<div class="muted-note">No semantic hits yet — cache is warming.</div>';
    }
    // §5.2/§7b: the rate renders only when the window has semantic hits;
    // warming shows `—` (a mode) while the threshold-miss count carries the
    // measurement pressure.
    var showRate = cache.semantic_hit_count > 0;
    var rate = cache.semantic_hit_rate;
    var rateLine = "hit rate " +
      (showRate && rate !== null && rate !== undefined ? rate + "%" : "—");
    if (cache.semantic_threshold_miss_count > 0) {
      rateLine = rateLine + " · " + fmt(cache.semantic_threshold_miss_count) + " threshold-misses";
    }
    html = html + '<div class="muted-note">' + rateLine + "</div>";
    html = html + versionLine(cache);
    return html + "</div>";
  }

  /* §4.2: the only percentage this card may show is semantic_hit_rate — the
   * combined rate renders nowhere (§3). The numeral renders only in live
   * mode: off/warming are modes (—, §5.1/§5.2), never 0% presentations of
   * stale or empty windows. The contract carries no per-bucket semantic
   * series, so there is no delta line: one would have to be fabricated
   * client-side (I-1 forbids it). */
  function semanticHitRateCard(cache) {
    var mode = semanticMode(cache);
    var num = "—";
    if (mode === "live" && cache.semantic_hit_rate !== null &&
        cache.semantic_hit_rate !== undefined) {
      num = cache.semantic_hit_rate + "%";
    }
    return '<div class="card span3"><h3>Semantic cache hit rate ' + modeBadge(mode) + "</h3>" +
      '<div class="kpi-num">' + num + "</div></div>";
  }

  /* §4.3 (as adapted): the /api/kpis contract exposes ledger cache_status as
   * window-scope counts, not per-request rows, so the Traffic surface renders
   * one badge per raw ledger status with its verbatim count. Badge text is
   * the raw status string — no renaming layer, so QA reconciles badge ↔
   * ledger taxonomy directly. Flag-off: semantic statuses show neutral `off`
   * (a mode — no count), exact_hit/miss keep counting (unaffected by flag). */
  function cacheStatusCard(cache) {
    if (!cache) return "";
    var badgeCount = function (status, cls, count) {
      var n = count === null || count === undefined ? "—" : fmt(count);
      return '<div class="subrow"><span>' + status + '</span>' +
        '<span class="badge ' + cls + '">' + status + "</span> <strong>" + n + "</strong></div>";
    };
    var html = '<div class="card span12"><h3>Cache status (ledger taxonomy)</h3>' +
      badgeCount("exact_hit", "green", cache.exact_hit_count);
    if (cache.enabled) {
      html = html + badgeCount("semantic_hit", "green", cache.semantic_hit_count) +
        badgeCount("semantic_threshold_miss", "gold", cache.semantic_threshold_miss_count);
    } else {
      html = html + '<div class="subrow"><span>semantic_hit</span>' +
        '<span class="badge neutral">off</span></div>' +
        '<div class="subrow"><span>semantic_threshold_miss</span>' +
        '<span class="badge neutral">off</span></div>';
    }
    html = html + badgeCount("miss", "neutral", cache.miss_count) + "</div>";
    return html;
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
    var last = d.series.length ? d.series[d.series.length - 1] : null;
    var prev = d.series.length > 1 ? d.series[d.series.length - 2] : null;
    var deltaHtml = null, deltaDir = "";
    if (last && prev) {
      var diff = last.cost_saved - prev.cost_saved;
      deltaDir = diff >= 0 ? "up" : "down";
      deltaHtml = (diff >= 0 ? "▲ " : "▼ ") + money(Math.abs(diff)) + " vs prior bucket";
    }
    var cache = cacheOf(d);
    var html = '<div class="grid">' +
      kpiCard("Requests", fmt(ov.requests), null, null, null) +
      kpiCard("Tokens saved", fmt(ov.input_tokens_saved), ov.savings_pct + "% of input", "up", "sp-tokens") +
      kpiCard("Est. cost saved", money(ov.cost_saved), deltaHtml, deltaDir, "sp-cost") +
      kpiCard("Effective savings %", ov.savings_pct + "%", "cache hit " + ov.cache_hit_pct + "%", "up", null) +
      semanticHitRateCard(cache) +
      '<div class="card span6"><h3>Savings breakdown</h3>' +
        '<p class="breakdown-note">Savings decomposition: exact, semantic, and L1 are per-request categories — never summed into the headline.</p>' +
        l1SubTile(ov) + exactSubTile(cache, ov) + semanticSubTile(cache) +
      "</div>" +
      chartCard("Savings over time", "span6", '<div class="chart-wrap"><canvas id="c-savings"></canvas></div>') +
      chartCard("Cost saved per model", "span12", '<div class="chart-wrap"><canvas id="c-models"></canvas></div>') +
      '<div class="card span12"><h3>Recent buckets</h3><table><thead><tr><th>Bucket</th><th>Requests</th><th>Tokens saved</th><th>Cost saved</th><th>Cache savings</th><th>Errors</th></tr></thead><tbody>' +
      d.series.slice(-25).map(function (s) {
        return "<tr><td>" + s.bucket + "</td><td>" + fmt(s.requests) + "</td><td>" +
          fmt(s.tokens_saved) + "</td><td>" + money(s.cost_saved) + "</td><td>" +
          money(s.cache_savings) + "</td><td>" + (s.errors ? '<span class="badge red">' + s.errors + "</span>" : "0") + "</td></tr>";
      }).join("") + "</tbody></table></div></div>";
    content.innerHTML = html;
    lineChart("c-savings", labels, d.series.map(function (s) { return s.cost_saved; }), "cost saved", "#4f8cff");
    // F1 (AC-A8 / D1 ratified): donut = exactly ONE by_model field, cost_saved,
    // titled to match — by_model carries no cost_before and no l1_* (§5).
    donutChart("c-models", d.by_model.map(function (m) { return m.model; }),
      d.by_model.map(function (m) { return m.cost_saved; }),
      ["#4f8cff", "#35c28f", "#d9a53f", "#e5484d", "#9b7bff", "#5ac8fa"]);
  }

  /* §6.2 Route mix (D-2): `by_route` is a window-global array of
   * {route, requests, cost_saved} — two verbatim rows plus one stacked bar
   * whose widths are geometry only. Absent field (pre-V2.2 backend) → the
   * card is omitted entirely; route data is NEVER synthesized from other
   * fields. A passthrough-only window renders a one-row table, not an error. */
  function routeMixCard(d) {
    var rows = d.by_route;
    if (!rows || !rows.length) return "";
    var total = 0;
    rows.forEach(function (r) { total = total + (r.requests || 0); });
    var bar = rows.map(function (r, i) {
      var w = total > 0 ? Math.min(100, 100 * (r.requests || 0) / total) : 0;
      var colors = ["var(--accent)", "var(--gold)"];
      return '<div style="position:absolute;left:0;top:0;bottom:0;width:' + w +
        '%;background:' + colors[i % 2] + '"></div>';
    }).join("");
    return '<div class="card span6"><h3>Route mix</h3>' +
      '<div class="table-scroll"><table><thead><tr><th>Route</th><th>Requests</th><th>Cost saved</th></tr></thead><tbody>' +
      rows.map(function (r) {
        return "<tr><td>" + escapeHtml(r.route) + "</td><td>" + fmt(r.requests) +
          "</td><td>" + money(r.cost_saved) + "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      '<div class="bar-total" title="request share by route">' + bar + "</div>" +
      '<div class="muted-note">Bar widths show each route\'s share of requests; route counts are window-global.</div></div>';
  }

  function renderTraffic(d) {
    var ov = d.overview;
    var labels = d.series.map(function (s) { return s.bucket; });
    var errBadge = ov.error_rate_pct > 0 ? '<span class="badge red">' + ov.error_rate_pct + "%</span>"
                                          : '<span class="badge green">0%</span>';
    // F2 (AC-A8 / D2 ratified): the contract exposes window-global percentiles
    // only (latency.p50/p95/p99) — no per-bucket latency series exists in
    // `series`, so no latency line chart may be fabricated. Percentiles render
    // as KPI cards, 1:1, alongside overview.avg_latency_ms.
    var html = '<div class="grid">' +
      kpiCard("Latency p50", fmt(d.latency.p50) + " ms", null, null, null) +
      kpiCard("Latency p95", fmt(d.latency.p95) + " ms", null, null, null) +
      kpiCard("Latency p99", fmt(d.latency.p99) + " ms", null, null, null) +
      kpiCard("Avg latency", fmt(ov.avg_latency_ms) + " ms", null, null, null) +
      chartCard("Requests per bucket", "span6", '<div class="chart-wrap"><canvas id="c-req"></canvas></div>') +
      chartCard("Errors per bucket", "span6", '<div class="chart-wrap"><canvas id="c-err"></canvas></div>') +
      kpiCard("Error rate", errBadge, null, null, null) +
      kpiCard("Cache hit rate", ov.cache_hit_pct + "%", null, null, null) +
      routeMixCard(d) +
      cacheStatusCard(cacheOf(d)) +
      "</div>";
    content.innerHTML = html;
    lineChart("c-req", labels, d.series.map(function (s) { return s.requests; }), "requests", "#4f8cff");
    lineChart("c-err", labels, d.series.map(function (s) { return s.errors; }), "errors", "#e5484d");
  }

  function renderProvidersKpis(d, registry, registryNote) {
    var byName = {};
    if (registry) registry.forEach(function (r) { byName[r.name] = r; });
    var rows = d.by_provider.map(function (p) {
      var reg = byName[p.provider] || null;
      return providerCardHtml(p, reg, registryNote);
    });
    content.innerHTML = '<div class="grid">' +
      (rows.length ? rows.join("") : '<div class="card span12"><div class="empty">No provider traffic yet.</div></div>') +
      "</div>";
  }

  /* §6.3: one card per KPI provider row, joined name-keyed with the registry
   * row (a join, not aggregation — no number is computed). Registry facts:
   * adapter chip, monospace base_url, enabled/disabled badge. `base_url` is
   * configuration and displayable (PM §2.3); no credential field exists on
   * the registry row (BYOK invariant, PM §3.4). Provider-native cache usage
   * renders ONLY when the provider's KPI object carries the fields. */
  function providerCardHtml(p, reg, registryNote) {
    var errBadge = p.error_pct > 0 ? '<span class="badge red">' + p.error_pct + "%</span>"
                                   : '<span class="badge green">0%</span>';
    var l1Line = p.l1_tokens_stripped
      ? "L1 structural: <strong>" + fmt(p.l1_tokens_stripped) + " tokens</strong>, " +
        money(p.l1_cost_saved) + ' <span class="muted-note">portion of cost saved</span>'
      : 'L1 structural: <span class="zero-dash">—</span>';
    var html = '<div class="card span6"><h3>' + escapeHtml(p.provider) + " " +
      (reg
        ? (reg.enabled
          ? '<span class="badge green">enabled</span>'
          : '<span class="badge neutral">disabled</span>') +
          ' <span class="chip">' + escapeHtml(reg.adapter_class) + "</span>"
        : "") + "</h3>";
    if (reg && reg.base_url) {
      html += '<div class="muted-note mono">' + escapeHtml(reg.base_url) + "</div>";
    }
    if (registryNote) {
      html += '<div class="muted-note">' + registryNote + "</div>";
    }
    html += '<div class="kpi-num">' + fmt(p.requests) +
      ' <span style="font-size:.9rem;color:var(--muted)">requests</span></div>' +
      "<p>Tokens saved: <strong>" + fmt(p.tokens_saved) + "</strong><br>" +
      "Cost saved: <strong>" + money(p.cost_saved) + "</strong><br>" +
      l1Line + "<br>" +
      "Cache hits: <strong>" + p.cache_hits + "</strong> (" + p.cache_hit_pct + "%) " +
      "Errors: " + errBadge + "</p>";
    if (p.provider_cache_read_tokens !== undefined ||
        p.provider_cache_write_tokens !== undefined) {
      html += '<div class="subrow"><span>Provider-native cache</span><span>' +
        "read <strong>" + fmt(p.provider_cache_read_tokens) + "</strong> · " +
        "write <strong>" + fmt(p.provider_cache_write_tokens) + "</strong>" +
        "</span></div>" +
        '<div class="muted-note">provider-native cache — measured evidence, never merged into savings</div>';
    }
    return html + "</div>";
  }

  /* §5.3: the Providers tab issues TWO fetches — /api/kpis (existing) and
   * /api/providers (D-2 registry). Either failing degrades only its own
   * facts: registry down renders KPI cards + a card-level note; KPIs down
   * renders the shared error block with Retry. */
  function loadProvidersTab() {
    content.innerHTML = '<div class="skel-row"></div><div class="skel-row"></div>';
    fetchJson("/api/providers").then(function (regBody) {
      // a 200 without the expected {providers:[…]} envelope is "registry
      // facts unavailable" too — the degraded-note path, not a silent
      // render. An empty array is a real inventory and renders facts.
      var registry = (regBody && regBody.providers && regBody.providers.slice
                      && regBody.providers.length !== undefined)
        ? regBody.providers : null;
      var regNote = registry
        ? (registry.length
          ? null
          : "registry facts unavailable — Retry")
        : "registry facts unavailable — Retry";
      fetchKpis().then(function (d) {
        renderProvidersKpis(d, registry, regNote);
      }).catch(showError);
    }).catch(function () {
      fetchKpis().then(function (d) {
        renderProvidersKpis(d, null, "registry facts unavailable — Retry");
      }).catch(showError);
    });
  }

  // ---------------- Keys & Tenants tab (C6 — keys-tenants-tab-spec.md) ------
  /* Proxy-facing key management only (C10 redaction: key_last4 + the one-time
   * plaintext reveal; key_hash is never sent by the API and never rendered).
   * Every displayed number is read 1:1 from /api/kpis (tenant_id/api_key_id
   * scoped) or /api/tenants|/api/keys — zero client-side aggregation.
   * Write endpoints attach `Authorization: Bearer <admin token>` ONLY after
   * §4.5 token entry; the token lives in a module variable ONLY — never in
   * storage, URLs, or logs, and never rendered outside the masked input. */

  var adminToken = null;      // §4.5: module variable only
  var tokenPrompt = null;     // {retry, msg} — pending write replay after 401
  var keysCtx = null;         // {tenant, keys, tkpis} from the last load
  var pendingConfirm = null;  // {type: "revoke"|"rotate", keyId}
  var confirmTimer = null;
  var keyFormOpen = false;
  var revealData = null;      // {key, last4, mode: "create"|"rotate"}
  var drawerState = null;     // {key, loading|data|error}

  function escapeHtml(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function escDate(s) { return escapeHtml(String(s || "").slice(0, 10)); }

  /* §8.2/I-12 (G2): when any tenant row carries a non-self_host plan, Keys
   * and Settings render this warning verbatim — management/KPI reads are
   * unauthenticated by the self-host ruling, and a hosted plan drifts from
   * that threat model. Dismissal is visual only and resets on reload. */
  var hostedBannerDismissed = false;
  function hostedBannerHtml(tenants) {
    if (hostedBannerDismissed) return "";
    var hosted = (tenants || []).some(function (t) {
      return t && t.plan && t.plan !== "self_host";
    });
    if (!hosted) return "";
    return '<div class="banner warn" id="hosted-banner">' +
      "Management and KPI reads are unauthenticated (self-host threat model). " +
      "This deployment has a non-self-host tenant plan — restrict network " +
      "access or front the proxy with an auth layer." +
      '<button id="hosted-dismiss" type="button" class="btn ghost">Dismiss</button></div>';
  }
  function bindHostedBanner() {
    bindClick("hosted-dismiss", function () {
      hostedBannerDismissed = true;
      var el = document.getElementById("hosted-banner");
      if (el) el.innerHTML = "";
    });
  }

  function fetchJson(url) {
    return fetch(url).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        if (!r.ok) {
          var e = new Error(b && b.detail ? b.detail : "bad status " + r.status);
          e.status = r.status;
          throw e;
        }
        return b;
      });
    });
  }
  var keysFetch = fetchJson;

  /* Scoped KPI url: selector first so a filtered url is distinguishable from
   * the unscoped /api/kpis?bucket=… fetch the other tabs make. */
  function keysKpisUrl(extra) {
    var url = "/api/kpis?" + extra + "&bucket=" + state.bucket;
    if (state.from) url += "&from=" + encodeURIComponent(state.from);
    if (state.to) url += "&to=" + encodeURIComponent(state.to);
    return url;
  }

  function onKeysTab() {
    return currentTab() === "keys";
  }

  function loadKeys() {
    pendingConfirm = null;
    keyFormOpen = false;
    revealData = null;
    drawerState = null;
    if (confirmTimer) { clearTimeout(confirmTimer); confirmTimer = null; }
    content.innerHTML = '<div class="skel-row"></div><div class="skel-row"></div><div class="skel-row short" style="width:60%"></div>';
    keysFetch("/api/tenants").then(function (tenants) {
      if (!tenants || !tenants.length) {
        throw new Error("No tenant rows found — the proxy may not be initialized yet.");
      }
      var t = tenants[0]; // self-host: exactly one tenant, no picker (§2.1)
      return Promise.all([
        keysFetch("/api/keys?tenant_id=" + encodeURIComponent(t.id)),
        keysFetch(keysKpisUrl("tenant_id=" + encodeURIComponent(t.id)))
      ]).then(function (res) {
        keysCtx = { tenant: t, keys: res[0], tkpis: res[1] };
        renderKeysUI();
      });
    }).catch(function (e) { keysError(e && e.message); });
  }

  function keysError(msg) {
    destroyCharts();
    content.innerHTML = '<div class="error-box"><strong>Couldn\'t load key management.</strong>' +
      "<p>" + escapeHtml(msg || "The tenants/keys API may be unavailable.") + "</p>" +
      '<button id="retry" type="button">Retry</button></div>';
    document.getElementById("retry").addEventListener("click", function () { loadKeys(); });
  }

  function statusBadge(s) {
    if (s === "active") return '<span class="badge green">active</span>';
    if (s === "revoked") return '<span class="badge red">revoked</span>';
    return '<span class="badge neutral">rotated</span>';
  }

  function actionsInner(k) {
    var id = String(k.id);
    var errSlot = '<span class="inline-err" id="keyerr-' + id + '"></span>';
    if (k.status !== "active") return "—" + errSlot;
    var confirm = pendingConfirm && pendingConfirm.keyId === id ? pendingConfirm.type : null;
    if (confirm === "rotate") {
      return '<button id="rot-' + id + '" class="btn danger">Confirm rotate?</button> ' +
        '<button id="rotcancel-' + id + '" class="btn ghost">Cancel</button>' +
        '<div class="form-hint">Rotating issues a new key and immediately stops the old one. Update callers with the new key.</div>' +
        errSlot;
    }
    if (confirm === "revoke") {
      return '<button id="rev-' + id + '" class="btn danger">Confirm revoke?</button> ' +
        '<button id="revcancel-' + id + '" class="btn ghost">Cancel</button>' +
        '<div class="form-hint">Revoked keys stop authenticating immediately. This cannot be undone.</div>' +
        errSlot;
    }
    return '<button id="rot-' + id + '" class="btn ghost">Rotate</button> ' +
      '<button id="rev-' + id + '" class="btn danger">Revoke</button>' + errSlot;
  }

  function keyRow(k) {
    var id = String(k.id);
    var scopes = (k.scopes && k.scopes.length)
      ? k.scopes.map(function (s) { return '<span class="chip">' + escapeHtml(s) + "</span>"; }).join("")
      : "—";
    return '<tr id="keyrow-' + id + '" tabindex="0" role="button" aria-label="Key ending ' +
      escapeHtml(k.key_last4) + ' — open usage">' +
      '<td><span class="key-dot">••••</span> ' + escapeHtml(k.key_last4) + "</td>" +
      "<td>" + scopes + "</td>" +
      "<td>" + (k.spend_cap_usd === null || k.spend_cap_usd === undefined
        ? "Inherits tenant" : money(k.spend_cap_usd)) + "</td>" +
      "<td>" + statusBadge(k.status) + "</td>" +
      '<td class="muted-note">' + escDate(k.created_at) + "</td>" +
      '<td class="muted-note">' + (k.revoked_at ? escDate(k.revoked_at) : "—") + "</td>" +
      "<td>" + actionsInner(k) + "</td></tr>";
  }

  function createFormHtml() {
    return '<div class="card span12"><h3>Create proxy key</h3>' +
      '<div class="form-row"><label class="form-label" for="key-scopes">Scopes (comma-separated, optional)</label>' +
      '<input id="key-scopes" class="form-input" autocomplete="off" placeholder="e.g. chat, embeddings"></div>' +
      '<div class="form-row"><label class="form-label" for="key-cap">Spend cap USD (optional)</label>' +
      '<input id="key-cap" class="form-input" autocomplete="off" placeholder="e.g. 20.00"></div>' +
      '<button id="key-create-btn" class="btn">Create key</button> ' +
      '<button id="key-form-cancel" class="btn ghost">Cancel</button> ' +
      '<span class="inline-err" id="keyerr-create"></span></div>';
  }

  function revealPanelHtml() {
    return '<div class="card span12"><h3>' +
      (revealData.mode === "rotate" ? "Key rotated" : "Key created") + "</h3>" +
      '<p><span class="badge green">•••• ' + escapeHtml(revealData.last4) + "</span></p>" +
      '<p><code class="reveal-code" id="reveal-code">' + escapeHtml(revealData.key) + "</code></p>" +
      '<p><button id="copy-key" class="btn">Copy</button> <span id="copy-note" class="muted-note"></span></p>' +
      '<p class="inline-err">This key is shown once. Copy it now — it cannot be retrieved again.</p>' +
      '<button id="reveal-done" class="btn ghost">Done</button></div>';
  }

  function drawerHtml() {
    if (!drawerState) return "";
    var k = drawerState.key;
    var head = '<div class="card span12"><h3>Usage — <span class="key-dot">••••</span> ' +
      escapeHtml(k.key_last4) + "</h3>";
    if (drawerState.loading) return head + '<div class="skel-row"></div></div>';
    if (drawerState.error) {
      return head + '<div class="error-box">Couldn\'t load usage for this key.' +
        '<button id="drawer-retry" type="button">Retry</button></div></div>';
    }
    var ov = (drawerState.data && drawerState.data.overview) || {};
    return head + '<div class="grid">' +
      kpiCard("Requests", fmt(ov.requests), null, null, null) +
      kpiCard("Input tokens saved", fmt(ov.input_tokens_saved), null, null, null) +
      kpiCard("Cost saved", money(ov.cost_saved), null, null, null) +
      "</div>" +
      '<button id="drawer-close" class="btn ghost">Close</button></div>';
  }

  function tokenPromptHtml() {
    var msg = tokenPrompt.msg || "Printed once to the proxy's startup log at boot.";
    return '<div class="card span12"><h3>Admin token</h3>' +
      '<p class="form-hint">' + escapeHtml(msg) + "</p>" +
      '<div class="form-row"><input id="token-input" type="password" class="form-input" autocomplete="off" aria-label="Admin token"></div>' +
      '<button id="token-save" class="btn">Save</button></div>';
  }

  function renderKeysUI() {
    destroyCharts();
    if (!keysCtx) return;
    var t = keysCtx.tenant;
    var ov = (keysCtx.tkpis && keysCtx.tkpis.overview) || {};
    var keys = keysCtx.keys || [];
    var html = '<div id="token-slot">' + (tokenPrompt ? tokenPromptHtml() : "") + "</div>" +
      hostedBannerHtml([keysCtx.tenant]) +
      '<div class="grid">' +
      '<div class="card span6"><h3>Tenant</h3>' +
      '<div class="kpi-num">' + escapeHtml(t.name) + "</div>" +
      "<p>Plan: <span class=\"badge neutral\">" + escapeHtml(t.plan) + "</span><br>" +
      "Spend cap: " + (t.spend_cap_usd === null || t.spend_cap_usd === undefined
        ? "Uncapped" : money(t.spend_cap_usd)) + "<br>" +
      '<span class="muted-note">Created ' + escDate(t.created_at) + "</span></p></div>" +
      kpiCard("Requests (tenant)", fmt(ov.requests), null, null, null) +
      kpiCard("Cost saved (tenant)", money(ov.cost_saved), null, null, null) +
      '<div class="card span12"><h3>Proxy keys' +
      (keys.length ? '<span class="hdr-action"><button id="keys-create" class="btn">Create key</button></span>' : "") +
      "</h3>";
    if (!keys.length) {
      html += '<div class="empty"><h2>No proxy keys yet</h2>' +
        "<p>Create a key so your applications can authenticate to the proxy.</p>" +
        '<button id="keys-empty-create" class="btn">Create key</button></div>';
    } else {
      html += '<div class="table-scroll"><table><thead><tr><th>Key</th><th>Scopes</th><th>Spend cap</th><th>Status</th>' +
        "<th>Created</th><th>Revoked</th><th>Actions</th></tr></thead><tbody>" +
        keys.map(keyRow).join("") + "</tbody></table></div>";
    }
    html += "</div>" +
      '<div id="keys-inline">' +
      (revealData ? revealPanelHtml() : (keyFormOpen ? createFormHtml() : "")) +
      "</div>" +
      '<div id="drawer-slot">' + drawerHtml() + "</div>" +
      "</div>";
    content.innerHTML = html;
    bindKeysUI();
    bindHostedBanner();
  }

  function bindKeysUI() {
    var keys = (keysCtx && keysCtx.keys) || [];
    bindClick("keys-create", function () { keyFormOpen = true; renderKeysUI(); });
    bindClick("keys-empty-create", function () { keyFormOpen = true; renderKeysUI(); });
    if (revealData) {
      bindClick("reveal-done", function () {
        revealData = null;   // one-time reveal is dismissed for good (§4.2.4)
        keyFormOpen = false;
        loadKeys();
      });
      bindClick("copy-key", function () {
        var note = document.getElementById("copy-note");
        var fail = function () { if (note) note.textContent = "Copy failed — select the text manually."; };
        var done = function () {
          if (note) {
            note.textContent = "Copied";
            setTimeout(function () { note.textContent = ""; }, 2000);
          }
        };
        try {
          if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(revealData.key).then(done, fail);
          } else { fail(); }
        } catch (e) { fail(); }
      });
    } else if (keyFormOpen) {
      bindClick("key-create-btn", createKey);
      bindClick("key-form-cancel", function () { keyFormOpen = false; renderKeysUI(); });
    }
    if (tokenPrompt) bindClick("token-save", saveAdminToken);
    if (drawerState && drawerState.data) bindClick("drawer-close", function () { drawerState = null; renderKeysUI(); });
    if (drawerState && drawerState.error) bindClick("drawer-retry", function () { openKeyDrawer(drawerState.key); });
    keys.forEach(function (k) {
      var id = String(k.id);
      bindClick("keyrow-" + id, function (ev) {
        if (ev && ev.target && ev.target.tagName &&
            String(ev.target.tagName).toLowerCase() === "button") return;
        openKeyDrawer(k);
      });
      /* §6.4/I-9: Enter/Space on a focused row opens the drawer (keyboard
       * parity with the row's click target). */
      bindKeydown("keyrow-" + id, function (ev) {
        if (ev && (ev.key === "Enter" || ev.key === " ")) {
          if (ev.preventDefault) ev.preventDefault();
          openKeyDrawer(k);
        }
      });
      if (k.status !== "active") return;
      bindClick("rot-" + id, function () {
        if (pendingConfirm && pendingConfirm.keyId === id && pendingConfirm.type === "rotate") executeRotate(k);
        else setConfirm("rotate", id);
      });
      bindClick("rotcancel-" + id, cancelConfirm);
      bindClick("rev-" + id, function () {
        if (pendingConfirm && pendingConfirm.keyId === id && pendingConfirm.type === "revoke") executeRevoke(k);
        else setConfirm("revoke", id);
      });
      bindClick("revcancel-" + id, cancelConfirm);
    });
  }

  function bindClick(id, fn) {
    var el = document.getElementById(id);
    if (el && el.addEventListener) el.addEventListener("click", fn);
  }
  function bindKeydown(id, fn) {
    var el = document.getElementById(id);
    if (el && el.addEventListener) el.addEventListener("keydown", fn);
  }

  /* §7.5: an untouched two-tap confirm reverts after 5s, but the timer must
   * NOT fire while focus is inside the confirm group (pause-on-focus — a
   * keyboard user tabbing to Confirm must not have it vanish). Sub-900ms
   * scheduling keeps the visual deadline intact for pointer users. */
  var confirmFocusDepth = 0;
  function updateConfirmFocus(e) {
    var row = e && e.target && e.target.closest
      ? e.target.closest("tr, .setting-row, form") : null;
    var inConfirm = !!(row && row.querySelector &&
        row.querySelector('[id$="cancel-' + (pendingConfirm ? pendingConfirm.keyId : "__none__") + '"], [id^="rev-"], [id^="rot-"], [id="setrevert-"]'));
    if (e.type === "focusin" && inConfirm) confirmFocusDepth++;
    else if (e.type === "focusout" && inConfirm) confirmFocusDepth = Math.max(0, confirmFocusDepth - 1);
  }
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("focusin", updateConfirmFocus);
    document.addEventListener("focusout", updateConfirmFocus);
  }

  function confirmRevertTick(expected) {
    if (!pendingConfirm ||
        pendingConfirm.keyId !== expected.keyId ||
        pendingConfirm.type !== expected.type) return;
    if (confirmFocusDepth > 0) {
      confirmTimer = setTimeout(function () { confirmRevertTick(expected); }, 500);
      return;
    }
    confirmTimer = null;
    if (!onKeysTab()) return;
    pendingConfirm = null;
    renderKeysUI();
  }

  function setConfirm(type, keyId) {
    if (confirmTimer) clearTimeout(confirmTimer);
    pendingConfirm = { type: type, keyId: keyId };
    renderKeysUI();
    var expected = { type: type, keyId: keyId };
    confirmTimer = setTimeout(function () { confirmRevertTick(expected); }, 5000);
  }

  function cancelConfirm() {
    pendingConfirm = null;
    if (confirmTimer) { clearTimeout(confirmTimer); confirmTimer = null; }
    renderKeysUI();
  }

  function clearConfirm() {
    pendingConfirm = null;
    if (confirmTimer) { clearTimeout(confirmTimer); confirmTimer = null; }
  }

  /* Every write goes through here: bearer attached only when a token has been
   * entered (§4.5); 401 clears the token and prompts, preserving the pending
   * action; any other failure surfaces inline WITHOUT re-rendering (§4.4).
   * This core is tab-agnostic: `ok` returns its render-state and the caller's
   * `render()` paints it; `on401` restores whatever state the caller needs
   * preserved across the prompt (keys: the confirm; settings: the switch's
   * prior value). */
  function doWriteCore(url, opts, errId, ok, onFail, render, on401) {
    var headers = { "Content-Type": "application/json" };
    if (adminToken) headers["Authorization"] = "Bearer " + adminToken;
    return fetch(url, { method: opts.method || "POST", headers: headers,
                        body: JSON.stringify(opts.body || {}) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          if (r.status === 401) {
            var rejected = !!adminToken;
            adminToken = null;   // §4.5.3: any 401 clears the stored token
            tokenPrompt = {
              retry: function () { doWriteCore(url, opts, errId, ok, onFail, render, on401); },
              msg: rejected
                ? "Token rejected — the proxy may have restarted. Enter the current startup-log token."
                : null,
            };
            if (on401) on401();
            render();
            return null;
          }
          if (!r.ok) {
            writeError(errId, b && b.detail ? b.detail : "Request failed (" + r.status + ")");
            if (onFail) onFail();
            return null;
          }
          return typeof ok === "function" ? ok(b) : null;
        });
      })
      .catch(function () {
        writeError(errId, "Request failed — network error.");
        if (onFail) onFail();
      });
  }

  /* Keys-tab wrapper (§4.5.4): a 401 restores the pending rotate/revoke
   * confirm so the row still reads "Confirm rotate/revoke?" while the token
   * form is up; the operator re-enters the token once, not the whole flow. */
  function doWrite(url, body, errId, ok, onFail, confirm) {
    return doWriteCore(url, { method: "POST", body: body }, errId, ok, onFail,
      renderKeysUI,
      confirm ? function () { pendingConfirm = confirm; } : null);
  }

  function writeError(errId, msg) {
    var el = document.getElementById(errId);   // pre-rendered slot; no re-render (§4.4)
    if (el) el.textContent = msg;
  }

  function createKey() {
    var err = document.getElementById("keyerr-create");
    var btn = document.getElementById("key-create-btn");
    if (err) err.textContent = "";
    var cap = null;
    var capRaw = (document.getElementById("key-cap").value || "").trim();
    if (capRaw !== "") {
      cap = Number(capRaw);
      if (!isFinite(cap) || cap < 0) {
        if (err) err.textContent = "Spend cap must be a number.";
        return;
      }
    }
    var scopes = (document.getElementById("key-scopes").value || "").split(",")
      .map(function (s) { return s.trim(); }).filter(Boolean);
    if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }
    var restore = function () { if (btn) { btn.disabled = false; btn.textContent = "Create key"; } };
    doWrite("/api/keys",
      { tenant_id: keysCtx.tenant.id, scopes: scopes, spend_cap_usd: cap },
      "keyerr-create",
      function (b) {
        revealData = { key: b.key, last4: b.key_last4, mode: "create" };
        keyFormOpen = false;
        renderKeysUI();
        return b;
      },
      restore);
  }

  /* §4.5.4: the confirm is cleared only for the in-flight request — if the
   * write 401s, doWrite restores it so the pending action survives the
   * token re-prompt; non-401 failures keep the plain row (§4.4 inline error). */
  function executeRevoke(k) {
    clearConfirm();
    doWrite("/api/keys/" + k.id + "/revoke", {}, "keyerr-" + k.id,
      function () { loadKeys(); }, null, { type: "revoke", keyId: String(k.id) });
  }

  function executeRotate(k) {
    clearConfirm();
    doWrite("/api/keys/" + k.id + "/rotate", {}, "keyerr-" + k.id, function (b) {
      revealData = { key: b.key, last4: b.key_last4, mode: "rotate" };
      renderKeysUI();
      return b;
    }, null, { type: "rotate", keyId: String(k.id) });
  }

  function saveAdminToken() {
    var input = document.getElementById("token-input");
    var v = input ? input.value : "";
    if (!v) return;
    adminToken = v;          // §4.5.2: module variable only — never storage/URL/log
    var retry = tokenPrompt && tokenPrompt.retry;
    tokenPrompt = null;
    renderKeysUI();
    if (retry) retry();
  }

  function openKeyDrawer(k) {
    drawerState = { key: k, loading: true };
    renderKeysUI();
    keysFetch(keysKpisUrl("api_key_id=" + encodeURIComponent(k.id)))
      .then(function (d) { drawerState = { key: k, data: d }; renderKeysUI(); })
      .catch(function () { drawerState = { key: k, error: true }; renderKeysUI(); });
  }

  // ---------------- Settings tab (V2.2 §6.5) ----------------
  /* The Settings tab is NOT bucket-scoped and is NOT gated by the zero-
   * traffic empty state — a fresh deployment with zero traffic must still be
   * able to open Settings and flip controls (§5.3). Three independent
   * fetches (/api/settings, /api/strategies, /api/tripwire) feed their own
   * cards with their own skeleton/error/retry; one failing endpoint never
   * blanks the tab (I-10). Values always render from the SERVER RESPONSE,
   * never from the click (pessimistic writes, §5.2). */

  var settingsState = null;       // {runtime:[], deploy:[], updatedAt:{}, banners:[tenants]}
  var strategiesState = null;     // {data:[...]} | {error:true} | {unauthorized:true}
  var tripwireState = null;       // {data:{...}} | {error:true}
  var settingSaveState = {};      // name -> {saving, saved, error, value, source, locked, revertConfirm}
  var saveNoteTimers = {};
  var settingsConfirmTimer = null;

  var RUNTIME_CONTROLS = [
    ["l1_enabled", "L1 structural cleanup",
     "Lossless deterministic cleanup of coding-agent context, runs before caching.",
     "Saves tokens on structural noise; an entire-message pretty-printed JSON body gets whitespace-compacted (documented caveat)."],
    ["tool_schema_minify", "Tool-schema minification",
     "Minifies validated tool schemas before forwarding.",
     "Lossless on validated schemas; unvalidatable schemas forward verbatim."],
    ["tool_schema_cache_enabled", "Tool-schema cache",
     "Reuses minified schemas across requests; subordinate to the master cache switch.",
     "No wire-shape risk; disabling costs repeat tokens on schema-heavy traffic."],
    ["tool_result_optimization", "Tool-result optimization",
     "Filters/truncates completed tool results before upstream forwarding.",
     "Output-side only; assistant envelopes stay byte-preserved."],
    ["tool_result_compression_enabled", "Tool-result compression",
     "Lossless compression for the tool-result class only.",
     "Savings on verbose tool output; never touches assistant message envelopes."],
    ["output_conciseness_enabled", "Output conciseness",
     "Injects a conciseness instruction on long user prompts.",
     "Benchmarked NET-NEGATIVE on short prompts; a per-request header override keeps precedence over this default."],
    ["semantic_cache_enabled", "Semantic cache",
     "Serves repeated-meaning prompts from the vector cache.",
     "Gated: writable only while the AC-PC4/PC5 calibration gate is green."],
  ];

  function sourceChip(source) {
    var cls = source === "runtime" ? "chip source-runtime" : "chip source-" + source;
    return '<span class="' + cls + '">' + escapeHtml(source) + "</span>";
  }

  function saveNoteHtml(name) {
    var st = settingSaveState[name];
    if (!st) return "";
    if (st.saving) return "Saving…";
    if (st.error) return '<span class="inline-err">' + escapeHtml(st.error) + "</span>";
    if (st.saved) return "Saved — effective immediately";
    return "";
  }

  /* §6.5.2: one row per runtime item, in API order. The switch state is the
   * item's `value`; the source chip is verbatim `source`; `updated_at` (when
   * present) renders muted as "overridden YYYY-MM-DD". A server-supplied
   * `locked_reason` renders the control disabled + `locked` badge + verbatim
   * reason (I-6 — the UI never derives lock state). */
  function runtimeRowHtml(item) {
    var meta = null;
    for (var i = 0; i < RUNTIME_CONTROLS.length; i++) {
      if (RUNTIME_CONTROLS[i][0] === item.name) { meta = RUNTIME_CONTROLS[i]; break; }
    }
    var label = meta ? meta[1] : item.name;
    var desc = meta ? meta[2] : "";
    var impact = meta ? meta[3] : "";
    var st = settingSaveState[item.name] || {};
    var value = st.value !== undefined ? st.value : item.value;
    var source = st.source !== undefined ? st.source : item.source;
    var locked = st.locked !== undefined ? st.locked : (item.locked_reason ? item.locked_reason : null);
    var disabled = st.saving || !!locked;
    var errId = "seterr-" + item.name;
    var html = '<div class="setting-row">' +
      '<label class="switch">' +
      '<input type="checkbox" role="switch" id="setsw-' + item.name + '"' +
      (value ? " checked" : "") + (disabled ? " disabled" : "") +
      ' aria-label="' + escapeHtml(label) + '">' +
      '<span class="track"></span><span class="thumb"></span></label>' +
      '<div class="setting-body">' +
      '<div class="setting-label">' + escapeHtml(label) +
      ' <span class="badge ' + (value ? "green" : "neutral") + '">' +
      (value ? "on" : "off") + "</span>" + sourceChip(source) +
      (locked ? '<span class="badge neutral">locked</span>' : "") +
      (source === "runtime" && !locked
        ? (st.revertConfirm
          ? '<button id="setrevert-' + item.name + '" class="btn danger">Confirm revert?</button> ' +
            '<button id="setrevertcancel-' + item.name + '" class="btn ghost">Cancel</button>'
          : '<button id="setrevert-' + item.name + '" class="btn ghost">Revert</button>')
        : "") +
      "</div>" +
      (desc ? '<div class="setting-desc">' + escapeHtml(desc) + "</div>" : "") +
      (impact ? '<div class="setting-impact">' + escapeHtml(impact) + "</div>" : "") +
      (locked
        ? '<div class="setting-impact">' + escapeHtml(locked) +
          ' <span class="muted-note">Gate status: see Pipeline health below.</span></div>'
        : "") +
      (st.revertConfirm
        ? '<div class="form-hint">Reverting removes the runtime override; the setting returns to its env/default value.</div>'
        : "") +
      (item.updated_at
        ? '<div class="muted-note">overridden ' + escDate(item.updated_at) + "</div>"
        : "") +
      '<div class="save-note" id="savenote-' + item.name + '">' + saveNoteHtml(item.name) + "</div>" +
      '<span class="inline-err" id="' + errId + '"></span>' +
      "</div></div>";
    return html;
  }

  /* §6.5.3: deployment-only inventory. Values whose name contains
   * token/secret/key render •••• regardless of the payload (component-level
   * redaction, I-7). Booleans render on/off neutral badges; dicts/lists
   * render monospace one-line JSON; strings/numbers monospace. */
  function deployRowHtml(item) {
    var name = String(item.name || "");
    var value = item.value;
    var secretish = /token|secret|key/i.test(name);
    var rendered;
    if (secretish) {
      rendered = '<span class="key-dot">••••</span>' +
        (name === "admin_token"
          ? ' <span class="muted-note">managed at boot</span>' : "");
    } else if (typeof value === "boolean") {
      rendered = '<span class="badge neutral onoff">' + (value ? "on" : "off") + "</span>";
    } else if (value === null || value === undefined) {
      rendered = '<span class="muted-note">—</span>';
    } else if (typeof value === "object") {
      rendered = '<span class="mono value-scroll">' + escapeHtml(JSON.stringify(value)) + "</span>";
    } else {
      rendered = '<span class="mono">' + escapeHtml(String(value)) + "</span>";
    }
    return "<tr><td class=\"mono\">" + escapeHtml(name) + "</td><td>" + rendered + "</td></tr>";
  }

  function adminTokenCardHtml() {
    if (tokenPrompt) {
      return '<div class="card span12"><h3>Admin token</h3>' +
        '<p class="form-hint">' + escapeHtml(tokenPrompt.msg ||
          "The admin token is set at proxy boot — printed once to the startup log when not configured via env. Paste it here to change settings. It is kept in memory only and never stored.") + "</p>" +
        '<div class="form-row"><input id="token-input" type="password" class="form-input" autocomplete="off" aria-label="Admin token"></div>' +
        '<button id="token-save" class="btn">Save</button></div>';
    }
    if (adminToken) {
      return '<div class="card span12"><h3>Admin token</h3>' +
        '<p class="form-hint">Admin token entered for this session.</p>' +
        '<button id="token-forget" class="btn ghost">Forget token</button></div>';
    }
    return '<div class="card span12"><h3>Admin token</h3>' +
      '<p class="form-hint">The admin token is set at proxy boot — printed once to the startup log when not configured via env. Paste it here to change settings. It is kept in memory only and never stored.</p>' +
      '<div class="form-row"><input id="token-input" type="password" class="form-input" autocomplete="off" aria-label="Admin token"></div>' +
      '<button id="token-save" class="btn">Save</button></div>';
  }

  /* §6.5.4 Card A — strategy flags. The G1 banner is permanent (non-
   * dismissible): the V2.1 lanes govern benchmark harnesses only until
   * their flags are enabled, which requires authenticated tenant/session
   * scope (proxy key + X-Session-Id) on live traffic. */
  function strategiesCardHtml() {
    var head = '<div class="card span6"><h3>Strategy flags</h3>' +
      '<div class="banner warn">V2.1 strategy lanes (deferred_tools, tocp, idcp, atba, mtcc) ' +
      "govern benchmark harnesses only. Enabling them on live traffic requires " +
      "authenticated scope: a proxy key (Bearer tsk_…) plus an X-Session-Id header. " +
      "Without both, live requests are unaffected.</div>";
    if (!strategiesState) {
      return head + '<div class="skel-row"></div></div>';
    }
    if (strategiesState.error) {
      return head + '<div class="error-box">Couldn\'t load strategy flags.' +
        '<button id="strategies-retry" type="button">Retry</button></div></div>';
    }
    if (strategiesState.unauthorized) {
      return head +
        '<p class="form-hint">Enter the admin token to view strategy flags.</p>' +
        '<div class="form-row"><input id="token-input" type="password" class="form-input" autocomplete="off" aria-label="Admin token"></div>' +
        '<button id="token-save" class="btn">Save</button></div>';
    }
    var rows = (strategiesState.data || []).map(function (s) {
      return "<tr><td>" + escapeHtml(s.strategy) + "</td><td>" + escapeHtml(s.version) + "</td>" +
        "<td>" + (s.flag_enabled
          ? '<span class="badge green">on</span>'
          : '<span class="badge neutral">off</span>') + "</td>" +
        "<td>" + (s.default_off ? '<span class="badge neutral">default_off</span>' : "") +
          (s.enforcement_enabled ? ' <span class="badge green">enforcing</span>' : "") + "</td>" +
        '<td class="muted-note">' + escapeHtml(s.fallback) + "</td></tr>";
    }).join("");
    return head +
      '<div class="table-scroll"><table><thead><tr><th>Strategy</th><th>Version</th><th>Flag</th>' +
      "<th>Default-off</th><th>Fallback</th></tr></thead><tbody>" + rows +
      "</tbody></table></div></div>";
  }

  /* §6.5.4 Card B — tripwire. All status strings render VERBATIM (I-4):
   * unknown future statuses render as neutral badges with the raw text. The
   * flagged count is the array length of `flagged` (sanctioned array math). */
  function tripwireBadge(status) {
    var cls = "neutral";
    if (status === "green" || status === "clear") cls = "green";
    else if (status === "red" || status === "alert") cls = "red";
    else if (status === "pending" || status === "pending_calibration" ||
             status === "pending_metric_ruling" ||
             status === "insufficient_live_rows" ||
             status === "semantic_threshold_miss") cls = "gold";
    else if (status === "not_applicable_grounded_off") cls = "neutral";
    return '<span class="badge ' + cls + '">' + escapeHtml(status) + "</span>";
  }

  function tripwireRuleRow(label, rule) {
    var flagged = (rule.flagged || []).length;
    return '<div class="subrow"><span>' + label + "</span>" +
      tripwireBadge(rule.status) +
      ' <span class="muted-note">' + flagged + " flagged rows</span></div>";
  }

  function tripwireCardHtml() {
    var head = '<div class="card span6"><h3>Tripwire</h3>';
    if (!tripwireState) {
      return head + '<div class="skel-row"></div></div>';
    }
    if (tripwireState.error) {
      return head + '<div class="error-box">Couldn\'t load tripwire status.' +
        '<button id="tripwire-retry" type="button">Retry</button></div></div>';
    }
    var t = tripwireState.data || {};
    var html = head +
      '<div class="subrow"><span>Status</span>' + tripwireBadge(t.status || "unknown") + "</div>" +
      (t.dose_drift ? tripwireRuleRow("Dose drift", t.dose_drift) : "") +
      (t.missed_grounding ? tripwireRuleRow("Missed grounding", t.missed_grounding) : "");
    if (t.rows_scanned !== null && t.rows_scanned !== undefined) {
      html += '<div class="muted-note">' + t.rows_scanned + " rows scanned</div>";
    }
    if (t.calibration_artifact) {
      html += '<div class="muted-note">calibration: <span class="mono">' +
        escapeHtml(t.calibration_artifact) + "</span></div>";
    }
    return html + "</div>";
  }

  function renderSettingsUI() {
    destroyCharts();
    if (!settingsState) return;
    var html = '<div id="token-slot">' + adminTokenCardHtml() + "</div>" +
      hostedBannerHtml(settingsState.tenants || []) +
      '<h2 class="section-title" tabindex="-1">Runtime controls</h2>' +
      '<p class="section-note">Changes persist across restarts and take effect without one. ' +
      "Per-request benchmark headers always win.</p>" +
      '<div class="card span12">' +
      (settingsState.runtime.length
        ? settingsState.runtime.map(runtimeRowHtml).join("")
        : '<div class="error-box">Settings endpoint returned no items — the proxy may be running a pre-V2.2 backend.</div>') +
      "</div>" +
      '<h2 class="section-title">Deployment configuration (read-only)</h2>' +
      '<p class="section-note">Set via environment at boot. Changing these requires a redeploy; ' +
      "they are never writable from this UI. Values are env/default-sourced.</p>" +
      '<div class="card span12"><div class="table-scroll"><table><thead>' +
      "<tr><th>Setting</th><th>Value</th></tr></thead><tbody>" +
      settingsState.deploy.map(deployRowHtml).join("") +
      "</tbody></table></div></div>" +
      '<h2 class="section-title">Pipeline health</h2>' +
      '<div class="grid">' +
      strategiesCardHtml() +
      tripwireCardHtml() +
      "</div>";
    content.innerHTML = html;
    bindSettingsUI();
    bindHostedBanner();
  }

  function applyItemToState(item) {
    for (var i = 0; i < settingsState.runtime.length; i++) {
      if (settingsState.runtime[i].name === item.name) {
        settingsState.runtime[i] = item;
        break;
      }
    }
    var st = settingSaveState[item.name];
    if (st) {
      st.value = item.value;
      st.source = item.source;
      st.locked = item.locked_reason || null;
    }
  }

  /* §5.2 settings write lifecycle: flip → disabled + Saving… → PUT → the
   * RENDERED value comes from the server response (pessimistic). 400 reverts
   * the switch to the prior value with the field-named message inline; 401
   * prompts and replays (pending action preserved, I-8); 503 reverts with a
   * Retry-scoped inline error. */
  function flipSetting(name) {
    var item = null;
    for (var i = 0; i < settingsState.runtime.length; i++) {
      if (settingsState.runtime[i].name === name) { item = settingsState.runtime[i]; break; }
    }
    if (!item) return;
    var priorValue = !!item.value;
    var nextValue = !priorValue;
    var st = settingSaveState[name] = settingSaveState[name] || {};
    st.saving = true;
    st.error = null;
    st.saved = false;
    st.revertConfirm = false;
    renderSettingsUI();
    doWriteCore("/api/settings/" + name, { method: "PUT", body: { value: nextValue } },
      "seterr-" + name,
      function (b) {          // 200: render the server-returned effective item
        st.saving = false;
        st.saved = true;
        applyItemToState(b);
        renderSettingsUI();
        if (saveNoteTimers[name]) clearTimeout(saveNoteTimers[name]);
        saveNoteTimers[name] = setTimeout(function () {
          if (settingSaveState[name]) settingSaveState[name].saved = false;
          if (currentTab() === "settings" && settingsState) renderSettingsUI();
        }, 2000);
      },
      function () {           // non-401 failure: revert to prior value
        st.saving = false;
        st.value = priorValue;
        st.source = item.source;
        st.locked = item.locked_reason || null;
        renderSettingsUI();
      },
      renderSettingsUI,
      function () {           // 401: pending PUT replays; keep prior value
        st.saving = false;
        st.value = priorValue;
      });
  }

  function revertSetting(name) {
    var st = settingSaveState[name] = settingSaveState[name] || {};
    st.saving = true;
    st.revertConfirm = false;
    st.error = null;
    renderSettingsUI();
    doWriteCore("/api/settings/" + name, { method: "DELETE", body: {} },
      "seterr-" + name,
      function (b) {
        st.saving = false;
        st.saved = true;
        applyItemToState(b);
        renderSettingsUI();
      },
      function () {
        st.saving = false;
        renderSettingsUI();
      },
      renderSettingsUI,
      function () {
        st.saving = false;
        st.revertConfirm = true;   // confirm state survives the prompt (I-8)
      });
  }

  function armRevert(name) {
    var st = settingSaveState[name] = settingSaveState[name] || {};
    st.revertConfirm = true;
    renderSettingsUI();
    if (settingsConfirmTimer) clearTimeout(settingsConfirmTimer);
    settingsConfirmTimer = setTimeout(function () {
      settingsConfirmTimer = null;
      if (currentTab() !== "settings") return;
      if (settingSaveState[name] && settingSaveState[name].revertConfirm) {
        settingSaveState[name].revertConfirm = false;
        renderSettingsUI();
      }
    }, 5000);
  }

  function disarmRevert(name) {
    if (settingsConfirmTimer) { clearTimeout(settingsConfirmTimer); settingsConfirmTimer = null; }
    if (settingSaveState[name]) settingSaveState[name].revertConfirm = false;
    renderSettingsUI();
  }

  function bindSettingsUI() {
    settingsState.runtime.forEach(function (item) {
      var st = settingSaveState[item.name] || {};
      if (!st.locked && !item.locked_reason && !st.saving) {
        bindClick("setsw-" + item.name, function () { flipSetting(item.name); });
      }
      if (st.revertConfirm) {
        bindClick("setrevert-" + item.name, function () { revertSetting(item.name); });
        bindClick("setrevertcancel-" + item.name, function () { disarmRevert(item.name); });
      } else {
        bindClick("setrevert-" + item.name, function () { armRevert(item.name); });
      }
    });
    if (tokenPrompt || (strategiesState && strategiesState.unauthorized)) {
      bindClick("token-save", saveAdminTokenSettings);
    }
    bindClick("token-forget", function () {
      adminToken = null;   // clears the module variable only (§8.2.3)
      renderSettingsUI();
    });
    bindClick("strategies-retry", function () { loadStrategiesCard(); });
    bindClick("tripwire-retry", function () { loadTripwireCard(); });
  }

  /* Token save on the Settings tab: replay a pending write if one exists,
   * otherwise re-fetch the strategies card (a 401 on /api/strategies renders
   * a card-local prompt — §6.5.4 — that clears once the bearer works). */
  function saveAdminTokenSettings() {
    var input = document.getElementById("token-input");
    var v = input ? input.value : "";
    if (!v) return;
    adminToken = v;        // §4.5.2: module variable only
    var retry = tokenPrompt && tokenPrompt.retry;
    var wasStrategies401 = !!(strategiesState && strategiesState.unauthorized);
    tokenPrompt = null;
    renderSettingsUI();
    if (retry) retry();
    if (wasStrategies401) loadStrategiesCard();
  }

  function loadSettingsTab() {
    settingsState = null;
    strategiesState = null;
    tripwireState = null;
    content.innerHTML = '<div class="skel-row"></div><div class="skel-row"></div><div class="skel-row short" style="width:60%"></div>';
    fetchJson("/api/settings").then(function (body) {
      var items = (body && body.settings) || [];
      var runtime = items.filter(function (i) { return i.category === "runtime_configurable"; });
      var deploy = items.filter(function (i) { return i.category === "deployment_only"; });
      settingsState = { runtime: runtime, deploy: deploy, tenants: [] };
      renderSettingsUI();
      // the G2 banner needs the tenant plan; it degrades silently to absent
      fetchJson("/api/tenants").then(function (ts) {
        if (settingsState) {
          settingsState.tenants = ts || [];
          if (currentTab() === "settings") renderSettingsUI();
        }
      }).catch(function () {});
      loadStrategiesCard();
      loadTripwireCard();
    }).catch(function () {
      settingsState = { runtime: [], deploy: [], tenants: [] };
      renderSettingsUI();
    });
  }

  function loadStrategiesCard() {
    /* /api/strategies is ADMIN_TOKEN-gated — the only READ that carries the
     * bearer (PM §1.1); a 401 degrades just this card to a local prompt. */
    var headers = {};
    if (adminToken) headers["Authorization"] = "Bearer " + adminToken;
    return fetch("/api/strategies", { headers: headers }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        if (!r.ok) {
          var e = new Error(b && b.detail ? b.detail : "bad status " + r.status);
          e.status = r.status;
          throw e;
        }
        strategiesState = { data: (b && b.strategies) || [] };
        if (currentTab() === "settings" && settingsState) renderSettingsUI();
      });
    }).catch(function (e) {
      strategiesState = e && e.status === 401 ? { unauthorized: true } : { error: true };
      if (currentTab() === "settings" && settingsState) renderSettingsUI();
    });
  }

  function loadTripwireCard() {
    fetchJson("/api/tripwire").then(function (body) {
      tripwireState = { data: body || {} };
      if (currentTab() === "settings" && settingsState) renderSettingsUI();
    }).catch(function () {
      tripwireState = { error: true };
      if (currentTab() === "settings" && settingsState) renderSettingsUI();
    });
  }

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
    var tab = currentTab();
    // C6 (§4.1): the keys tab's states are its own (skel/error/"No proxy keys
    // yet") — a zero-traffic KPI window must not hijack it with emptyState().
    if (tab === "keys") { loadKeys(); return; }
    if (!d.overview || !d.overview.requests) { emptyState(); return; }
    if (tab === "overview") renderOverview(d);
    else if (tab === "traffic") renderTraffic(d);
    else renderOverview(d);
  }

  /* V2.2 routing (§4.1, §5.3): hash change re-renders the active nav anchor,
   * hides the bucket selector on the Settings tab (its data is not bucket-
   * scoped — a dead control is a disconnected-toggle violation), and moves
   * focus into `#content` so screen-reader users land on the new content
   * (I-9 §7.3). Unknown hashes fall back to overview. */
  function load() {
    var tab = currentTab();
    var selEl = document.getElementById("bucket-sel");
    if (selEl) selEl.style.display = (tab === "settings") ? "none" : "";
    if (tab === "settings") { loadSettingsTab(); return; }
    if (tab === "providers") { loadProvidersTab(); return; }
    content.innerHTML = '<div class="skel-row"></div><div class="skel-row"></div><div class="skel-row short" style="width:60%"></div>';
    fetchKpis().then(render).catch(showError);
  }

  function focusContent(e) {
    if (!e) return;
    var el = e.querySelector ? e.querySelector("h2, h3") : null;
    if (el) {
      if (!el.getAttribute || !el.getAttribute("tabindex")) el.tabIndex = -1;
      if (el.focus) el.focus();
    } else if (e.focus) e.focus();
  }

  window.addEventListener("hashchange", function () {
    var tab = currentTab();
    var valid = { overview: 1, traffic: 1, providers: 1, keys: 1, settings: 1 };
    var navLinks = document.querySelectorAll ? document.querySelectorAll("nav a") : [];
    for (var i = 0; i < navLinks.length; i++) {
      var a = navLinks[i];
      var wanted = (valid[tab] ? tab : "overview");
      if (a.classList && a.getAttribute && a.getAttribute("data-tab")) {
        if (a.getAttribute("data-tab") === wanted) a.classList.add("active");
        else a.classList.remove("active");
      }
    }
    load();
    focusContent(content);
  });

  // bucket selector (client-side control, not aggregation)
  var sel = document.createElement("select");
  sel.id = "bucket-sel";
  sel.setAttribute("aria-label", "KPI time bucket");
  sel.innerHTML = '<option value="minute">Minute</option><option value="hour">Hour</option><option value="day" selected>Day</option>';
  sel.addEventListener("change", function () { state.bucket = sel.value; load(); });
  var bucketSlot = document.getElementById("bucket-slot");
  if (bucketSlot && bucketSlot.appendChild) bucketSlot.appendChild(sel);

  load();
})();
