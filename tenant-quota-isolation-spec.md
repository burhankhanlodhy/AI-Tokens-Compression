# Tenant Quota, Rate-Limit & Isolation Spec (V1.3 scope)

Author: @product-manager · For: @application-developer (implementation) + @qa-lead (gates) + @database-administrator (review)
Status: DRAFT for approval — do not start implementation until ratified.
Parent decisions: t_d1bedf6c (owner-ratified priority order: tenant quota/isolation precedes fidelity-safe RAG/context), t_0da9d17a (V1.2.2 exact-SHA QA gate).
Grounded against branch tip `a28ba93cfed05783aee3e4f01a95ad6893e3d21f` (integration/v1.2.1-candidate); file:line cites below were verified at that tip and are the shipped mechanisms, not aspirations.

---

## 1. Problem & Goal

**Problem:** the proxy is multi-tenant in schema but not in behavior. Three verified gaps, each with its cite:

- **G-A. The data plane authenticates nobody.** `POST /v1/chat/completions` (main.py:594) and `POST /v1/embeddings` (main.py:1540) perform no proxy-key check. `Authorization`/`x-api-key` headers are the caller's **upstream BYOK credential**, extracted and forwarded verbatim (`_upstream_credential`, main.py:350-355). The `api_keys` table, its hash/last4/plaintext-once lifecycle, and the rotate/revoke endpoints (main.py:1696/1717/1744) mint keys that currently gate **nothing** on the request path.
- **G-B. The ledger and caches are pinned to the default tenant.** Every ledger insert hardcodes `tenant_id = '00000000-…'` and never writes `api_key_id` (stats.py:249-271). Exact-prefix cache lookup/record hardcode the same tenant (caching.py:73-79, 95-97). The semantic cache is tenant-scoped internally but the request path passes `DEFAULT_TENANT_ID` unconditionally (main.py:822). Result: in any multi-key deployment, per-tenant spend is unmeasurable and one tenant's cached prefixes/entries are accounted under `default`.
- **G-C. No quota or rate-limit mechanism exists anywhere.** `spend_cap_usd` on `tenants` and `api_keys` (postgres-schema-v2.sql:21, 61) is stored and displayed (Keys & Tenants tab) but never enforced. The only rate-limit reference in the tree is a comment about *upstream* 429s (main.py:1380). No middleware, no 429 path, no limiter.

**Goal:** make tenant identity real on the request path and enforce spend/rate limits per tenant and per key, **without breaking the drop-in contract** ("change one `base_url`, keep your API calls exactly the same" — product-spec.md §1). Self-host default behavior must be byte-for-byte unchanged when the feature is off.

**Non-goals (this scope):** full admin auth/accounts/sessions (deferred since the 2026-09-19 ADMIN_TOKEN ruling), hosted billing/stripe, per-scope key enforcement (scopes stay metadata), request-header-configurable limits (following the AC-PC5 deployment-only-switch precedent).

## 2. Design decisions (ratified here — do not re-litigate in implementation)

### D1. Tenant/key identity — `X-Proxy-Key` header, sha256 lookup
- The proxy-facing key is presented in a **new request header `X-Proxy-Key`** carrying the `tsk_…` plaintext exactly as minted (`_new_proxy_key`, main.py:1690-1693).
- **`Authorization` and `x-api-key` remain exclusively the upstream BYOK credential.** Reusing either would (a) collide with the x-api-key auth-style adapters (providers table, postgres-schema-v2.sql:26-45) and (b) force self-host users to choose between authenticating to us and authenticating upstream. The dedicated header avoids both; it is also invisible to any OpenAI SDK, which is why the default mode below must not require it.
- Lookup: `sha256(X-Proxy-Key)` against `api_keys.key_hash` (UNIQUE), row must be `status = 'active'` (revoked/rotated → 401). Key → tenant via `api_keys.tenant_id`.
- **Single mode switch `PROXY_AUTH_MODE` (Settings field, env `PROXY_AUTH_MODE`):**
  - `open` (**default**, today's behavior): no key checked; request attributed to the seeded default tenant `00000000-0000-0000-0000-000000000000`. Drop-in contract intact; OpenAI SDKs work unmodified.
  - `required`: `/v1/*` data-plane requests (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`) must carry a valid active key; **missing, unknown, revoked, or rotated → 401**, the compact error shape (`_unauthorized`, main.py:185-187). Fail-closed, always — an absent key must never fall through to the default tenant.
- Attribution is by key, never by client-supplied tenant id. No request header may select a tenant (AC-PC5 discipline: deployment switches only).

### D2. Quota semantics — calendar-month spend, checked pre-forward
- **Spend basis:** `SUM(requests.est_cost_after)` for the authenticating tenant (and the key, when it has its own cap) over the **current UTC calendar month**. `est_cost_after` is an estimate, not billed cost — this is stated in every quota error payload and doc so nobody mistakes it for an invoice.
- **Cap resolution:** effective cap = key's `spend_cap_usd` if non-NULL, else tenant's `spend_cap_usd` if non-NULL, else uncapped (NULL = uncapped, existing schema semantics, unchanged).
- **Check point:** before the upstream forward in the chat-completions and embeddings handlers. Over-cap → **429** with `detail` of the shape `{"error": "quota_exceeded", "scope": "tenant"|"key", "period": "calendar_month_utc"}`. (429 not 402: clients already have 429/backoff handling; retrying later in the month can genuinely succeed.)
- **Budget-check caching:** the month-spend SUM is cached **in-process for 30 seconds** per tenant to bound per-request DB cost (index `idx_requests_tenant_ts` (postgres-schema-v2.sql:118) serves it). Consequence, accepted deliberately: a tenant can overshoot its cap by up to ~30s of traffic. Overshoot magnitude is observable (AC-TQ9), not silently absorbed.
- **No new tables/columns.** Zero DB migration in this scope. Spend is derived from the ledger; rate limits are in-process. This keeps the V1.2.2 migration machinery untouched and rollback trivial.
- **Scope note (explicit):** monthly SUM over `est_cost_after` includes cache-hit rows, which record the savings rather than the billed cost of a replayed response. This is the *conservative* direction for a spend cap (cache hits understate spend). Accepted; revisit only if a hosted deployment demonstrates material undercharging.

### D3. Rate limiting — in-process token bucket, per key and per tenant
- Token bucket per **api_key** (default 60 req/min) and per **tenant aggregate** (default 240 req/min), configurable via `RATE_LIMIT_KEY_RPM` / `RATE_LIMIT_TENANT_RPM` (Settings fields; 0 = disabled).
- Exceeded → **429** with `Retry-After` (seconds to next token) and `{"error": "rate_limit_exceeded"}`. Buckets live in-process: single-worker correct, multi-worker best-effort (documented limitation; a shared Redis/Postgres bucket is explicitly out of scope until a measured multi-worker deployment exists — same evidence-first pattern as the D3 external-vector-store ruling).
- Buckets are refilled on a rolling window, not fixed windows; burst = 2× rate, no queueing.

### D4. Failure mode (fail-open vs fail-closed) — the split ruling
- **Auth: fail-closed, unconditionally** (in `required` mode). An infra error during key lookup is a 503, never an open door and never a fall-through to the default tenant.
- **Quota evaluation: fail-open on infra error by default.** If Postgres is unavailable the request forwards and the ledger marks `cache_status`-adjacent facts normally; the quota check reports `unknown` and the proxy stays on the "cache must never break the proxy path" continuity discipline (caching.py:84, main.py:803-806 precedent). Rationale: self-host owners choose this proxy for reliability; a DB hiccup must not down their LLM traffic.
- **Escape hatch:** `QUOTA_FAIL_MODE=fail_closed` (Settings field, default `fail_open`) makes over-cap-unknown block with 503 for hosted deployments where unbounded spend is the bigger risk. One env var, deployment-owned.
- **Rate limiter: cannot fail open-by-infra** — it is in-memory with no external dependency; a bucket error fails the bucket *closed* (429) rather than skipping the check.

### D5. Cache/ledger isolation — identity flows into every write path
- When a request authenticates (required mode), the authenticating `tenant_id` **and** `api_key_id` are threaded into the ledger insert (stats.py:249-271 replaces its hardcoded zero-UUID literal with the resolved identity; `api_key_id` gets its first writer — the column exists, postgres-schema-v2.sql:82, and has none today).
- Exact-prefix cache lookup/record (caching.py:57-105) and semantic-cache scope construction (main.py:822) use the resolved `tenant_id` instead of `DEFAULT_TENANT_ID`. `cache_entries`'s unique key already includes `tenant_id` (postgres-schema-v2.sql:155), so **cross-tenant cache sharing stays structurally impossible**; per-tenant rows just stop all collapsing into `default`.
- **Open mode is unchanged:** no key → default tenant zero-UUID everywhere, byte-identical behavior including cache sharing for identical prefixes. This is what makes the change shippable without a behavior cliff.
- KPI scoping needs no change: `/api/kpis?tenant_id=&api_key_id=` (AC-A7, kpis.py:45-54) starts returning non-degenerate per-tenant/per-key numbers the moment writes carry real identity.

### D6. Compatibility
- Default (`PROXY_AUTH_MODE=open`, caps NULL, limiters disabled-by-default via RPM=0? **No — decided: limiters ship with the defaults in D3 active** in `required` mode only; in `open` mode rate limiting is off and quotas are unenforced because everything is the default tenant) → **existing self-host deployments observe zero behavior change**.
- OpenAI SDK compatibility: `X-Proxy-Key` is an extra header; SDKs that pass through custom headers work unmodified; bare SDK users keep working in `open` mode.
- `X-Proxy-Key` never appears in logs, ledger rows, or error payloads (AC-A13 secret-handling discipline; the plaintext is stored nowhere — only hash + last4, per schema).
- No changes to `/health`, `/dashboard`, `/metrics`, `/api/kpis`, or the key-management endpoints' own auth (they keep the `ADMIN_TOKEN` bearer gate, main.py:190-198).

### D7. Rollout / rollback
- **Rollout is env-only:** set `PROXY_AUTH_MODE=required`, optionally `QUOTA_FAIL_MODE`, `RATE_LIMIT_KEY_RPM`, `RATE_LIMIT_TENANT_RPM`, and create keys via the existing `POST /api/keys`. No migration, no tag, no compose change beyond the env entries. Deploy order: ship the release in `open` mode everywhere (no behavior change, isolation plumbing exercised by tests only), then flip `required` per deployment when keys exist.
- **Rollback = unset the vars** and restart. The `open` path is the permanent fallback; there is no schema step to unwind.
- **Prerequisite before flipping `required`:** at least one active key minted and stored by the operator (401-lockout guard, AC-TQ8).

### D8. Observability
- New Prometheus counters on the existing `/metrics` surface (PlainTextResponse, main.py:1557): `token_saver_auth_failures_total{reason}`, `token_saver_quota_blocked_total{scope}`, `token_saver_rate_limited_total{scope}`, `token_saver_quota_check_unknown_total`.
- The 30s budget cache state and the fail-open/fail-closed limb actually taken are visible via the counters above — an operator can distinguish "under cap", "blocked", and "couldn't evaluate" without reading logs.
- No dashboard work in this scope (the dedicated tool-attribution KPI/dashboard expansion is deferred by owner ruling, t_d1bedf6c); the counters are the contract the deferred dashboard work will later read.

## 3. Acceptance criteria

Grouped P0 (identity/isolation correctness) → P1 (enforcement + observability) → P2 (DX). "Regression test" wording per house convention: the fix and its test are one unit.

### P0 — identity & isolation

- **AC-TQ1. `required`-mode 401s (fail-closed auth).** In `PROXY_AUTH_MODE=required`, a data-plane request with a missing, unknown, revoked, or rotated `X-Proxy-Key` → 401 with the compact error shape; the request never reaches upstream and is never attributed to the default tenant. Regression test: parametrized over the four invalid-key classes asserting 401 + zero ledger row.
- **AC-TQ2. `open` mode is byte-identical.** With `PROXY_AUTH_MODE` unset (default), a request with no `X-Proxy-Key` behaves exactly as at a28ba93: no auth check, ledger row on the default tenant, cache keys on the default tenant. Regression test: golden compare of ledger row + cache row against pre-change fixtures.
- **AC-TQ3. Identity threads into ledger + caches.** In `required` mode, a successful request with key K (tenant T) writes `requests.tenant_id = T` and `requests.api_key_id = K`, and exact-prefix + semantic cache rows for that request carry `tenant_id = T`. Regression test: two keys of two tenants with identical bodies → distinct cache rows, no cross-tenant hit, ledger rows carry both ids.
- **AC-TQ4. No header can select a tenant.** In `open` mode, `X-Proxy-Key` (or any header) claiming another tenant cannot redirect attribution; attribution in `open` mode is always the default tenant. Regression test: send `X-Proxy-Key: <valid-key-from-T>` in `open` mode → still default-tenant attribution (identity comes from the mode, not the header).
- **AC-TQ5. Key material never leaks.** No log line, ledger column, cache payload, metric label, or error body ever contains the `tsk_…` plaintext or full `key_hash`; only `key_last4` (Keys & Tenants tab contract). Regression test: assert over captured logs + responses for a full request lifecycle.

### P1 — enforcement & observability

- **AC-TQ6. Spend-cap enforcement.** Tenant at/over its calendar-month `SUM(est_cost_after)` cap → 429 `quota_exceeded` before upstream forward; key-level cap overrides tenant cap when set; uncapped (both NULL) never blocks. Regression test: seeded month-to-date spend at cap−ε (pass), cap+ε (429), key-overrides-tenant, NULL-never-blocks.
- **AC-TQ7. Rate limits.** Sustained traffic over `RATE_LIMIT_KEY_RPM` → 429 with `Retry-After` for that key while other keys of the same tenant continue; tenant-aggregate limiter fires at `RATE_LIMIT_TENANT_RPM`. Regression test: bucket-filling requests with frozen clock.
- **AC-TQ8. Lockout guard.** Refuse to start in `required` mode with zero active keys (fail-fast at boot with a clear log) OR gate the mode flip on the first key existing — implementer picks one, states it, tests it. Invariant: it must not be possible to flip to `required` and lock every client out including the operator.
- **AC-TQ9. Metrics.** The four D8 counters exist, increment on their events, and appear in valid exposition format on `/metrics`. Regression test: scrape `/metrics` before/after each event class.
- **AC-TQ10. Fail-open vs fail-closed limbs.** With Postgres unreachable and `QUOTA_FAIL_MODE=fail_open` (default), requests forward and `token_saver_quota_check_unknown_total` increments; with `fail_closed`, they 503. Regression test: DSN pointed at a dead socket, both limbs asserted.

### P2 — DX & docs

- **AC-TQ11. Env manifest.** New vars (`PROXY_AUTH_MODE`, `QUOTA_FAIL_MODE`, `RATE_LIMIT_KEY_RPM`, `RATE_LIMIT_TENANT_RPM`) documented in README's env-var section and present with empty/default values in `.env.example` (verified absent at a28ba93 despite README:122 referencing it — this scope creates it or the AC's placeholder lands with whatever env file the repo standardizes on; Dev states which in the implementation PR).
- **AC-TQ12. Key lifecycle end-to-end.** Mint via `POST /api/keys` → use on data plane in `required` mode → rotate → old key 401s immediately, new key works (the rotate contract already promises "old key stops authenticating immediately", keys-tenants-tab-spec.md §3 — this is the first scope where that promise is actually testable on the request path). Regression test: full lifecycle against a live app instance.

## 4. Open decisions (need @user or later gate — not blockers for spec approval)

1. **Default RPM values** (D3: 60/240) are my recommendation, not measured. Ratify or adjust at implementation review; the ACs gate on the mechanism, not the constant.
2. **30s budget-cache overshoot** (D2) accepted by me; if a hosted deployment needs tighter caps, the knob is the cache TTL — flag, don't redesign.
3. **Hosted plan gating** (`hosted_paid` plan semantics, e.g. hard-stop vs grace period) is V2.0 discovery-only per the owner's sequencing (t_d1bedf6c) — out of scope here.

## 5. Handoffs

- **@application-developer:** unblocked on spec approval. Build list: D1 mode switch + header lookup, D2 quota check + budget cache, D3 buckets, D4 fail-mode limbs, D5 identity threading, D8 counters. ACs AC-TQ1..TQ12 are your gates.
- **@qa-lead:** gates are the numbered ACs; the AC-TQ2 byte-identical golden is the release-critical one — any drift there fails the drop-in contract, not just the AC.
- **@database-administrator:** review requested on D2/D5 (no-migration ruling, index sufficiency of `idx_requests_tenant_ts` for the month-SUM at production volume). No schema work is requested in this scope.
