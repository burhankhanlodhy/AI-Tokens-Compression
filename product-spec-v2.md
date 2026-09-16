# AI Token Compression Proxy — Product Spec v2 (Sprint 2: multi-provider + dashboard)

Author: @product-manager | Draft for review | Supersedes v1 for sprint 2 scope; v1 AC-1..17 remain the sprint-1 bar and stay live.

---

## 1. Strategic direction (PM synthesis of the room's analysis)

**Market reality (from Dev's research):** provider-native prompt caching (Anthropic ~90% cached-input discount, OpenAI ~50%) is eating the *soft-compression* value prop. Winning products are **caching-first, compression-second**. We will not beat Portkey/LiteLLM on gateway features.

**Positioning wedge (agreed):** *"cheaper than Portkey, simpler than LiteLLM, compression + caching-focused where gateways are routing-focused."* We are the single-purpose, honest-savings optimization layer — not another gateway.

**What we are building is a product, not the single-user tool:** multi-provider, a money-visibility dashboard, and — later — the multi-tenant/admin surface that makes it sellable.

---

## 2. Scope decisions (confirmed by @user)

- **D1. Monetization:** self-host (free/OSS) first, hosted-cloud tier as the eventual paid path. ✅ Confirmed.
- **D2. Storage:** **Postgres NOW in Phase A** (override). Postgres-native from the start; multi-tenant schema designed Postgres-first, no portable-SQLite dialect constraint. ✅ Confirmed (DBA: net-positive effort).
- **D3. Semantic caching:** DEFER to Phase C. Exact-prefix caching ships in Phase A; embedding-similarity waiting on pgvector decision. ✅ Confirmed.

---

## 3. Phase A scope (the gate — PM locks here once D1–D3 confirmed)

### PA-0 Postgres migration (D2 override — Phase A, Postgres-native)
- Migrate from SQLite to **Postgres in Phase A**; schema designed Postgres-first (no portable-dialect constraint).
- Multi-tenant Postgres schema: `tenants`, `api_keys` (hashed/scoped), `providers` (registry), `requests` (gains `provider_id`, `tenant_id`, `cache_status`, `cache_savings`, **NUMERIC** cost columns, FK constraints with `ON DELETE RESTRICT`).
- Indexes on `(tenant_id, ts)` and `(provider_id, ts)` for KPI time-bucket queries; expression/generated column for `date_trunc('day', ts)` time buckets.
- **Migration safety gate (QA, non-negotiable — AC-A11):** run against BOTH a populated SQLite-era dataset (backfill) AND a fresh Postgres install; verify backfill correctness, rollback/failure recovery, tenant/isolation integrity, and NUMERIC cost precision.

### PA-1 Multi-provider support (headline ask)
- Provider registry (`providers` table + config): **Anthropic, OpenAI, OpenRouter, xAI, Google, + local vLLM/Ollama** — adding a provider = a row, not a code change.
- Per-request routing by model string (`model="anthropic/claude-..."` auto-routes to Anthropic adapter).
- **Real adapter layer**, not URL swap: OpenAI-compatible shape and **Anthropic `/v1/messages` shape** (+ system-as-param).
- Per-provider auth handling; BYOK passthrough preserved.
- **Provider contract matrix (QA gate, non-negotiable):** request translation, streaming, tool calls, multimodal input, auth, retries, rate limits, provider-error normalization — for OpenAI, Anthropic, OpenRouter, xAI, and one local backend (vLLM or Ollama).

### PA-2 KPI / monitoring API
- `/api/kpis?bucket=hour|day|minute&from=...` returning time-bucketed `{overview, series, by_model, by_provider, latency{p50,p95,p99}}` — the single contract both the dashboard and the Prometheus path consume. **One source of truth; no client-side aggregation** (financial correctness).
- `GET /api/kpis` correctness is an explicit AC (see AC list).

### PA-3 Dashboard (charts)
- Four-tab dashboard per @ui-ux-engineer's IA: **Overview** (KPI cards + savings-over-time + spend-per-model donut), **Traffic** (req/s, latency percentiles, error rate, cache-hit gauge), **Providers** (per-provider spend/savings/cache/error sortable table), **Keys & Tenants** (Phase C placeholder).
- One shared component set: big numeral + delta arrow + sparkline; **empty / loading / error** states carried from v1 AC-14/15/16. Dark theme. server-rendered + Chart.js CDN + single `dashboard.js`.
- **Cost-savings contract (B2-b — decomposition, NOT addend).** `cost_saved = est_cost_before − est_cost_after` **already contains** the L1 dollars; `l1_cost_saved` is the L1 *portion of* `cost_saved`, never a separate addend. Invariant, pinned per row and at the `total`, per-bucket, per-provider, and per-key levels: `0 ≤ l1_cost_saved ≤ cost_saved`, and `l1_cost_saved = 0` whenever `l1_tokens_stripped = 0`. **The headline savings KPI is `cost_saved` alone — never `cost_saved + l1_cost_saved` (that double-counts).** `l1_cost_saved` may render only as a breakdown/annotation *of* `cost_saved` (e.g. "of which L1 structural: $X"), in a stacked combo where the L1 slice is contained inside the total bar, and must be surfaced mutually exclusively with `cache_savings` per AC-A6.

### PA-4 Prompt caching orchestration (stack: exact-prefix only, Phase A/B)
- Detect cacheable static prefixes; send provider cache flags; **report cache-hit savings separately from compression savings** in stats (so the dashboard is honest and defies the native-caching critique).
- `requests` gains `cache_status` (miss/exact-hit) + `cache_savings` (Postgres hash lookups, not SQLite).

---

## 4. Phase grouping (as boarded by PM)

- **Phase A (this gate):** PA-0 Postgres migration (D2), PA-1 multi-provider+adapters, PA-2 KPI API, PA-3 dashboard, PA-4 exact-prefix caching. + DBA's Postgres-native multi-tenant schema.
- **Phase B:** dashboard polish, deeper caching attribution, load/concurrency hardening.
- **Phase C:** multi-tenant keys/quotas/spend-caps, semantic caching (pending pgvector decision on Postgres), further scale-out.
- **Explicitly OUT of Phase A:** semantic caching, hosted billing. (No scope creep.)

---

## 5. Acceptance criteria (Phase A — expanded for QA; QA's flags are codified)

- AC-A1. Registry supports all 6 providers; adding a provider requires no code change (config/row only).
- AC-A2. `model=` string auto-routes to the correct provider adapter; unknown provider → clear 4xx, no crash.
- AC-A3. **Contract matrix passes.** All registry providers are tested *through their adapter class*: one parametrized `OpenAICompatAdapter` suite covering OpenAI/OpenRouter/xAI/Google/vLLM/Ollama (parameterized over auth_style: bearer / x-api-key / query-param / none, and model-route prefix) + a separate Anthropic suite. Across: streams, tool calls, multimodal, auth, retries, rate limits, error normalization. (QA-owned suite; recorded fixtures, no network in CI.)
- AC-A4. Anthropic requests use `/v1/messages` + system-as-param; OpenAI-compatible shape unchanged for OpenAI/OpenRouter/xAI.
- AC-A5. KPI API: `/api/kpis` returns the documented JSON contract; **numbers reconcile exactly to the Postgres `requests` ledger rows** (no drift). QA asserts financial-arithmetic correctness.
- AC-A6. Cache-hit savings are reported **separately from** compression savings in both KPI API and dashboard.
- AC-A7. **Tenant/key isolation & KPI selector contract (B-6a, ratified against implementation at `token-saver/proxy/kpis.py`, committed `7f2d102`).** `/api/kpis` accepts two **optional** query params, `tenant_id` and `api_key_id`. They are resolved on the route wrapper and threaded into `_fetch_kpis`, where they contribute to the ledger `WHERE` clause **before** every aggregate — so the `overview`, `series`, `by_model`, `by_provider` **and** `latency` groups are all scoped by the same selection (verified in code: the `where` is built once at the top of `_fetch_kpis`, lines 45–54, and reused by every query). Semantics (the agreed, shipped shape): **both absent** = aggregate across **all** tenants (admin/overview view); **present but unknown id** = **empty scope** — 200 with zeroed aggregates, *not* a 400 (an empty scope must never be readable as, nor turn into, cross-tenant data); **both present** = intersection (rows must match *both* `tenant_id` AND `api_key_id`). Scope is asserted **before** aggregation so no client-side filtering can leak rows. Per-tenant/per-key views return only that tenant's data; no cross-tenant leakage in any group. (Secret redaction on any key event logs, per AC-A13.) QA asserts: all-tenants = Σ tenants' rows; unknown tenant → zeros; a single tenant's overview/series/by_model/by_provider/latency all match the 1-tenant seed, and `api_key_id` narrows `tenant_id` further.
- AC-A8. Dashboard: four tabs render; every chart/card maps 1:1 to the KPI API (no client-side aggregation); empty/loading/error states behave per v1 AC-14/15/16.
- AC-A9. Graceful provider failure: upstream timeout/5xx → normalized error + metric, not a crash.
- AC-A10. Concurrency: Postgres handles concurrent `/v1/*` + dashboard + KPI calls with no lock contention (SQLite WAL no longer applies post-migration).
- AC-A11. **(Migration release gate, QA)** Migration tested on populated SQLite-era data AND fresh Postgres: backfill correct, rollback recovers, tenant FK isolation holds. **Cost precision contract:** cost columns are stored as `NUMERIC(14,8)`; Postgres rounds stored costs to 8 decimal places ($0.00000001 — far finer than any real cost measurement). **Drift bound stated as a function of row count, not just "8 decimals":** per-row quantization rounds to at most ±0.5e-8, so on a ledger of N rows the worst-case accumulated absolute drift is **N × 0.5e-8 USD** (e.g. 10M rows ≈ $0.05 — acceptable for this release; state it as such). The approved verification compares cost sums **at NUMERIC(14,8) quantized scale** — this is the explicit contract, so "no drift" means *no drift beyond the row-count-scaled quantization bound*, documented and tested as such (already implemented + documented by Dev). Legacy REAL float-artifacts (e.g. `0.30000000000000004`) verify equal after quantization. If finer precision than 1e-8 USD were ever needed, that scale bump would be a separate reviewed change; it is not required for this release.
- AC-A12. **(Financial trust, QA)** `/api/kpis` totals and every time bucket reconcile **exactly** to the request-level ledger rows in Postgres — no drift; QA ledger-reconciliation suite is a release gate.
- AC-A13. Secret handling (Postgres-native): API keys stored only as hashes (in `api_keys`); no raw keys in DB, logs, or error bodies.

### P1 output-conciseness benchmark (P1-1 — revise-and-re-run, per audit 2026-09)
> **Status (post-audit):** the initial two runs are **statistically uninterpretable** and their "target NOT MET" conclusion is withdrawn. Root cause: the harness reported a *mean-of-per-prompt-ratios* with one sample per arm — an estimator that collapses to ≈0 (and can go negative) under realistic output-length CV regardless of the true effect, so it could not distinguish "no effect" from "~17% effect." AC-P1..P1c below are the corrected, preregistered criteria for the re-run. The feature's enable-by-default decision is **re-opened** pending this re-run; it must not stand on the withdrawn evidence.

- **AC-P1 (headline = ratio-of-sums).** On the **same committed** ≥40-prompt fixture set, headline metric is **ratio-of-sums** (`Σ_treatment_output_tokens / Σ_baseline_output_tokens`), which is what a customer's bill actually sees. Target: on non-code categories (the categories conciseness can affect), the headline must show **≥15% output-token reduction**. Report **per-category** too; never present code/precise-route results in the headline (classifier passes those through by policy).
- **AC-P1a (statistics, corrected).** Take **k ≥ 5 samples per arm** per prompt, **temperature pinned** across both arms. First, run ONE prompt 5× on the baseline arm and **measure the output-token SD** — this single cheap measurement gates whether anything is measurable at all; if CV is too high to resolve a 15% effect at n×k, say so rather than run blind. Inference uses **bootstrap or Wilcoxon** (the ratio distribution is not normal); use **t(39)=2.023**, not 1.96. **Count output via the provider's `usage.completion_tokens`**, not `count_text()` on the returned string (the proxy counter is the wrong quantity and invisible to always-on reasoning tokens). **Count injected input tokens every request** and report net cost including them and proxy overhead.
- **AC-P1b (honesty gate, hardened).** Same committed fixtures **with the checksum pinned to an expected value in the repo and the runner failing on mismatch**. QA independently reproduces the run; the published headline must match the committed run. Judge: **randomize A/B order per item** (baseline is not always A — position bias is documented) and **average both orders**; parity gate uses the spec's **"≤1pt mean regression," not "zero items >1pt"** (the zero-item version is stricter than the spec and won't survive judge noise). No claim ships unless reproducible.
- **AC-P1c (floor, corrected).** **Remove the LLMLingua 20–30% citation** — that figure is **input-side** context compression, the wrong literature class for an output-conciseness target (category error, now confirmed). The realistic floor is set from **output-length findings only** (e.g. Microsoft's 20–30% response-length reduction) and honest developer-traffic experience (TokenShift 12–21%). If the corrected method still fails the floor at parity, ship honest shortfall and feature off-by-default.
- AC-P1d (cache-correctness interaction). Conditional injection is **not** applied to prompts that would fragment the PA-4 exact-prefix cache: the conciseness decision must be **deterministic per canonical prompt** (stable prefix), never a coin-flip classifier, so cacheability is preserved. Regression: cache hit rate does not drop when the feature toggles per category vs. a static-prefix baseline.
- **Corpus coverage note (B-15, PM, measured):** the pinned 40-prompt corpus (`benchmark/prompts.json`) contains **0** prompts whose last user message clears the conciseness gate's short-question heuristic — all 40 last-user messages are ≤160 chars (median 70, max 160) while the gate requires >400 chars / non-short-question shape. The gate is therefore **untested by the pinned corpus**: no output-conciseness savings claim can be derived from the committed benchmark set. Gate ordering/behavior is guarded only by the synthetic `LONG_USER` (~468-char) fixture in `test_benchmark_control.py` (B-13), not by any corpus member. Because output-conciseness is enable-by-default **re-opened** and currently off-by-default, PM does **not** add corpus fixtures for it now; corpus expansion is deferred to the AC-P1c re-run decision — if the feature is re-armed, fixtures whose last user message clears 400 chars must be added to the pinned set at that point, before any claim is re-measured.

### Phase B candidate — Lossless L1 structural cleanup (recommended over further output-conciseness)
> **PM endorsement (post-audit):** the strongest defensible savings claim available is **lossless L1 structural cleanup**, not output conciseness. A proxy is uniquely positioned: it sees the raw request and can strip pretty-printed JSON scaffolding, duplicated system blocks, and dead retrieval metadata with **zero quality risk and no judge required** — no rubric to defend. Early benchmark (audit): **~31% token reduction at 100% answer retention**, vs token-level pruning which added ~1.5pp but destroyed ~41pp sentence integrity. This fits the "honest savings" positioning far better than any claim needing a rubric.
> **Taxonomy v1.1 → v1.2 amendment (PM, measured — supersedes l1-taxonomy.md §5 "passthrough routes" negative list):** the byte-identity blast radius of ruling A is exactly the two `json_rag` arms of `test_passthrough_reaches_upstream_byte_identical`, so L1 gets its **own eligibility gate independent of the lossy router**, and the byte-identity guarantee is narrowed by content class (below). All four `L1_ENABLED=false` arms and the `code` arms remain green at HEAD.
- **AC-P1e. L1 clean target:** strip only lossless structural content — JSON whitespace compaction (C1), duplicate/empty system-block removal (C2), dead retrieval/RAG metadata drop (C3, shape-gated) — **with no change to answer content whatsoever**, per taxonomy v1.1 §4/§5 dead+negative lists implemented in `proxy/l1_clean.py` (commits c0b3565 + 105a832). Success bar: **≥15% input-token reduction on RAG/JSON-heavy categories**, fully **deterministic** (stable cache prefix, defaults clean to the same bytes) so PA-4 caching is preserved.
  - **L1 eligibility gate (v1.2):** L1 has its **own independent eligibility predicate** — `l1_eligible(messages, route)` — extracted into `proxy/l1_clean.py` and imported by BOTH the production path (`main.py`) and the benchmark harness. L1 runs on passthrough-classified routes; the lossy route gate (`route == "passthrough"`, `main.py:354`) governs **lossy compression only**, never L1. The harness must import the shared predicate and must not keep a hardcoded `if route == "passthrough"` shortcut (`run_l1_benchmark.py:83` — this is why `--production-path` currently hardcodes `a = b`). Re-introducing the gate in the shared predicate only must make `--production-path` report 0.0%.
  - **Byte-identity guarantee, by content class (v1.2 narrowed, cites the measured 2-test collision):** upstream receives **byte-identical** input for **lossy compression on ALL content** and for **lossless L1 on `CODE` content**. For **L1 on JSON/RAG** (cleaned structurally), upstream receives **non-identical** bytes — the guarantee is **round-trip reversibility**, not passthrough identity: `clean` is idempotent and the raw→clean mapping is reproducible (cache keyed on clean bytes, AC-P1f), which the regression asserts in place of passthrough byte-identity. QA keeps `test_passthrough_reaches_upstream_byte_identical` for the lossy-all and lossless-`CODE` arms and drops the `[json_rag-True-*]` parametrizations; all `L1_ENABLED=false` arms stay.
  - **Published savings arm (named):** the headline marketing figure quotes the **production-default** arm — **C1+C2+C3 all on, 70.4%** — as measured reproducibly by `run_l1_benchmark.py --production-path` (divergence gate green, transform-level == end-to-end). **C1-only = 29.9%** is stated as the conservative guaranteed floor (yield a user gets if they disable C2/C3). Neither number is a dashboard constant — tiles read ledger `l1_tokens_stripped` per the a1eae90 decomposition contract.
- **AC-P1f. Interaction with caching:** L1 cleaning must be a pure reversible transform keyed deterministically; cache key = clean bytes, and the raw→clean mapping must be reproducible so a cache hit serves an identical clean prompt. No non-determinism, no lossy path.
- **Sequencing (audit-endorsed):** do **not** run multi-model output-conciseness yet — it multiplies an underpowered design. Fix estimator/counting/sampling, re-run output-conciseness on GLM-5.3-Flash once, THEN go multi-model; land L1 cleanup as Phase B independent of that re-run.

---

## 6. Open blockers (only after D1–D3 confirmed — otherwise these are the plan)

- Provider contract matrix draft + adapter API surface → @application-developer to propose, @qa-lead to own the matrix test suite.
- Exact-prefix cache key design (hash over canonicalized static prefix) → @database-administrator + @application-developer.
- Postgres DDL + migration script + backfill/rollback design → @database-administrator (D2 locked; spec v2 updated).

---

## 7. Revenue / popularity levers (analysis → action)

- Honest numbers (cache vs compression separated) = the trust that converts — build this into the dashboard, don't hide it.
- Single-command install (`pip install` / `docker run`) is the adoption gate.
- The wedge (cheaper/simpler than gateways, compression+caching focused) is defensible in community channels (HN, dev Reddit) — positioning copy should sell that, not feature-count.

---

*Revision: v2 by @product-manager — Draft, gated on D1–D3 from @user.*