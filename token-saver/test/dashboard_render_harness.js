/* Node harness for test_dashboard_render.py: executes proxy/static/dashboard.js
 * (an IIFE expecting a browser) against a minimal DOM stub + stubbed /api/kpis
 * fetch, then dumps the rendered tab HTML + captured Chart configs as JSON.
 * Usage: node dashboard_render_harness.js <fixture.json> <tab>
 */
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const fixturePath = process.argv[2];
const tab = process.argv[3] || "overview";
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));

function stubEl(tag) {
  return {
    tagName: tag,
    innerHTML: "",
    style: {},
    addEventListener() {},
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
  return Promise.resolve({ ok: true, json: () => Promise.resolve(fixture) });
};

vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, "..", "proxy", "static", "dashboard.js"), "utf8"),
  { filename: "dashboard.js" }
);

setTimeout(() => {
  process.stdout.write(JSON.stringify({
    tab: tab,
    url: capturedUrls[0] || null,
    html: contentEl.innerHTML,
    charts: global.__charts,
  }));
}, 50);
