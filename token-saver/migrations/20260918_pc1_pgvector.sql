-- Phase C-1 upgrade for an existing Postgres volume (AC-PC1/AC-PC2).
--
-- Run once against the application database after switching the Postgres image
-- to pgvector/pgvector:0.8.6-pg16-bookworm.  This is deliberately a loud,
-- fire-once migration: do not hide a partially-applied deployment with
-- IF NOT EXISTS on the table or indexes.  Fresh Compose databases receive
-- this file after ../postgres-schema-v2.sql via the pgvector service mount.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE semantic_cache_entries (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider_id             INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    model                   TEXT NOT NULL,
    embedding_model         TEXT NOT NULL,
    embedding_dimensions    INTEGER NOT NULL CHECK (embedding_dimensions = 1536),
    embedding_version       TEXT NOT NULL,
    quality_version         TEXT NOT NULL,
    request_parameters_hash TEXT NOT NULL,
    canonical_prompt_hash   TEXT NOT NULL,
    embedding               vector(1536) NOT NULL,
    response_ref            TEXT NOT NULL,
    hit_count               INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_hit_at             TIMESTAMPTZ,
    expires_at              TIMESTAMPTZ NOT NULL,
    CONSTRAINT chk_semantic_cache_versions CHECK (
        length(embedding_model) > 0
        AND length(embedding_version) > 0
        AND length(quality_version) > 0
        AND length(request_parameters_hash) > 0
        AND length(canonical_prompt_hash) > 0
        AND length(response_ref) > 0
    )
);

CREATE UNIQUE INDEX idx_semantic_cache_identity
    ON semantic_cache_entries (
        tenant_id, provider_id, model, embedding_model, embedding_dimensions,
        embedding_version, quality_version, request_parameters_hash,
        canonical_prompt_hash
    );

CREATE INDEX idx_semantic_cache_embedding_hnsw
    ON semantic_cache_entries USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_semantic_cache_scope
    ON semantic_cache_entries (
        tenant_id, provider_id, model, embedding_model, embedding_dimensions,
        embedding_version, quality_version, request_parameters_hash, expires_at
    );
CREATE INDEX idx_semantic_cache_expiry ON semantic_cache_entries(expires_at);

COMMIT;
