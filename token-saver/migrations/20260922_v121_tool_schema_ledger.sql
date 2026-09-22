-- v1.2.1 tool-schema optimization ledger attribution.
-- Apply before deploying the v1.2.1 writer so a Postgres ledger insert can
-- include schema cache facts without being dropped by the proxy's fail-open
-- telemetry guard.

BEGIN;

ALTER TABLE requests
    ADD COLUMN schema_cache_hit BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN schema_bytes_saved INTEGER NOT NULL DEFAULT 0;

COMMIT;