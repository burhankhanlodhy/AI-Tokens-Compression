/* Node harness for the dashboard render gates (test_dashboard_render.py,
 * test_ac_a8_dashboard.py): executes proxy/static/dashboard.js (an IIFE
 * expecting a browser) against a minimal DOM stub + stubbed /api/kpis fetch,
 * then dumps the rendered tab HTML + captured Chart configs as JSON.
 *
 * Usage: node dashboard_render_harness.js <fixture.json> [tab] [mode]
 *   tab  = overview|traffic|providers|keys  (default: overview)
 *   mode = "fail-fetch" — every fetch resolves !ok (503) to exercise the
 *          error state; the harness then clicks the rendered Retry button and
 *          reports each fetch URL so the test can assert the refire.
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
  return {
    tagName: tag,
    innerHTML: "",
    style: {},
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    trigger(type) { (this.listeners[type] || []).forEach((fn) => fn()); },
    insertBefore() {},
    setAttribute() {},
    appendChild() {},
  };
}

const contentEl = stubEl("div");
const elements = { content: contentEl };

global.document = {
  getElementById(id) {
    if (!elements[id]) elements[id] = stubEl("div");
    elements[id].id = id; // stamp so Chart captures can be identified
    return elements[id];
  },
  createElement(tag) { return stubEl(tag); },
  querySelector() { return stubEl("nav"); },
};

global.location = { hash: "#" + tab };
global.window = { addEventListener() {}, location: global.location };

global.__charts = [];
global.Chart = function (el, cfg) {
  global.__charts.push({ id: el && el.id, cfg: cfg });
  return { destroy() {} };
};

const capturedUrls = [];
global.fetch = function (url) {
  capturedUrls.push(String(url));
  if (mode === "fail-fetch") {
    return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(fixture) });
};

vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, "..", "proxy", "static", "dashboard.js"), "utf8"),
  { filename: "dashboard.js" }
);

setTimeout(() => {
  // error-state mode: drive the rendered Retry button and record the refetch
  if (mode === "fail-fetch" && elements.retry) {
    elements.retry.trigger("click");
  }
  setTimeout(() => {
    process.stdout.write(JSON.stringify({
      tab: tab,
      mode: mode,
      url: capturedUrls[0] || null,
      urls: capturedUrls,
      html: contentEl.innerHTML,
      charts: global.__charts,
    }));
  }, mode === "fail-fetch" ? 80 : 0);
}, 50);
