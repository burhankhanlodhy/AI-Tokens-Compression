-- AC-3/AC-4 probe: seed + reconciliation + EXPLAIN on hot paths
\set ON_ERROR_STOP on
BEGIN;
INSERT INTO tenants (id, name, plan) VALUES ('00000000-0000-0000-0000-000000000000','default','self_host');
INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES ('legacy','http://legacy.invalid','OpenAICompatAdapter','bearer');
INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES ('openai','http://localhost:8001/v1','OpenAICompatAdapter','bearer');
INSERT INTO api_keys (tenant_id, key_hash, key_last4) VALUES ('00000000-0000-0000-0000-000000000000','deadbeef','1234');
INSERT INTO requests (tenant_id, api_key_id, provider_id, ts, model, route, input_tokens_before, input_tokens_after, output_tokens, est_cost_before, est_cost_after, cache_savings, l1_tokens_stripped, l1_savings, latency_ms, compressed, status)
VALUES
('00000000-0000-0000-0000-000000000000',(SELECT id FROM api_keys WHERE key_last4='1234'),(SELECT id FROM providers WHERE name='legacy'),'2026-09-17 23:59:59+00','gpt-4o','compress',1000,400,300,0.01000000,0.00400000,0,50,0.00005000,120,true,200),
('00000000-0000-0000-0000-000000000000',(SELECT id FROM api_keys WHERE key_last4='1234'),(SELECT id FROM providers WHERE name='openai'),'2026-09-18 00:15:00+00','gpt-4o','passthrough',500,500,200,0.00500000,0.00500000,0,0,0,80,false,200),
('00000000-0000-0000-0000-000000000000',(SELECT id FROM api_keys WHERE key_last4='1234'),(SELECT id FROM providers WHERE name='openai'),'2026-09-18 12:00:00+00','gpt-4o','compress',2000,600,800,0.02000000,0.00600000,0,120,0.00012000,150,true,200);
COMMIT;

-- generated column check: day_bucket must render UTC date, two distinct buckets for these ts
SELECT day_bucket, count(*) AS n FROM requests GROUP BY day_bucket ORDER BY day_bucket;

-- AC-3: reconciliation, ledger-only SUMs shaped like /api/kpis (compress route)
SELECT r.day_bucket,
       count(*) FILTER (WHERE r.route='compress')          AS compress_n,
       sum(r.input_tokens_before) FILTER (WHERE r.route='compress') AS in_before,
       sum(r.input_tokens_after)  FILTER (WHERE r.route='compress') AS in_after,
       sum(r.est_cost_before)     FILTER (WHERE r.route='compress') AS cost_before,
       sum(r.est_cost_after)      FILTER (WHERE r.route='compress') AS cost_after
FROM requests r
WHERE r.tenant_id='00000000-0000-0000-0000-000000000000'
GROUP BY r.day_bucket ORDER BY r.day_bucket;

-- AC-4a: EXPLAIN tenant/day hot path (dashboard per-tenant KPI bucket)
EXPLAIN (ANALYZE, VERBOSE)
SELECT count(*), sum(est_cost_after) FROM requests
WHERE tenant_id='00000000-0000-0000-0000-000000000000'
  AND ts >= '2026-09-01 00:00:00+00' AND ts < '2026-10-01 00:00:00+00';

-- AC-4b: EXPLAIN provider/day hot path
EXPLAIN (ANALYZE, VERBOSE)
SELECT count(*), sum(est_cost_after) FROM requests
WHERE provider_id=(SELECT id FROM providers WHERE name='openai')
  AND ts >= '2026-09-01 00:00:00+00' AND ts < '2026-10-01 00:00:00+00';