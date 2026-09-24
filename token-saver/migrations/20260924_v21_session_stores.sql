-- V2.1 session stores and savings data contracts (t_cfccc06c).
-- Implements the smallest Postgres-first retention surface ratified in
-- docs/v2.1-scope-ratification.md for the six V2.1 lanes:
--
--   tocp_continuations   — TOCP (plan §4.B): complete over-cap tool output,
--                          TTL-bounded, tenant/api-key/session scoped.
--   idcp_file_versions   — IDCP (plan §4.C): session-scoped immutable
--                          file-version ledger (path, hash, version id, bytes).
--   mtcc_turns           — MTCC (plan §4.E): verbatim original conversation
--                          turns with exact-source retrieval metadata.
--   strategy_telemetry   — ATBA + registry (plan §4.D/§4.F): per-lane
--                          decision/eligibility evidence, no savings fields.
--
-- Design rules (binding, from the ratification):
-- * Isolation is non-negotiable: every row is bound to tenant + api-key
--   scope + session. Tenant and api_key FKs are ON DELETE CASCADE (these
--   stores are disposable derived state, like cache_entries — never audit
--   history). Session scoping is enforced by composite unique/index keys;
--   every retrieval query must include tenant_id AND session_id.
-- * Omission is never irreversible without a retention policy: originals
--   are stored verbatim (BYTEA) with sha256/octet_length database-enforced,
--   and every store carries NOT NULL expires_at (TTL). Purge is documented
--   in MIGRATIONS.md ("V2.1 session stores").
-- * No double counting: no savings columns are added here. New lanes have no
--   demonstrated attribution gap yet (the provider-cache gap was closed in
--   20260924_v2_provider_cache_usage.sql); per-lane savings columns are
--   added only by each lane's own migration-before-writer change, keeping
--   the ledger decomposition non-overlapping and speculative DDL out.
-- * Bytes are authoritative, JSONB projections are not: mirrors
--   semantic_cache_responses — JSONB normalizes whitespace/key order, so
--   verbatim originals live in BYTEA with a database-checked sha256.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS /
-- COMMENT IF-present guard, so one file serves both the fresh Compose initdb
-- mount and re-runs on existing volumes (same convention as migrations
-- 20260922_* and 20260924_v2). Like all migrations, apply with
-- ON_ERROR_STOP=1.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- digest() for sha256 checks

-- ============================================================
-- TOCP continuation store (plan §4.B)
-- One row per over-cap tool result. The proxy returns summary +
-- continuation_id; the model fetches bounded segments back through the
-- registered retrieval path. Retrieval MUST filter on tenant_id AND
-- session_id AND continuation_id AND expires_at > now().
-- ============================================================
CREATE TABLE IF NOT EXISTS tocp_continuations (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    api_key_id        UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    session_id        TEXT NOT NULL,
    continuation_id   TEXT NOT NULL,            -- opaque id handed to the model
    model             TEXT NOT NULL,
    tool_name         TEXT NOT NULL,
    result_status     TEXT NOT NULL,            -- e.g. 'success' | 'error'
    exit_code         INTEGER,                  -- NULL = tool has no exit code
    summary           TEXT NOT NULL,            -- bounded preview returned in lieu of full output
    content           BYTEA NOT NULL,           -- complete original output, authoritative bytes
    content_sha256    TEXT NOT NULL,
    content_length    INTEGER NOT NULL,
    segment_bytes     INTEGER NOT NULL DEFAULT 0,   -- bounded retrieval segment size; 0 = whole-content retrieval
    segment_count     INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_tocp_continuations_tenant_id
        UNIQUE (tenant_id, continuation_id),
    CONSTRAINT chk_tocp_ids_nonempty
        CHECK (length(session_id) > 0 AND length(continuation_id) > 0
               AND length(tool_name) > 0 AND length(result_status) > 0),
    CONSTRAINT chk_tocp_content_digest
        CHECK (
            content_sha256 ~ '^[0-9a-f]{64}$'
            AND encode(digest(content, 'sha256'), 'hex') = content_sha256
        ),
    CONSTRAINT chk_tocp_content_length
        CHECK (content_length >= 0 AND content_length = octet_length(content)),
    CONSTRAINT chk_tocp_segment_bytes
        CHECK (segment_bytes >= 0),
    CONSTRAINT chk_tocp_ttl_positive
        CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_tocp_continuations_lookup
    ON tocp_continuations (tenant_id, session_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_tocp_continuations_expiry
    ON tocp_continuations (expires_at);
COMMENT ON TABLE tocp_continuations IS
    'V2.1 TOCP: TTL-bounded session-scoped store of complete over-cap tool outputs. Retrieval paths must filter tenant_id AND session_id AND expires_at > now(); never re-run the original tool to recover output.';

-- ============================================================
-- IDCP file-version ledger (plan §4.C)
-- One row per immutable (session, canonical path, version). First read
-- inserts a version; unchanged reads reuse the hash; changed reads insert
-- a new version. Diff/fallback decisions are application logic; the store
-- only guarantees immutability, identity, and verbatim reconstruction.
-- ============================================================
CREATE TABLE IF NOT EXISTS idcp_file_versions (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    api_key_id        UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    session_id        TEXT NOT NULL,
    canonical_path    TEXT NOT NULL,
    version_id        TEXT NOT NULL,            -- immutable version identity
    content           BYTEA NOT NULL,           -- full file content at that version
    content_sha256    TEXT NOT NULL,
    content_length    INTEGER NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_idcp_file_versions_identity
        UNIQUE (tenant_id, session_id, canonical_path, version_id),
    CONSTRAINT chk_idcp_ids_nonempty
        CHECK (length(session_id) > 0 AND length(canonical_path) > 0
               AND length(version_id) > 0),
    CONSTRAINT chk_idcp_content_digest
        CHECK (
            content_sha256 ~ '^[0-9a-f]{64}$'
            AND encode(digest(content, 'sha256'), 'hex') = content_sha256
        ),
    CONSTRAINT chk_idcp_content_length
        CHECK (content_length >= 0 AND content_length = octet_length(content)),
    CONSTRAINT chk_idcp_ttl_positive
        CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_idcp_file_versions_lookup
    ON idcp_file_versions (tenant_id, session_id, canonical_path, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_idcp_file_versions_expiry
    ON idcp_file_versions (expires_at);
COMMENT ON TABLE idcp_file_versions IS
    'V2.1 IDCP: session-scoped immutable file-version ledger (canonical path, sha256, version id, verbatim bytes). Version rows are never mutated; supersession is a new row. Retrieval must filter tenant_id AND session_id.';

-- ============================================================
-- MTCC original-turn store (plan §4.E)
-- Verbatim originals for conversation turns. Compression/collapse decisions
-- write compressed_summary + relevance_tier alongside the never-mutated
-- original; exact retrieval returns content verbatim. turn_index is the
-- per-session append-only ordinal.
-- ============================================================
CREATE TABLE IF NOT EXISTS mtcc_turns (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    api_key_id          UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL,
    turn_index          INTEGER NOT NULL CHECK (turn_index >= 0),
    role                TEXT NOT NULL,          -- provider-normalized role
    content             BYTEA NOT NULL,         -- verbatim original turn, authoritative bytes
    content_sha256      TEXT NOT NULL,
    content_length      INTEGER NOT NULL,
    relevance_tier      TEXT,                   -- 'high' | 'medium' | 'low'; NULL = not yet scored
    compressed_summary  TEXT,                   -- inspectable structured facts; NULL = retained verbatim
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_mtcc_turns_identity
        UNIQUE (tenant_id, session_id, turn_index),
    CONSTRAINT chk_mtcc_ids_nonempty
        CHECK (length(session_id) > 0 AND length(role) > 0),
    -- MTCC hard rule (plan §4.E): originals are never removed while TTL is
    -- active. There is no state in which content is absent but the row stays,
    -- so no CHECK can express "compressed without original" — enforced by
    -- never mutating content. compressed_summary may only coexist with it.
    CONSTRAINT chk_mtcc_relevance_tier
        CHECK (relevance_tier IS NULL OR relevance_tier IN ('high', 'medium', 'low')),
    CONSTRAINT chk_mtcc_content_digest
        CHECK (
            content_sha256 ~ '^[0-9a-f]{64}$'
            AND encode(digest(content, 'sha256'), 'hex') = content_sha256
        ),
    CONSTRAINT chk_mtcc_content_length
        CHECK (content_length >= 0 AND content_length = octet_length(content)),
    CONSTRAINT chk_mtcc_ttl_positive
        CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_mtcc_turns_lookup
    ON mtcc_turns (tenant_id, session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_mtcc_turns_expiry
    ON mtcc_turns (expires_at);
COMMENT ON TABLE mtcc_turns IS
    'V2.1 MTCC: verbatim original conversation turns per session with exact-source retrieval. content is never mutated; compression writes compressed_summary/relevance_tier alongside it. Retrieval must filter tenant_id AND session_id.';

-- ============================================================
-- Strategy telemetry (plan §4.D ATBA shadow evaluation + §4.F registry)
-- Evidence surface, NOT a savings ledger: no token/cost/denominator fields.
-- Per-request per-lane decisions land here so shadow evaluation, fallback
-- rates, and flag audits are queryable without touching the requests
-- ledger decomposition. strategy is deliberately free-text (nonempty) —
-- the ratified six lanes must not require a CHECK migration when the
-- registry reports existing V2.0 lanes alongside them.
-- ============================================================
CREATE TABLE IF NOT EXISTS strategy_telemetry (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    api_key_id        UUID REFERENCES api_keys(id) ON DELETE CASCADE,
    session_id        TEXT,                     -- NULL = request not session-scoped
    ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy          TEXT NOT NULL,            -- lane name, e.g. 'atba' | 'tocp' | 'l1'
    decision          TEXT NOT NULL,            -- 'eligible' | 'applied' | 'skipped' | 'fallback' | 'shadow'
    reason            TEXT,                     -- registry reason code / explanation
    strategy_version  TEXT,                     -- lane implementation version
    flag_enabled      BOOLEAN NOT NULL DEFAULT false,
    latency_ms        NUMERIC(10,2),
    metadata          JSONB,                    -- lane-specific evidence dimensions
    CONSTRAINT chk_strategy_ids_nonempty
        CHECK (length(strategy) > 0 AND length(decision) > 0),
    CONSTRAINT chk_strategy_decision
        CHECK (decision IN ('eligible', 'applied', 'skipped', 'fallback', 'shadow')),
    CONSTRAINT chk_strategy_latency
        CHECK (latency_ms IS NULL OR latency_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_strategy_telemetry_tenant_ts
    ON strategy_telemetry (tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_strategy_telemetry_strategy_ts
    ON strategy_telemetry (strategy, ts);
COMMENT ON TABLE strategy_telemetry IS
    'V2.1 ATBA/registry telemetry: per-lane eligibility/decision evidence (shadow evaluation, fallback, flag audit). Deliberately carries no savings fields — attribution stays in the requests ledger with its own non-overlapping decomposition.';

COMMIT;
