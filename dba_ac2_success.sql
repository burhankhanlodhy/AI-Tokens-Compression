-- AC-2 (c) success path + post-conditions, run AFTER the expected-failure probe
\set ON_ERROR_STOP on
BEGIN;
INSERT INTO backfill_batches (source_row_count, checksum) VALUES (1, 'ck-ok');
INSERT INTO requests (tenant_id, provider_id, ts, model, route, input_tokens_before, input_tokens_after, status)
VALUES ('00000000-0000-0000-0000-000000000000',1,'2026-09-18 13:00:00+00','gpt-4o','passthrough',100,100,200);
COMMIT;
-- post-conditions
SELECT 'markers' AS probe, count(*) AS n FROM backfill_batches
UNION ALL
SELECT 'leak_rows_at_poisoned_ts', count(*) FROM requests WHERE ts='2026-09-18 13:00:00+00';
SELECT checksum, source_row_count FROM backfill_batches ORDER BY id;