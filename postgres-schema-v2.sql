-- AI Token Compression Proxy — Phase A Postgres-native schema (PA-0)
-- Author: @database-administrator | Draft v0.1, feeds AC-A11/A12/A13 QA gates
-- Design goals: multi-tenant from day 1, financial correctness (NUMERIC, not
-- float), FK-enforced tenant isolation, KPI time-bucket queries indexed,
-- exact-prefix cache (PA-4) columns included, semantic cache (Phase C)
-- deliberately deferred (pgvector extension add later, no rework needed).

-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
-- Phase A bootstrap stays usable on stock PostgreSQL.  Phase C-1's pgvector
-- extension and semantic table are applied by the explicit migration mounted
-- by the pgvector Compose deployment; do not make unrelated consumers install
-- vector just to create the ledger.

-- ============================================================
-- tenants — top-level account boundary. Even self-host/OSS single-user
-- mode gets a default tenant row so schema needs no future migration.
-- ============================================================
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    plan            TEXT NOT NULL DEFAULT 'self_host',   -- 'self_host' | 'hosted_free' | 'hosted_paid'
    spend_cap_usd   NUMERIC(12,6),                        -- NULL = uncapped
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Default tenant for self-host/BYOK mode (Phase A ships single-tenant UX,
-- schema is multi-tenant-ready). Application seeds this row on first boot.
-- INSERT INTO tenants (id, name, plan) VALUES
--   ('00000000-0000-0000-0000-000000000000', 'default', 'self_host');

-- ============================================================
-- providers — registry row = "add a provider", per adapter-api-surface.md
-- ============================================================
CREATE TABLE providers (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,          -- 'openai' | 'anthropic' | 'openrouter' | 'xai' | 'google' | 'vllm' | 'ollama'
    base_url        TEXT NOT NULL,
    adapter_class   TEXT NOT NULL,                 -- 'OpenAICompatAdapter' | 'AnthropicAdapter'
    auth_style      TEXT NOT NULL,                 -- 'bearer' | 'x-api-key' | 'api-key' | 'query-param' | 'x-goog-api-key'
    enabled         BOOLEAN NOT NULL DEFAULT true,
    pricing_json_url TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_adapter_class CHECK (adapter_class IN ('OpenAICompatAdapter', 'AnthropicAdapter')),
    CONSTRAINT chk_auth_style CHECK (auth_style IN ('bearer', 'x-api-key', 'api-key', 'query-param', 'x-goog-api-key', 'none'))
);

-- ============================================================
-- api_keys — never store raw keys. Hash + last-4 for display only.
-- BYOK: tenant supplies their own upstream credential at request time;
-- this table is the *proxy-facing* key (what the caller authenticates
-- to us with), not the upstream provider credential — those are never
-- persisted per adapter-api-surface.md C10/AC-A13.
-- ============================================================
CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    key_hash        TEXT NOT NULL UNIQUE,           -- sha256/argon2 hash, never plaintext
    key_last4       TEXT NOT NULL,                  -- display only
    scopes          TEXT[] NOT NULL DEFAULT '{}',
    spend_cap_usd   NUMERIC(12,6),                   -- per-key override of tenant cap
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'revoked' | 'rotated'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    CONSTRAINT chk_key_status CHECK (status IN ('active', 'revoked', 'rotated'))
);
CREATE INDEX idx_api_keys_tenant ON api_keys(tenant_id) WHERE status = 'active';

-- ============================================================
-- requests — the ledger. Every row is an immutable fact for KPI
-- reconciliation (AC-A12); no UPDATE after insert except stream-completion
-- backfill of usage/latency (handled as a single UPDATE at request end,
-- never a partial-aggregate mutation).
-- ============================================================
CREATE TABLE requests (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    api_key_id              UUID REFERENCES api_keys(id) ON DELETE RESTRICT,
    provider_id             INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    model                   TEXT NOT NULL,
    route                   TEXT NOT NULL,           -- 'compress' | 'passthrough'
    cache_status            TEXT NOT NULL DEFAULT 'miss',  -- 'miss' | 'exact_hit' | 'semantic_hit' | 'semantic_threshold_miss'
    input_tokens_before     INTEGER NOT NULL,
    input_tokens_after      INTEGER NOT NULL,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    est_cost_before         NUMERIC(14,8) NOT NULL DEFAULT 0,   -- NUMERIC not REAL: no float drift (AC-A11/A12)
    est_cost_after          NUMERIC(14,8) NOT NULL DEFAULT 0,
    cache_savings           NUMERIC(14,8) NOT NULL DEFAULT 0,   -- reported separately from compression savings (AC-A6)
    l1_tokens_stripped      INTEGER NOT NULL DEFAULT 0,          -- B3: L1 structural-clean savings, separate from cache/compression
    l1_savings              NUMERIC(14,8) NOT NULL DEFAULT 0,    -- B3: est. USD of stripped tokens; 0 on cache-hit rows (never summed with cache_savings)
    tool_compression_saved  INTEGER NOT NULL DEFAULT 0,           -- T1: tool-result/schema lossless savings; attribution subset, never additive with L1 totals
    latency_ms              NUMERIC(10,2) NOT NULL DEFAULT 0,
    compressed              BOOLEAN NOT NULL DEFAULT false,
    status                  INTEGER NOT NULL DEFAULT 0,          -- HTTP status returned to caller
    error_kind              TEXT,                                -- ProviderError.kind, NULL if none (C8)
    -- AC-P6f live tripwire loop: NULL dose_tier/grounded_risk means the
    -- discriminator never ran on the request (conciseness off / passthrough
    -- route) — itself a signal the missed-grounding rule consumes.
    dose_tier               TEXT,                                -- resolved tier at request time: 'none' | 'bounded' | 'full'
    grounded_risk           TEXT,                                -- discriminator risk: 'none' | 'bounded' | 'fidelity_critical'
    envelope_shape          INTEGER,                             -- AC-P6j scanner hit on the raw request content (1/0); NULL = no content logged
    measurement_tag         TEXT,                                -- AC-P6f: stamp from a measurement deployment (TOKEN_SAVER_MEASUREMENT_TAG); /api/tripwire excludes tagged rows
    -- generated column: pre-computed day bucket, indexable without a
    -- function wrapper (fixes the SQLite idx_requests_ts non-indexable
    -- date() expression issue flagged in the v1 schema review)
    day_bucket              DATE GENERATED ALWAYS AS ((ts AT TIME ZONE 'UTC')::date) STORED,
    CONSTRAINT chk_route CHECK (route IN ('compress', 'passthrough')),
    CONSTRAINT chk_cache_status CHECK (cache_status IN ('miss', 'exact_hit', 'semantic_hit', 'semantic_threshold_miss'))
);

-- KPI time-bucket queries: per-tenant and per-provider dashboards filter by
-- ts range first, so tenant_id/provider_id lead the composite index.
CREATE INDEX idx_requests_tenant_ts ON requests(tenant_id, ts);
CREATE INDEX idx_requests_provider_ts ON requests(provider_id, ts);
CREATE INDEX idx_requests_day_bucket ON requests(day_bucket);
-- Secret redaction (C10): no key material column exists on this table by
-- design — auth headers are never written to the ledger.

-- ============================================================
-- backfill_batches — idempotency marker for the SQLite -> Postgres
-- migration. A source checksum is unique, and the marker is inserted in
-- the same transaction as the ledger rows so failed verification rolls back
-- both data and the marker.
-- ============================================================
CREATE TABLE backfill_batches (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_row_count  BIGINT NOT NULL,
    checksum          TEXT NOT NULL UNIQUE,
    completed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- cache_entries — exact-prefix cache only in Phase A (PA-4). Keyed by a
-- hash of the canonicalized static prefix + model + provider, so the
-- lookup is a plain btree equality — no pgvector needed until Phase C
-- semantic caching, at which point this table gets an embedding column
-- and an ivfflat/hnsw index added via ALTER, not a rebuild.
-- ============================================================
CREATE TABLE cache_entries (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider_id     INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    prefix_hash     TEXT NOT NULL,          -- sha256 of canonicalized static prefix
    model           TEXT NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_hit_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX idx_cache_entries_lookup ON cache_entries(tenant_id, provider_id, model, prefix_hash);
CREATE INDEX idx_cache_entries_expiry ON cache_entries(expires_at);

-- ============================================================
-- Notes for @application-developer / @qa-lead:
-- 1. AC-A11 migration gate: backfill script (SQLite requests -> Postgres
--    requests) must map legacy REAL costs to NUMERIC losslessly (cast via
--    text, not direct float cast, to avoid introducing drift on the way
--    IN) and assign every backfilled row to the seeded default tenant +
--    a synthetic "legacy" provider row.
-- 2. AC-A12 reconciliation: totals/time-buckets must be computed via SUM()
--    over requests directly in /api/kpis — no separate aggregate/rollup
--    table in Phase A, so there is nothing that can drift from the ledger.
--    (Rollup/materialized views are a Phase B perf item, not now.)
-- 3. AC-A13: api_keys.key_hash is the only place key material lives, and
--    it's a hash. Upstream BYOK credentials are never persisted anywhere
--    in this schema — confirmed no column carries them.
-- 4. ON DELETE RESTRICT on tenants/providers/api_keys FKs from requests is
--    deliberate: a tenant or provider cannot be hard-deleted while it has
--    ledger history (audit integrity). Deactivate via status/enabled
--    flags instead; only cache_entries cascades (it's disposable).
