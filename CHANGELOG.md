# Changelog

All notable changes to **token-saver** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] - Unreleased

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
