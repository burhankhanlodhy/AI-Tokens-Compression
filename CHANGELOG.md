# Changelog

All notable changes to **token-saver** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1] - 2026-09-20

### Fixed

- **Tool-calling request integrity** — requests that declare tools or tool
  choice, contain assistant `tool_calls`, or carry `role: tool` results now
  bypass lossy compression and L1 cleanup. Tool protocol envelopes are
  preserved so agentic requests reach the upstream provider unchanged in
  their message content and remain valid for function calling.


Initial release. token-saver is an OpenAI-compatible drop-in proxy that
compresses prompts before they hit the upstream LLM, injects a conciseness
instruction, suppresses hidden reasoning tokens by default, and records
token/cost savings in a Postgres ledger (SQLite remains the local fallback).

### Added

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

[1.0.1]: https://github.com/burhankhanlodhy/AI-Tokens-Compression/releases/tag/v1.0.1
[1.0.0]: https://github.com/burhankhanlodhy/AI-Tokens-Compression/releases/tag/v1.0.0