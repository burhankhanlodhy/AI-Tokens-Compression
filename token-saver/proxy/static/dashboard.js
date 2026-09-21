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
      cacheStatusCard(cacheOf(d)) +
      "</div>";
    content.innerHTML = html;
    lineChart("c-req", labels, d.series.map(function (s) { return s.requests; }), "requests", "#4f8cff");
    lineChart("c-err", labels, d.series.map(function (s) { return s.errors; }), "errors", "#e5484d");
  }

  function renderProviders(d) {
    var rows = d.by_provider.map(function (p) {
      var errBadge = p.error_pct > 0 ? '<span class="badge red">' + p.error_pct + "%</span>"
                                     : '<span class="badge green">0%</span>';
      var l1Line = p.l1_tokens_stripped
        ? "L1 structural: <strong>" + fmt(p.l1_tokens_stripped) + " tokens</strong>, " +
          money(p.l1_cost_saved) + ' <span class="muted-note">portion of cost saved</span>'
        : 'L1 structural: <span class="zero-dash">—</span>';
      return '<div class="card span6"><h3>' + p.provider + "</h3>" +
        '<div class="kpi-num">' + fmt(p.requests) + ' <span style="font-size:.9rem;color:var(--muted)">requests</span></div>' +
        "<p>Tokens saved: <strong>" + fmt(p.tokens_saved) + "</strong><br>" +
        "Cost saved: <strong>" + money(p.cost_saved) + "</strong><br>" +
        l1Line + "<br>" +
        "Cache hits: <strong>" + p.cache_hits + "</strong> (" + p.cache_hit_pct + "%) " +
        "Errors: " + errBadge +
        "</p></div>";
    });
    content.innerHTML = '<div class="grid">' +
      (rows.length ? rows.join("") : '<div class="card span12"><div class="empty">No provider traffic yet.</div></div>') +
      "</div>";
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

  function keysFetch(url) {
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

  /* Scoped KPI url: selector first so a filtered url is distinguishable from
   * the unscoped /api/kpis?bucket=… fetch the other tabs make. */
  function keysKpisUrl(extra) {
    var url = "/api/kpis?" + extra + "&bucket=" + state.bucket;
    if (state.from) url += "&from=" + encodeURIComponent(state.from);
    if (state.to) url += "&to=" + encodeURIComponent(state.to);
    return url;
  }

  function onKeysTab() {
    return (location.hash.replace("#", "") || "overview") === "keys";
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
    return '<tr id="keyrow-' + id + '">' +
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
      '<div class="form-row"><input id="token-input" type="password" class="form-input" autocomplete="off"></div>' +
      '<button id="token-save" class="btn">Save</button></div>';
  }

  function renderKeysUI() {
    destroyCharts();
    if (!keysCtx) return;
    var t = keysCtx.tenant;
    var ov = (keysCtx.tkpis && keysCtx.tkpis.overview) || {};
    var keys = keysCtx.keys || [];
    var html = '<div id="token-slot">' + (tokenPrompt ? tokenPromptHtml() : "") + "</div>" +
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
      html += "<table><thead><tr><th>Key</th><th>Scopes</th><th>Spend cap</th><th>Status</th>" +
        "<th>Created</th><th>Revoked</th><th>Actions</th></tr></thead><tbody>" +
        keys.map(keyRow).join("") + "</tbody></table>";
    }
    html += "</div>" +
      '<div id="keys-inline">' +
      (revealData ? revealPanelHtml() : (keyFormOpen ? createFormHtml() : "")) +
      "</div>" +
      '<div id="drawer-slot">' + drawerHtml() + "</div>" +
      "</div>";
    content.innerHTML = html;
    bindKeysUI();
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

  function setConfirm(type, keyId) {
    if (confirmTimer) clearTimeout(confirmTimer);
    pendingConfirm = { type: type, keyId: keyId };
    renderKeysUI();
    confirmTimer = setTimeout(function () {   // §4.3: untouched confirm reverts after 5s
      confirmTimer = null;
      if (!onKeysTab()) return;
      if (pendingConfirm && pendingConfirm.keyId === keyId && pendingConfirm.type === type) {
        pendingConfirm = null;
        renderKeysUI();
      }
    }, 5000);
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
   * action; any other failure surfaces inline WITHOUT re-rendering (§4.4). */
  function doWrite(url, body, errId, ok, onFail) {
    var headers = { "Content-Type": "application/json" };
    if (adminToken) headers["Authorization"] = "Bearer " + adminToken;
    return fetch(url, { method: "POST", headers: headers, body: JSON.stringify(body || {}) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          if (r.status === 401) {
            var rejected = !!adminToken;
            adminToken = null;   // §4.5.3: any 401 clears the stored token
            tokenPrompt = {
              retry: function () { doWrite(url, body, errId, ok, onFail); },
              msg: rejected
                ? "Token rejected — the proxy may have restarted. Enter the current startup-log token."
                : null,
            };
            renderKeysUI();
            return null;
          }
          if (!r.ok) {
            writeError(errId, b && b.detail ? b.detail : "Request failed (" + r.status + ")");
            if (onFail) onFail();
            return null;
          }
          return ok(b);
        });
      })
      .catch(function () {
        writeError(errId, "Request failed — network error.");
        if (onFail) onFail();
      });
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

  function executeRevoke(k) {
    clearConfirm();
    doWrite("/api/keys/" + k.id + "/revoke", {}, "keyerr-" + k.id, function () { loadKeys(); });
  }

  function executeRotate(k) {
    clearConfirm();
    doWrite("/api/keys/" + k.id + "/rotate", {}, "keyerr-" + k.id, function (b) {
      revealData = { key: b.key, last4: b.key_last4, mode: "rotate" };
      renderKeysUI();
      return b;
    });
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
    var tab = location.hash.replace("#", "") || "overview";
    // C6 (§4.1): the keys tab's states are its own (skel/error/"No proxy keys
    // yet") — a zero-traffic KPI window must not hijack it with emptyState().
    if (tab === "keys") { loadKeys(); return; }
    if (!d.overview || !d.overview.requests) { emptyState(); return; }
    if (tab === "overview") renderOverview(d);
    else if (tab === "traffic") renderTraffic(d);
    else if (tab === "providers") renderProviders(d);
    else if (tab === "keys") loadKeys();
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
