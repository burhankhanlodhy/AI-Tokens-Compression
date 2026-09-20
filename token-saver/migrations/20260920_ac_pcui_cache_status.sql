-- AC-PC-UI cache-status taxonomy widening.
--
-- The v1.1 writer emits semantic_threshold_miss for a semantic lookup that
-- finds no compatible row within the calibrated threshold.  Apply this
-- migration before deploying that writer literal: a rejected Postgres ledger
-- insert is swallowed by the proxy's ledger-failure guard and drops the entire
-- request row, including its token/cost attribution.
--
-- This is intentionally fire-once and transactional.  Do not use IF EXISTS or
-- IF NOT EXISTS: a missing/renamed constraint means the deployment is not being
-- applied to the schema it was reviewed against.  Reapplication after the
-- widened constraint is also rejected before any DDL runs. Existing rows already
-- satisfy the old constraint, so the replacement check needs no data rewrite.

BEGIN;

DO $$
DECLARE
    current_definition TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid)
      INTO current_definition
      FROM pg_constraint
     WHERE conrelid = 'requests'::regclass
       AND conname = 'chk_cache_status';

    IF current_definition IS NULL THEN
        RAISE EXCEPTION 'chk_cache_status is missing; refusing unreviewed schema';
    ELSIF position('semantic_threshold_miss' IN current_definition) > 0 THEN
        RAISE EXCEPTION 'chk_cache_status migration already applied';
    END IF;
END
$$;

ALTER TABLE requests
    DROP CONSTRAINT chk_cache_status;

ALTER TABLE requests
    ADD CONSTRAINT chk_cache_status
    CHECK (
        cache_status IN (
            'miss',
            'exact_hit',
            'semantic_hit',
            'semantic_threshold_miss'
        )
    );

COMMIT;
