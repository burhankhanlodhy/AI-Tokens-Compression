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
- AC-A7. **Tenant/key isolation:** per-tenant and per-key views return only that tenant's data; no cross-tenant leakage. (Secret redaction on any key event logs.)
- AC-A8. Dashboard: four tabs render; every chart/card maps 1:1 to the KPI API (no client-side aggregation); empty/loading/error states behave per v1 AC-14/15/16.
- AC-A9. Graceful provider failure: upstream timeout/5xx → normalized error + metric, not a crash.
- AC-A10. Concurrency: Postgres handles concurrent `/v1/*` + dashboard + KPI calls with no lock contention (SQLite WAL no longer applies post-migration).
- AC-A11. **(Migration release gate, QA)** Migration tested on populated SQLite-era data AND fresh Postgres: backfill correct, rollback recovers, tenant FK isolation holds. **Cost precision contract:** cost columns are stored as `NUMERIC(14,8)`; Postgres rounds stored costs to 8 decimal places ($0.00000001 — far finer than any real cost measurement). The approved verification compares cost sums **at NUMERIC(14,8) quantized scale** — this is the explicit contract, so "no drift" means *no drift beyond 8-decimal quantization*, documented and tested as such (already implemented + documented by Dev). Legacy REAL float-artifacts (e.g. `0.30000000000000004`) verify equal after quantization. If finer precision than 1e-8 USD were ever needed, that scale bump would be a separate reviewed change; it is not required for this release.
- AC-A12. **(Financial trust, QA)** `/api/kpis` totals and every time bucket reconcile **exactly** to the request-level ledger rows in Postgres — no drift; QA ledger-reconciliation suite is a release gate.
- AC-A13. Secret handling (Postgres-native): API keys stored only as hashes (in `api_keys`); no raw keys in DB, logs, or error bodies.

### P1 output-conciseness benchmark (P1-1 — numeric target defined by @product-manager)
- **AC-P1. Success bar (P0-recommended, must all hold):** On a fixed, committed benchmark suite of **≥40 real prompts** (mix: conversational/QA/RAG-ledger ≥60%, code/precise-spec ≤40% — mirroring our classifier's pass-through policy), the proxy with output-conciseness ON must show **mean output-token reduction ≥15%** vs the pass-through baseline, at **quality parity**: ≤1pt regression on a rubric-scored quality check (structural correctness + answer fidelity), evaluated on a **model-based side-by-side pairwise judge** (e.g. GPT-4-class evaluator) with ties-and-wins counted; **no config where code/precise-route outputs lose correctness**. 
- **AC-P1a. Statistical soundness:** the ≥15% reduction and ≤1pt parity must hold at **95% CI (paired test)**, not just as a point estimate; n is fixed and committed before running so results can't be cherry-picked.
- **AC-P1b. Honesty gate (QA + independent verify):** the benchmark is **repo-committed and reproducible with recorded fixtures**; QA independently re-runs it and the published headline ("X% fewer output tokens, no quality loss") must match the committed run. **No claim ships unless it matches the reproducible run** — the JetBrains/rtk lesson (advertised 60-90%, measured +7.6% costlier) made trust the moat.
- **AC-P1c. Realistic floor:** target ≥15% mean reduction, documented as conservative vs. research-figure 20-30% (LLMLingua's reported range), because real curated developer traffic tends lower (TokenShift reports 12-21%). If ≥15% proves unachievable at ≤1pt parity, ship the honest achieved number ≥10% rather than inflating the claim.

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