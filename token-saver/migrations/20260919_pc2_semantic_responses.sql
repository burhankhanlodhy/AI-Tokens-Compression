-- Phase C-1 response payload store (AC-PC2/AC-PC3).
--
-- Apply after 20260918_pc1_pgvector.sql. This is deliberately fire-once:
-- do not hide a partially-applied response-store deployment with IF NOT EXISTS.
-- The writer must insert the response row and semantic_cache_entries row in one
-- transaction, using the same tenant_id/response_ref and expires_at values.
--
-- payload is the JSONB projection required by AC-PC2. payload_bytes is the
-- authoritative UTF-8 response body: JSONB normalizes whitespace/key order, so
-- it cannot by itself satisfy the contract to replay the provider body
-- verbatim or validate the original sha256/content_length. The writer stores
-- the exact relayed bytes in payload_bytes and the parsed projection in payload.

BEGIN;

CREATE TABLE semantic_cache_responses (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    response_ref    TEXT NOT NULL,
    payload         JSONB NOT NULL,
    payload_bytes   BYTEA NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1,
    sha256          TEXT NOT NULL,
    content_length  INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_semantic_cache_responses_tenant_ref
        UNIQUE (tenant_id, response_ref),
    CONSTRAINT chk_semantic_response_ref_nonempty
        CHECK (length(response_ref) > 0),
    CONSTRAINT chk_semantic_response_schema_version
        CHECK (schema_version = 1),
    CONSTRAINT chk_semantic_response_sha256
        CHECK (
            sha256 ~ '^[0-9a-f]{64}$'
            AND encode(digest(payload_bytes, 'sha256'), 'hex') = sha256
        ),
    CONSTRAINT chk_semantic_response_content_length
        CHECK (
            content_length >= 0
            AND content_length = octet_length(payload_bytes)
        ),
    CONSTRAINT chk_semantic_response_payload_projection
        CHECK (payload = convert_from(payload_bytes, 'UTF8')::jsonb)
);

-- Pair the response reference with the tenant boundary already carried by the
-- entry. ON DELETE CASCADE makes a deliberate response purge remove dependent
-- semantic entries rather than leaving dangling hits; lookup still treats any
-- pre-existing/corrupt missing reference as a clean miss.
ALTER TABLE semantic_cache_entries
    ADD CONSTRAINT fk_semantic_cache_entry_response
    FOREIGN KEY (tenant_id, response_ref)
    REFERENCES semantic_cache_responses (tenant_id, response_ref)
    ON DELETE CASCADE;

COMMIT;
