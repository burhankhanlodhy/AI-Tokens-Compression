# Changelog

All notable changes to **token-saver** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-09-25

### Added

- **Five-tab operator dashboard** — the single-page shell gains a new
  **Settings** tab (below) and evolves Overview/Traffic/Providers/Keys &
  Tenants per the V2.2 product spec
  (`docs/v2.2-ui-ux-overhaul-spec.md`) and the UI/UX design contract
  (`docs/v2.2-ui-ux-design-system.md`):
  - **Traffic** gains a window-global `by_route` route-mix card
    (compress vs passthrough), backed by a new SQL-side `by_route` series
    on `/api/kpis`.
  - **Providers** gains provider **registry facts** (name, base_url,
    adapter_class, enabled) via a new read-only `GET /api/providers`,
    joined with per-provider KPIs; a registry outage degrades to
    KPI-cards-only. Provider-native cache usage stays labeled measured
    evidence and is never merged into savings.
  - **Design system**: new tokens (`--panel-raised`, `--overlay`,
    `--violet`, `--cyan`), `:focus-visible` ring on every interactive
    element, `prefers-reduced-motion` skeleton-freeze, min-44px touch
    targets, scrollable tab strip below 900px (no hamburger), brand
    collapse to "ts" below 560px. All numbers remain 1:1 API-field
    renders; zero client-side aggregation is preserved (render-harness
    fixtures in `test/test_v22_dashboard_render.py`).
- **Runtime settings persistence** (`app_settings`, migration 8,
  `migrations/20260925_v22_runtime_settings.sql`): a new **Settings** tab
  exposes exactly seven server-side-allowlisted runtime switches
  (`l1_enabled`, `tool_schema_minify`, `tool_schema_cache_enabled`,
  `tool_result_optimization`, `tool_result_compression_enabled`,
  `output_conciseness_enabled`, `semantic_cache_enabled` — the latter
  visible-but-locked pending the AC-PC4 calibration gate). Precedence is
  per-request control headers > runtime override > environment variable >
  built-in default. Writes are `PUT /api/settings/{name}` behind
  `ADMIN_TOKEN`; unknown/not-runtime names are rejected 400; deletes
  revert to env/default immediately; overrides survive a proxy restart;
  each request reads the snapshot once at request start, so a dashboard
  flip cannot alter an in-flight request. Every override records
  `updated_at`/`updated_by` (never a token fragment). Deployment-only
  settings (lossy compressor, routing, dose-calibration instruments,
  credentials) are structurally excluded from runtime writes and rendered
  read-only with component-level redaction — no key material, hash, or
  credential field appears in any settings payload.
- **G1 proxy-key scope resolution** (`_resolve_proxy_key_scope`,
  `proxy/main.py`): a `Bearer tsk_…` proxy key that resolves to an active
  `api_keys` row (sha256 key-hash convention) plus an `X-Session-Id`
  header populates tenant/session context on the live request path, so
  TOCP continuations, `/v1/tool-results`, and strategy PolicyContext
  become reachable with real isolation. Unknown or missing keys fail
  closed (401); SQLite single-user deployments keep benchmark-only
  labeling.
- **V2.2 scope and design records** — `docs/v2.2-ui-ux-overhaul-spec.md`
  (PM contract: IA, runtime/deployment-only ruling, data contracts, AC) and
  `docs/v2.2-ui-ux-design-system.md` (UI/UX binding design contract).

### Changed

- **Version metadata** — proxy and semantic-cache quality namespace now
  report `2.2.0`.
- **CI** — test workflow bootstraps the fresh Postgres schema plus all
  migrations before the test lane; the collected-test floor is raised to
  953.

### Unchanged by design

- The compression pipeline itself, pipeline order, and the ledger schema
  are untouched beyond the `app_settings` table and G1 wiring (V2.2 is a
  UI/UX + configuration-persistence release).
- `/stats`, `/metrics`, `/health`, and the existing `/api/kpis` contract
  shapes are pinned unchanged (additive `by_route` only).
- L1 lossless posture, semantic-cache flag-off default, provider-native
  cache attribution, and routing off-by-default carry forward from V2.1.

### Release quality gate

- Independent QA **GO** at candidate
  `05c8ed8f46930b9989c981e6c020cb4f8e29fe7b` (t_819088ac): live-path
  runtime + real-browser verification of all five tabs; settings API
  negative paths (401/400/503, precedence, restart persistence);
  ledger-reconciled A/B proof that a runtime toggle alters proxy behavior
  on real `/v1/chat/completions` traffic (L1 1336→731 vs 1336→1336);
  G1 fail-closed scope resolution; by_route vs independent SQL truth;
  secret masking verified item-by-item; full regression **997 passed /
  0 failed / 0 skipped** in both standard and reverse order on the real
  Postgres DSN.

## [2.1.0] - 2026-09-24

### Added

- **Experimental multi-strategy savings engine (V2.1, all lanes off by
  default)** — six server-side feature-flagged lanes ratified in
  `docs/v2.1-scope-ratification.md`, each disabled by default with no
  client-request enablement path (`v21_*` flags in
  `token-saver/proxy/config.py`, all `False`; no request header or query
  parameter can enable a lane):
  - **Deferred tool loading** (`v21_deferred_tools_enabled`) — provider-gated
    compact tool catalog so eligible requests do not ship every tool schema
    up front.
  - **Tool Output Continuation Protocol / TOCP** (`v21_tocp_enabled`) —
    TTL-bounded, tenant/api-key/session-scoped continuation store for
    truncated tool output with explicit bounded retrieval; capacity
    pressure rejects a new continuation rather than evicting an unexpired
    one (plan §3 retention guarantee), falling back to standard truncation.
    Continuation retrieval is an authorized, scoped read of an
    already-accounted provider tool result, not a new provider request or
    savings event (NO_LOG, rationale in `proxy/tocp.py`).
  - **Incremental Diff Context Protocol / IDCP** (`v21_idcp_enabled`) —
    session-scoped file-version ledger: full content on first read,
    explicit unchanged notice, versioned diff, full-content fallback ladder;
    never applies a stale diff silently. Provisional synthetic replay
    evidence only (`docs/v2.1-idcp-implementation.md`): explicitly
    `NOT_EVALUABLE` against the ratified acceptance gate; no savings claim
    is made.
  - **Adaptive Turn-Budget Allocator / ATBA** (`v21_atba_enabled`,
    `v21_atba_enforce`) — conservative shadow policies; enforcement stays
    off until paired evidence meets the ratified gate.
  - **Multi-Turn Conversation Compression / MTCC** (`v21_mtcc_enabled`) —
    verbatim recent turns + exact-source retrieval; highest-risk lane,
    stays off unless its gate passes.
  - **Strategy orchestration registry** — shared registry reporting status,
    evidence, fallback, and per-strategy attribution without merging
    overlapping denominators. `/api/strategies` is an admin-only read-only
    deployment audit view (NO_LOG; it is not an inference request or
    savings event).
- **V2.1 session stores** — `migrations/20260924_v21_session_stores.sql`
  (migration 7): additive-only `tocp_continuations`, `idcp_file_versions`,
  `mtcc_turns`, `strategy_telemetry` tables with tenant/api-key/session
  binding, DB-enforced sha256/length checks, and TTL columns with expiry
  purge indexes. Retention and rollback documented in `MIGRATIONS.md`
  ("V2.1 session stores"); derived session state is expiry-safe, the
  `requests` audit ledger is untouched.

### Notes

- **No savings claims ship with this release.** All lanes are off by
  default and none has met its ratified empirical acceptance gate; the
  synthetic IDCP figure in the docs is labeled not-evaluable and is not a
  product guarantee. Per the ratified scope: no market/research/vendor
  percentage is an acceptance criterion or product constant (extends
  AC-V2-2 to all V2.1 lanes).
- **Release quality gate**: independent QA GO at candidate
  `682a2f1430f5b96f6ac6f3bd5c8f405383919fca` (t_d66781dd) on the real
  Postgres DSN — standard lane 904/904 (0 skipped), reverse-order lane
  904/904 (0 skipped), PG lane 64/64 (0 skipped); AC-A12 route-accounting
  mutation test fails as required with both NO_LOG entries removed.

## [2.0.0] - 2026-09-24

### Added

- **Provider-native cache usage attribution** — the ledger now persists the
  cache-read/cache-write token counts the provider actually returned
  (Anthropic `cache_read_input_tokens` / `cache_creation_input_tokens`;
  OpenAI-compatible `prompt_tokens_details.cached_tokens`) in two new
  nullable `requests` columns (`provider_cache_read_tokens`,
  `provider_cache_write_tokens`). NULL means the provider returned no cache
  usage evidence; an explicit 0 is a measured zero. These lanes are disjoint
  from `cache_savings`, `l1_savings`, and `tool_compression_saved` — measured
  provider facts are never merged into or double-counted with proxy-computed
  savings. Dollar attribution derives at read time from measured tokens and
  provider rates; no assumed vendor discount and no inferred hit.
- **Anthropic stable system-prefix caching** — the stable system prefix is
  now emitted as a text block with ephemeral `cache_control` so eligible
  requests can receive provider-native cache reads.
- **V2.0 scope and audit records** — `docs/v2.0-scope-ratification.md` (PM
  scope decision, AC-V2-1..9), `docs/v2.0-token-cost-savings-plan.md`,
  `docs/v2.0-ledger-audit.md` (DBA), and `docs/v2.0-provider-cache-audit.md`.

### Changed

- **Version metadata** — proxy and semantic-cache quality namespace now
  report `2.0.0`.

### Unchanged by design (V2.0 is an attribution-and-audit release)

- Routing/cascades remains a server-side, **off-by-default** provider
  selection capability (`provider_routing=false`); it is ratified as a
  discovery-only experiment with no V2.0 savings claim, dashboard figure, or
  production enablement.
- Semantic caching remains flag-off (`SEMANTIC_CACHE_ENABLED=false`) pending
  the AC-PC4 calibration gate.
- L1 lossless and output-conciseness paths, their evidence contracts, and the
  57.71pp headline are untouched.

## [1.2.2] - Unreleased

### Fixed

- **Tool-schema savings ledger attribution** — reconciles estimated savings
  with bytes removed and the whole-request input-token delta. This is a
  schema-only correction to the existing ledger definition; it adds no columns.

## [1.2.1] - 2026-09-23

### Added

- **Codebase-context optimization** — enabled by default for coding-agent
  prompts: truncates oversized fenced file bodies while keeping their head and
  tail, deduplicates frequently repeated import lines, and filters recognizable
  shell noise while preserving errors, warnings, results, and Python traceback
  frames. The master switch and subfeature settings are documented in
  `TUNING.md`.
- **Selective tool-protocol compression** — losslessly compacts validated JSON
  tool-result content and minifies tool schemas. Tool-call arguments, choices,
  and message envelopes are not rewritten by these transforms; each feature can
  be disabled independently.
- **Measured candidate evidence (not a release verdict):** the general E2E
  corpus (20 scenarios / 5 segments, k=2; SHA-256
  `af21ac53e8d1da7f2ab4402a0573ec3709bc5d92513a3ecbc8ce575f6f10a33a`)
  measured **92.61% as-shipped codebase-segment input-token reduction** and
  **0.55% as-shipped marginal tool-segment reduction** (14,682 → 14,601).
  On that same run, the **15.51% isolated transformer contribution** was
  measured as 9,722 `tool_compression_saved` tokens / 62,666 baseline
  tool+schema prompt tokens; this is not customer-bill savings and is published only
  beside the as-shipped result. On the separate schema corpus (92 tools / 5
  scenarios; SHA-256
  `e689f2c7fc8accf6140f2b22dc19bceb0b3e4012d14c0517cb947d40d97d2284`),
  compact-client end-to-end savings were 1.22%, pretty-printed-client savings
  were 43.9%, and repeated-tool-set cache hit rate was 100% (40/40 eligible
  turns). On the general E2E corpus, the result segment measured 0.00%
  (55,145 → 55,145 tokens) because its 4,506-token results were below the
  5,000-token optimization cap. These are population-specific measurements,
  not per-request guarantees. V1.2.1 remains **NO-GO**; re-baselined gates are
  pending measurement on the new fixtures, and no release is claimed.

### Changed

- **Version metadata** — proxy and semantic-cache quality namespace now report
  `1.2.1` for the unreleased candidate. This entry does not indicate that a
  release has shipped.

## [1.1.0] - 2026-09-21

### Added

- **Semantic caching with pgvector** — approximate nearest-neighbor lookup
  for semantically similar prompts using HNSW indexing (cosine distance).
  Enabled via `SEMANTIC_CACHE_ENABLED=true` (off by default). The ratified
  C1 operating point uses threshold=0.18 and ef_search=100.
- **Request versioning schema** — cache entries track a `version` hash so
  code/model/config changes can invalidate the cache without dropping the
  table (migration `20260920_pc5_request_versions.sql`).
- **Semantic cache dashboard** — KPIs and visualization for semantic hits,
  query latency, and cache efficiency in the dashboard's semantic tab.

### Changed

- **Performance Calibration (AC-PC1 through AC-PC5 verified)**:
  - AC-PC1: HNSW index build with ef_construction=200
  - AC-PC2: Query plan enforcement (ordered HNSW scan, never sequential)
  - AC-PC3: Traffic-shaped safety audit (no false positives on real prompt pairs)
  - AC-PC4: Real-traffic calibration verified at 11,500-row scale
  - AC-PC5: 4/27 grid cells pass frozen accuracy/latency bars; C1
    (th=0.18, ef_search=100) ratified as GO
- **CI test suite floor** — raised to 710 passing tests (includes semantic
  cache request path, dashboard render, and vertical integration gates).

### Configuration

- `SEMANTIC_CACHE_ENABLED` — defaults to `false`; human-gated production enablement.
- `SEMANTIC_CACHE_MAX_COSINE_DISTANCE` — similarity threshold as cosine
  distance (ratified operating point: **0.18**; unset by default — an
  absent threshold fails closed to clean misses).
- `SEMANTIC_CACHE_HNSW_EF_SEARCH` — HNSW `ef_search` per lookup
  (default: **100**; keep ≤ 300).
- `SEMANTIC_CACHE_TTL_SECONDS` — entry + response-payload lifetime as one
  expiring pair (default: 300).
- `SEMANTIC_CACHE_MAX_RESPONSE_BYTES` — larger responses are clean misses,
  never truncated (default: 1 MiB).
- `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` — embedding namespace
  (default `text-embedding-3-small` @ 1536); changes bump
  `embedding_version` and quarantine existing entries until expiry.

## [1.2.0] - 2026-09-21

### Added

- **L1 cleanup productionization** — the lossless structural cleanup moves
  from benchmark-hardened to production-hardened: eligibility gate shared
  between the production path and the benchmark harness
  (`l1_eligible(messages, route)`), byte-identity guarantee scoped by
  content class (passthrough-identical for lossy-on-all and L1-on-CODE;
  round-trip reversible for L1-on-JSON/RAG), and ledger attribution kept
  decomposition-safe (`l1_cost_saved` ⊆ `cost_saved`, never an addend).
  Published production savings: 29.9% conservative (C1-only), 70.4% e2e with
  production-default config (per-item range 18.7-80.6%, median 55.7%).
- **Keys/Tenants auth hardening** — `ADMIN_TOKEN`-gated write endpoints
  (`POST /api/keys`, rotate, revoke) with boot-printed fallback token; the
  Keys & Tenants dashboard tab graduates with it. Read endpoints remain
  unauthenticated under the self-host trust model.
- **Documentation** — this release completes the user-facing docs for
  v1.1/v1.2: README configuration matrix (all `SEMANTIC_CACHE_*` env vars
  with real names and defaults), MIGRATIONS.md pgvector cutover runbook,
  and TUNING.md performance guide.
- **UI fix** — confirm-state preservation across 401 re-prompts (§4.5.4),
  preventing stale action resubmission after re-authentication.

### Changed

- **CI test suite floor** — raised to 710 passing tests (ratcheted in
  .github/workflows/ci.yml; includes auth regression coverage for rotate/revoke).
- **HNSW index build parameters** — ratified at `ef_construction=200` in the
  pc1 migration (the C1 operating point is conditional on it; the pre-v1.1
  default of 64 does not meet the frozen bar).

## [1.0.1] - 2026-09-20

### Fixed

- **Tool-calling request integrity** — requests that declare tools or tool
  choice, contain assistant `tool_calls`, or carry `role: tool` results now
  bypass lossy compression and L1 cleanup. Tool protocol envelopes are
  preserved so agentic requests reach the upstream provider unchanged in
  their message content and remain valid for function calling.
- **LLMLingua 512-token input-window guard** — lossy compression now
  counts the input's tokens and leaves the text unchanged when it would
  exceed LLMLingua-2's bundled 512-token BERT input window, instead of
  invoking the compressor past its positional-embedding limit and risking
  corrupted output.

## [1.0.0] - 2026-09-19

Initial release. token-saver is an OpenAI-compatible drop-in proxy that
compresses prompts before they hit the upstream LLM, injects a conciseness
instruction, suppresses hidden reasoning tokens by default, and records
token/cost savings in a Postgres ledger (SQLite remains the local fallback).

### Added (v1.0.0)

- **L1 lossless structural cleanup** — on by default. Whitespace-compacts
  pretty-printed JSON, removes duplicate/empty system blocks and dead RAG
  metadata, and preserves every other content class byte-identical. The
  behavior is pinned on the checksummed taxonomy corpus (13 control fixtures:
  code fences, user-authored YAML, `tool_calls` turns, multimodal parts
  arrays, markdown tables, mixed prose+JSON, JSON inside a fence). Set
  `L1_ENABLED=false` if you send bare JSON you want left verbatim.
- **Lossy prompt compression** — LLMLingua-2 based, tuned via
  `COMPRESSION_RATE`, gated by intent classification on the raw bytes.
- **Conciseness instruction injection** — off by default
  (`OUTPUT_CONCISENESS_ENABLED`).
- **Reasoning-token suppression** — hidden reasoning tokens dropped by
  default for reasoner models (`DISABLE_REASONING_BY_DEFAULT`).
- **BYOK auth passthrough** — the client's own `Authorization` header is
  forwarded per request; the proxy never stores API keys.
- **Streaming** — SSE streaming supported, with Anthropic → OpenAI chunk and
  `tool_calls` delta translation.
- **Token/cost accounting** — per-request input/output tokens and estimated
  USD cost from the built-in price table (unknown models fall back to
  defaults).
- **Postgres ledger** — every request recorded in the `requests` ledger with
  cache and L1 attribution columns; initialized from
  `postgres-schema-v2.sql` on a fresh volume.
- **Dashboards & metrics** — four-tab Chart.js dashboard (`/dashboard`)
  reading the ledger via `/api/kpis` (time-bucketed, tenant/key scoped);
  Prometheus `/metrics` exposing
  `token_saver_requests_total`, `token_saver_tokens_saved`,
  `token_saver_cost_saved`, `token_saver_latency_ms`, model/provider/day
  breakdowns, and `token_saver_ledger_write_failures` (must stay 0).
  `/metrics` and `/api/kpis` fail with 503 when the ledger is unavailable
  rather than reporting zeros.
- **SQLite fallback** — when `TOKEN_SAVER_PG_DSN` is unset, requests are
  recorded locally and read via `/stats` (JSON or text).
- **Exact-prefix request cache** — on by default, with cache hits attributed
  in the ledger and surfaced in the dashboard.
- **Docker Compose quickstart** — proxy on `:8000`, Postgres on `:5433`,
  initdb schema bridge, and one-shot migration files for existing volumes
  (pgvector cutover, response store, cache-status taxonomy).
- **CI acceptance** — full unit suite plus reverse-order pass, a pinned
  pgvector test lane, and clean-clone Docker acceptance on GitHub runners.

### Not included in v1.0.0

- **Semantic caching is disabled by default and not wired.** The
  `SEMANTIC_CACHE_ENABLED` flag exists in configuration but defaults to
  `false`, has no call sites in the request path, and cannot be enabled by a
  client request. The pgvector extension, semantic-cache entry/response
  tables and the safe lookup seam are schema and API preparation only. Do
  not enable the flag until the calibration, tenant-isolation, and
  deterministic-invalidation gates have passed.

## Links

[1.1.0]: https://github.com/burhankhanlodhy/AI-Tokens-Compression/compare/v1.0.1...v1.1
[1.0.1]: https://github.com/burhankhanlodhy/AI-Tokens-Compression/releases/tag/v1.0.1
[1.0.0]: https://github.com/burhankhanlodhy/AI-Tokens-Compression/releases/tag/v1.0.0
