-- v1.1 semantic-cache ledger namespace columns.
-- Apply after postgres-schema-v2.sql and before enabling semantic caching.
-- NULL is intentional for miss/exact_hit/semantic_threshold_miss rows; only
-- rows served by the semantic cache are stamped with both process-derived
-- namespaces.

BEGIN;

ALTER TABLE requests
    ADD COLUMN embedding_version TEXT NULL,
    ADD COLUMN quality_version TEXT NULL;

COMMIT;
