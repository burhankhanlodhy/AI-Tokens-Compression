/* Node harness for the dashboard render gates (test_dashboard_render.py,
 * test_ac_a8_dashboard.py, test_keys_tab_render.py): executes
 * proxy/static/dashboard.js (an IIFE expecting a browser) against a minimal
 * DOM stub + stubbed fetch, then dumps the rendered tab HTML, captured fetch
 * traffic, and Chart configs as JSON.
 *
 * Usage: node dashboard_render_harness.js <fixture.json> [tab] [mode]
 *   tab  = overview|traffic|providers|keys  (default: overview)
 *   mode = "fail-fetch" — every fetch resolves !ok (503) to exercise the
 *          error state; the harness then clicks the rendered Retry button and
 *          reports each fetch URL so the test can assert the refire.
 *
 * Fixture extensions (all optional, backward compatible):
 *   _routes  — map of route key → {status?, body} | [step, ...] (a sequence:
 *              each call to that route consumes the next step, repeating the
 *              last one; used for 401-then-OK write flows). A key with a
 *              space ("POST /api/keys") matches that method only. A key
 *              containing "?" matches as a substring of the full URL
 *              (e.g. "/api/kpis?tenant_id="); otherwise it matches the path
 *              exactly. Query-style keys are checked before path keys.
 *   _script  — list of steps executed after the first render, in order:
 *              {"click": "<element id>"} | {"set": ["<id>", "<value>"]} |
 *              {"wait": <ms>}. Element ids resolve through getElementById,
 *              exactly as the page's own bindings do.
 *
 * DOM semantics mirrored from a real browser: setting any element's
 * innerHTML resets the element registry, so re-renders rebind fresh
 * elements (no stale/duplicate listeners), matching innerHTML replacement.
 * Output keys: tab, mode, url (first fetch), urls, writes (non-GET with
 * method+headers, for the §4.5 bearer gates), html, charts.
 */
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const fixturePath = process.argv[2];
const tab = process.argv[3] || "overview";
const mode = process.argv[4] || "";
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));

function stubEl(tag) {
  const el = {
    tagName: tag,
    style: {},
    listeners: {},
    value: "",
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    trigger(type) { (this.listeners[type] || []).forEach((fn) => fn()); },
    insertBefore() {},
    setAttribute() {},
    appendChild() {},
  };
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    configurable: true,
    get() { return html; },
    set(v) { html = String(v === null || v === undefined ? "" : v); resetElements(); },
  });
  // textContent writes (e.g. §4.4 inline error slots) are recorded so the
  // dump can assert them — the surrounding innerHTML is untouched by design.
  Object.defineProperty(el, "textContent", {
    configurable: true,
    get() { return el._text || ""; },
    set(v) {
      el._text = String(v === null || v === undefined ? "" : v);
      if (el.id && el._text) global.__texts[el.id] = el._text;
    },
  });
  return el;
}

const contentEl = stubEl("div");
contentEl.id = "content";
let elements = { content: contentEl };
function resetElements() { elements = { content: contentEl }; }

global.document = {
  getElementById(id) {
    if (!elements[id]) { elements[id] = stubEl("div"); elements[id].id = id; }
    return elements[id];
  },
  createElement(tag) { return stubEl(tag); },
  querySelector() { return stubEl("nav"); },
};

global.location = { hash: "#" + tab };
global.window = { addEventListener() {}, location: global.location };

global.__charts = [];
global.__texts = {};   // id → last textContent write (§4.4 inline errors)
global.Chart = function (el, cfg) {
  global.__charts.push({ id: el && el.id, cfg: cfg });
  return { destroy() {} };
};

const captured = [];   // {url, method, headers}
const seqCount = {};   // route key → calls consumed from a _routes sequence

function routeFor(url, method) {
  if (mode === "fail-fetch") return { status: 503, body: {} };
  const routes = fixture._routes;
  if (!routes) return { status: 200, body: fixture };
  const urlStr = String(url);
  const pathOnly = urlStr.split("?")[0];
  const keys = Object.keys(routes);
  const isQueryKey = (k) => k.indexOf("?") >= 0;
  const hasMethod = (k) => k.indexOf(" ") > 0 && /^(GET|POST)$/.test(k.slice(0, k.indexOf(" ")));
  // precedence: method-specific, then query-style (substring) so
  // "/api/kpis?tenant_id=" beats "/api/kpis", then plain path keys — so a
  // "POST /api/keys" route is never shadowed by the "/api/keys" read route.
  const passes = [keys.filter(hasMethod), keys.filter((k) => !hasMethod(k) && isQueryKey(k)),
                  keys.filter((k) => !hasMethod(k) && !isQueryKey(k))];
  for (const pass of passes) {
    for (const key of pass) {
      let m = key;
      let wantMethod = null;
      const sp = key.indexOf(" ");
      if (sp > 0 && /^(GET|POST)$/.test(key.slice(0, sp))) {
        wantMethod = key.slice(0, sp);
        m = key.slice(sp + 1);
      }
      if (wantMethod && wantMethod !== method) continue;
      const hit = isQueryKey(m) ? urlStr.indexOf(m) !== -1 : pathOnly === m;
      if (!hit) continue;
      let entry = routes[key];
      if (Array.isArray(entry)) {
        const idx = seqCount[key] || 0;
        seqCount[key] = idx + 1;
        entry = entry[Math.min(idx, entry.length - 1)];
      }
      return {
        status: entry.status === undefined ? 200 : entry.status,
        body: entry.body === undefined ? {} : entry.body,
      };
    }
  }
  return { status: 404, body: { detail: "route not found in fixture" } };
}

global.fetch = function (url, init) {
  const rec = {
    url: String(url),
    method: (init && init.method) || "GET",
    headers: (init && init.headers) || {},
  };
  captured.push(rec);
  const r = routeFor(url, rec.method);
  return Promise.resolve({
    ok: r.status < 400,
    status: r.status,
    json: () => Promise.resolve(r.body),
  });
};

async function runScript(script) {
  for (const step of script || []) {
    if (step.wait !== undefined) {
      await new Promise((res) => setTimeout(res, Number(step.wait)));
    } else if (step.click !== undefined) {
      const el = global.document.getElementById(step.click);
      if (!el || !(el.listeners.click || []).length) {
        throw new Error("script click target not bound: " + step.click);
      }
      el.trigger("click");
    } else if (step.set !== undefined) {
      const el = global.document.getElementById(step.set[0]);
      if (!el) throw new Error("script set target not bound: " + step.set[0]);
      el.value = step.set[1];
    }
  }
}

vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, "..", "proxy", "static", "dashboard.js"), "utf8"),
  { filename: "dashboard.js" }
);

const wait = (ms) => new Promise((res) => setTimeout(res, ms));
(async () => {
  await wait(50);
  // error-state mode: drive the rendered Retry button and record the refetch
  if (mode === "fail-fetch" && elements.retry) elements.retry.trigger("click");
  await wait(30);
  let scriptError = null;
  try { await runScript(fixture._script); } catch (e) { scriptError = String((e && e.message) || e); }
  const out = {
    tab: tab,
    mode: mode,
    script_error: scriptError,
    url: captured.length ? captured[0].url : null,
    urls: captured.map((c) => c.url),
    writes: captured.filter((c) => c.method !== "GET")
      .map((c) => ({ url: c.url, method: c.method, headers: c.headers })),
    html: contentEl.innerHTML,
    texts: global.__texts,
    charts: global.__charts,
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);   // kill pending UI timers (5s confirm revert) — output is flushed
})();
