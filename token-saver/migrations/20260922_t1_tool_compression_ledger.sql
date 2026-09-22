-- T1 selective tool-protocol compression ledger attribution.
--
-- Apply before deploying a proxy writer that emits tool_compression_saved.
-- The field is an attributable subset of the L1/message and schema-wire
-- savings; dashboards must not add it to l1_tokens_stripped.

BEGIN;

-- The canonical fresh-volume schema already declares this final column.
-- Existing volumes need the same additive upgrade; IF NOT EXISTS makes both
-- initialization sequences valid without masking unrelated migration errors.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS tool_compression_saved INTEGER NOT NULL DEFAULT 0;

COMMIT;
