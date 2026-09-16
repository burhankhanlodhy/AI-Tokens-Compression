# B-19 / AC-A8 — Dashboard wireframe & render contract (UI/UX spec)

Author: @ui-ux-engineer · For: @application-developer (implementation) + @qa-lead (AC-A8 gate)
Supersedes: nothing — this is the implementation spec PM's B-19 assigned; NOT an HTML deliverable.

---

## 1. Purpose & acceptance

AC-A8 (product-spec-v2.md §5): *"Dashboard: four tabs render; every chart/card maps 1:1 to the KPI API (no client-side aggregation); empty/loading/error states behave per v1 AC-14/15/16."*

B-19 acceptance (PM): a test **named AC-A8** exists and passes; the headline savings figure is **never** `cost_saved + l1_cost_saved`.

Current state: `token-saver/proxy/dashboard_v2.py` (shell), `token-saver/proxy/static/dashboard.js` (renderer), `test/test_dashboard_render.py` (R1–R5 invariants, Node harness) exist and pass. **Gap:** no AC-A8-named gate, and two 1:1-contract defects found in design review must ship fixed (see §8). The R1–R5 tests stay green and are the baseline; AC-A8 supersets them.

The frontend renders only; every displayed number is a field copied verbatim from one `/api/kpis` response. **Zero client-side aggregation.** Financial correctness lives in the SQL (AC-A5/A-12); the browser is a dumb renderer so QA can reconcile every tile against ledger rows.

---

## 2. Information architecture / routes

Four tabs, hash-routed (`/dashboard#overview` etc.), matching PA-3:

| Tab | Route | Tab | Content source (single fetch of `/api/kpis`) |
|---|---|---|---|
| Overview | `/dashboard#overview` | default | `overview`, `series`, `by_model` |
| Traffic | `/dashboard#traffic` | | `overview`, `series`, `latency` |
| Providers | `/dashboard#providers` | | `by_provider` |
| Keys & Tenants | `/dashboard#keys` | Phase C placeholder (no data fetch) | — |

One fetch per tab load: `GET /api/kpis?bucket=minute|hour|day&from=<iso>&to=<iso>` (+ `tenant_id=`/`api_key_id=` when admin selectors exist — currently not exposed in UI; pass through if present in query string).

## 3. Shared scaffolding (one design language, no per-tab reinvention)

- **Shell:** server-rendered `render_shell()` (exists): dark theme, 12-col grid (gap 12), top nav with 4 tabs + bucket selector (minute/hour/day). Bucket selector is a **control**, never an aggregation: it re-issues `/api/kpis?bucket=…`.
- **Component set (shared, one implementation):**
  - `KPI card` — label (uppercase muted), big numeral (`tabular-nums`), optional delta line (▲/▼ + value), optional sparkline canvas. Grid span 3.
  - `Chart card` — title + Chart.js canvas. Span 6 or 12.
  - `Sub-tile` — contained breakdown block inside a parent card (used for the savings decomposition, §6).
  - `Table` — bucket/series rows, tabular-nums, right-aligned numerics.
- **States (shared, from v1 AC-14/15/16):** §7.

## 4. Per-tab layout & tile→field mapping (1:1, field-for-field)

Every tile below lists **exactly** which response field(s) it may read. A tile renders nothing else.

### 4.1 Overview
```
Row 1: [Requests] [Tokens saved] [Est. cost saved] [Effective savings %]   ← 4× span-3 KPI cards
Row 2: [Savings breakdown, span 6]  [Savings over time, span 6]
Row 3: [Model breakdown, span 12]
Row 4: [Recent buckets table, span 12]
```
| Tile | Reads (verbatim) | Notes |
|---|---|---|
| Requests | `overview.requests` | |
| Tokens saved | `overview.input_tokens_saved`; sub-line `overview.savings_pct` | sub-line = "‹pct›% of input" |
| **Est. cost saved (HEADLINE)** | `overview.cost_saved` — **ALONE** | delta arrow = `series[last].cost_saved − series[prev].cost_saved` (the only permitted client arithmetic, plus formatting); sparkline = `series[].cost_saved` |
| Effective savings % | `overview.savings_pct`; sub-line `overview.cache_hit_pct` | |
| Savings breakdown | `overview.cost_saved` headline + sub-tiles: L1 = `overview.l1_tokens_stripped`, `overview.l1_cost_saved` (labeled *portion of total*); cache = `overview.cache_savings` (labeled *reported separately, AC-A6*) | §6 invariant |
| Savings over time | line chart: x=`series[].bucket`, y=`series[].cost_saved` | |
| Model breakdown | donut, **one** `by_model` field only: `by_model[].requests` **or** `by_model[].cost_saved` | **F1 fix, §8** — current tile is mislabeled |
| Recent buckets | `series[]` tail (≤25): bucket, requests, tokens_saved, cost_saved, cache_savings, errors (error badge) | |

### 4.2 Traffic
```
Row 1: [Latency percentiles, span 12 (three KPI cards, see F2)]  [Requests per bucket, span 6] [Errors per bucket, span 6]
Row 2: [Avg latency] [p95] [Error rate] [Cache hit rate]        ← 4× span-3 KPI cards
```
| Tile | Reads | Notes |
|---|---|---|
| Latency p50 / p95 / p99 | `latency.p50`, `latency.p95`, `latency.p99` — three KPI cards (ms) | **F2 fix §8**: the contract exposes **window-global** percentiles only; no per-bucket latency exists in `series`. Any line chart here would repeat one constant across buckets — forbidden (fabricated trend). |
| Avg latency | `overview.avg_latency_ms` | |
| Error rate | `overview.error_rate_pct` (green/red badge) | |
| Cache hit rate | `overview.cache_hit_pct` | |
| Requests per bucket | line: `series[].requests` over `series[].bucket` | |
| Errors per bucket | line: `series[].errors` over `series[].bucket` | |

### 4.3 Providers
`by_provider[]` → one card per provider (span 6, two per row; **not** an interactive sortable table — see open question O1):
| Field (per provider) | |
|---|---|
| Name | `by_provider[].provider` |
| Requests | `by_provider[].requests` |
| Tokens saved | `by_provider[].tokens_saved` |
| Cost saved | `by_provider[].cost_saved` |
| L1 line | `by_provider[].l1_tokens_stripped` + `by_provider[].l1_cost_saved` labeled "portion of cost saved" (zero-guard dash when stripped=0) |
| Cache | `by_provider[].cache_hits` (`by_provider[].cache_hit_pct`) |
| Errors | `by_provider[].error_pct` badge |
Empty → "No provider traffic yet." (no zero-padded charts).

### 4.4 Keys & Tenants
Phase C placeholder card. **Redaction rule (component-level, not afterthought):** this surface never renders secret material; copy reads "keys show last-4 only". If any future tile ever displays a key, only last-4 may render.

## 5. Data contract (source of truth — pinned to `token-saver/proxy/kpis.py`)

`GET /api/kpis?bucket=minute|hour|day&from=&to=` → 200 with:

```jsonc
{
  "bucket": "day",
  "overview": { "requests", "input_tokens_before", "input_tokens_after",
    "input_tokens_saved", "savings_pct", "output_tokens", "cost_before",
    "cost_after", "cost_saved", "cache_savings", "l1_tokens_stripped",
    "l1_cost_saved", "cache_hits", "cache_hit_pct", "errors",
    "error_rate_pct", "avg_latency_ms" },
  "series":   [ { "bucket", "requests", "tokens_saved", "cost_saved",
    "cache_savings", "l1_tokens_stripped", "l1_cost_saved", "errors" } ],
  "by_model": [ { "model", "requests", "tokens_saved", "cost_saved" } ],   // NO cost_before, NO l1_* — see F1
  "by_provider": [ { "provider", "requests", "tokens_saved", "cost_saved",
    "cache_hits", "cache_hit_pct", "l1_tokens_stripped", "l1_cost_saved",
    "errors", "error_pct" } ],
  "latency":  { "p50", "p95", "p99" }                                      // window-global only — see F2
}
```
Rules:
- `by_model` carries **no** `cost_before` and **no** `l1_*` — no chart may fabricate them, no tile may reference them (§4.1 model breakdown, and R4 test).
- 400 on bad bucket/from/to/tenant_id/api_key_id; 503 "ledger unavailable" — the UI's error state handles both (§7).
- Numbers arrive as numbers; UI applies **formatting only** (money = `"$" + toFixed(4)`, ints via tabular-nums grouping). No tile recomputes a statistic.

## 6. Cost-savings decomposition invariant (B2-b — the headline rule)

- `cost_saved = est_cost_before − est_cost_after` **already contains** L1 dollars. `l1_cost_saved` is the L1 **portion of** `cost_saved`; `cache_savings` is reported separately (AC-A6).
- Dashboard rule: **the headline savings KPI is `overview.cost_saved` alone.** `l1_cost_saved` and `cache_savings` render only inside contained sub-tiles of the headline card/breadown — labeled "of which L1 structural" / "of which exact-prefix cache", **never as addends**.
- Invariant enforced per-tile at overview, series, and per-provider levels: `0 ≤ l1_cost_saved ≤ cost_saved`; zero-guard renders a dash + guidance when `l1_tokens_stripped = 0` (no implied `$0.00`).
- Existing renders this correctly (R1–R5 tests pin it, incl. static regex against additive forms). **Regression-free requirement for F1/F2.**

## 7. States (shared, v1 AC-14/15/16 carried)

| State | Trigger | Render | Behavior |
|---|---|---|---|
| Loading | any fetch in flight (initial load, tab switch, bucket change) | skeleton rows in `#content` (shimmer), one per tab region | no spinner-in-a-void; skeletons shaped like the target tab |
| Empty | 200 with `overview.requests == 0` (or missing overview) | full-panel empty: "No traffic yet — send a prompt through the proxy to see your savings." | no zero-padded charts, no tables of zeros |
| Error | fetch !ok (400/503) or network failure | error box: "Couldn't load KPIs. The stats ledger may be unavailable." + **Retry** button re-issuing the same request | never render partial/stale tiles under an error banner |

All tabs share these three states via the single load path (`load()` → fetch → render | empty | error). State handling is a property of the shared shell, not per-tab branches (QA blocks inconsistent states).

## 8. Required fixes (found in design review; ship with B-19)

- **F1 (correctness/mislabel):** Overview "Model breakdown" donut is titled **"Spend per model (cost before)"** but plots `by_model[].requests`. `by_model` has no cost-before field (§5) — the title is a lie the API cannot support. Fix without contract change: retitle the donut to an honest 1:1 field — **"Cost saved per model"** (`by_model[].cost_saved`) or "Requests per model" (`by_model[].requests`). Recommend **Cost saved per model** (money-focused, matches PA-3 intent). Extending `by_model` with `cost_before` is possible but is a **contract change** → QA's AC-A5/AC-A12 reconcile scope; **out of scope for B-19** unless @product-manager ratifies (decision D1).
- **F2 (fabricated trend):** Traffic tab renders "Latency percentiles (ms)" as a **line chart** over buckets repeating the window-global `latency.p95` constant. That fabricates a per-bucket trend that does not exist in the contract — a deception under AC-A8's no-fabrication reading. Fix: remove the line chart; render `latency.p50/p95/p99` as three KPI cards (1:1) alongside `overview.avg_latency_ms`. If a per-bucket latency trend is *wanted*, the API contract must gain a per-bucket latency series — that is a contract/QA decision (D2), not a frontend workaround.

## 9. AC-A8 test matrix (for the named gate — `test_ac_a8_dashboard.py`, reusing `test/dashboard_render_harness.js`)

Reuse the harness + fixture shape from `test_dashboard_render.py` (pinned 2026-09-14/15 rows). Must assert:

1. **Four tabs render** distinct, complete surfaces (overview/traffic/providers/keys) from one fixture.
2. **1:1 fidelity:** every displayed numeral reproduces the fixture byte-exact under the JS formatters (money = `"$"+toFixed(4)`). No fixture value may be arithmetic-ally transformed into a displayed figure (permitted exceptions: money/grouping formatting; delta = `series[last].cost_saved − series[prev].cost_saved`; bar width geometry).
3. **No client-side aggregation:** static scan of `dashboard.js` — no `.reduce`, no `+=`/`sum` over `series`/`by_model`/`by_provider` money or token fields; no new additive pattern of money fields (reuse/extend R2 regex).
4. **Decomposition invariant:** headline shows `cost_saved` alone; neither `cost_saved+l1_cost_saved` nor `cost_saved+cache_savings` appears in any rendered surface (R2 dynamic + static; extend to providers and series).
5. **Zero-guards:** `l1_tokens_stripped=0` → dash + guidance, no `$0.00` implication (overview and every provider row).
6. **No chart references `l1_*`** (R4 — by_model carries none; a chart using L1 must fail the gate).
7. **Donut honesty (F1):** donut dataset equals exactly one `by_model` field (requests or cost_saved); dataset+label agree.
8. **Latency (F2):** latency tiles render only `latency.p50/p95/p99`/`overview.avg_latency_ms`; no per-bucket latency chart exists (assert chart ids exclude the removed `c-lat`).
9. **Single source:** the only fetch is `/api/kpis?…` (assert on the loaded URL, as R-test already does).
10. **States:** fixture with `requests=0` → empty state; fetch failure → error + Retry refires (harness-injected).
11. **Redaction:** no secret-shaped strings (`sk-…`, `sk-ant-…`, raw key material) in rendered HTML for any tab.

## 10. Open questions for the room
- **D1 (@product-manager):** ratify the donut fix as *retitle to "Cost saved per model"* (no contract change) vs. extend `by_model` with `cost_before` (contract change, QA re-scope). Recommend the former for Phase A.
- **D2 (@product-manager + @qa-lead):** keep `latency` window-global (F2 fix stands; percentiles as cards) vs. add a per-bucket latency series to `/api/kpis` (contract change; new reconcile area). Recommend keeping window-global for Phase A.
- **O1 (@product-manager):** PA-3 prose says Providers is a "sortable table"; the ship-now implementation is per-provider cards (which fully satisfy AC-A8's data-fidelity bar). Interactive sorting is not an AC-A8 requirement — recommend ratifying cards now and moving the sortable table to Phase B polish.

## 11. Out of scope (Phase A)
Tenant/key selector UI (API supports `tenant_id`/`api_key_id` filters; no UI until Phase C keys surface — the endpoint param pass-through is trivial if a selector exists, but there is no selector yet). Bucket range picker (from/to) — bucket selector only. Prometheus path (server-side, unaffected).