PC4 traffic-shaped pgvector calibration and deployment audit
==============================================================

Decision
--------
NO-GO for v1.1 semantic-cache threshold ratification. No measured ef_search x cosine-threshold point met the conservative release bar: zero exact hard-negative admissions, zero HNSW false hits, at least 90% positive exact coverage, at least 95% HNSW recall of eligible positives, and p95 latency <=100 ms. This is an evidence-based NO-GO, not a guessed default. Semantic caching remains disabled.

The machine-readable authoritative/provisional result is:
  token-saver/benchmark/results/pc4_real_embeddings_20260921T021941Z.json
The live safety audit is:
  token-saver/benchmark/results/pc4_live_safety_audit_20260921T021941Z.json

Corpus and embedding contract
-----------------------------
- Committed corpus: token-saver/benchmark/fixtures/pc4_real_prompt_pairs.json
- Corpus SHA-256: e3fa67aea1505c60be16fcc42f43a40a58928558a11841cf2411b99d436bd0e3
- 48 labeled pairs: 24 positive paraphrases and 24 realistic hard negatives.
- 72 unique prompts were embedded in one request using text-embedding-3-small.
- Response model: text-embedding-3-small; dimensions: 1536.
- Namespace: embedding_version openai:text-embedding-3-small@1536; quality_version pc4-real-corpus-v1; request-parameter scope pc4-traffic-v1.
- Embeddings were obtained from the OpenAI-compatible OpenRouter endpoint; no API key is stored in the corpus, runner, or artifacts.

Isolated database and workload
------------------------------
- Scratch database: dba_verify_pc4_real_219 (dropped in the runner finally path).
- PostgreSQL: 16.15 on aarch64; pgvector extension: 0.8.6.
- 11,500 semantic rows: 10,000 target-scope rows, 500 same-compatible rows in tenant B, 500 other-provider rows, and 500 embedding/version/parameter-mismatch rows. The 10,000 target rows are the requested traffic-shaped dominant volume; selectivity populations are additional by design.
- Target rows use real corpus vectors. Filler rows repeat those real vectors, so canonical-row exact distances are unchanged while HNSW sees the full traffic-shaped volume.
- Five parameterized mandatory scope filters were exercised: tenant, provider, model, embedding version, quality version, and request-parameter hash (plus embedding model/dimensions and expiry).
- Each grid cell has one measured observation per corpus case; p50/p95/p99 are computed over 48 latency observations. The runner supports repeated observations and was executed with repetitions=1 and warmups=0 after the initial Pi run exceeded the practical wall clock with five repeats.

Grid summary
------------
Columns: ef_search | threshold | positive exact coverage | HNSW recall | false-hit rate | exact negative hits | p95 ms

 100 | 0.05 |   0.00% |   0.00% |  0.00% | 0 |   1.389
 100 | 0.08 |   0.00% |   0.00% |  0.00% | 0 |   1.360
 100 | 0.10 |   0.00% |   0.00% |  0.00% | 0 |   1.719
 100 | 0.12 |   0.00% |   0.00% |  0.00% | 0 |   1.856
 100 | 0.15 |   0.00% |   0.00% |  0.00% | 0 |   1.330
 100 | 0.18 |   0.00% |   0.00% |  0.00% | 0 |   1.323
 100 | 0.22 |   8.33% | 100.00% |  0.00% | 1 |   1.810
 100 | 0.28 |  41.67% |  80.00% |  4.17% | 3 |   1.796
 100 | 0.35 |  54.17% |  69.23% | 12.50% | 7 |   1.354
 300 | 0.05 |   0.00% |   0.00% |  0.00% | 0 |   1.665
 300 | 0.08 |   0.00% |   0.00% |  0.00% | 0 |   2.266
 300 | 0.10 |   0.00% |   0.00% |  0.00% | 0 |   1.631
 300 | 0.12 |   0.00% |   0.00% |  0.00% | 0 |   1.663
 300 | 0.15 |   0.00% |   0.00% |  0.00% | 0 |   1.622
 300 | 0.18 |   0.00% |   0.00% |  0.00% | 0 |   1.697
 300 | 0.22 |   8.33% | 100.00% |  0.00% | 1 |   2.235
 300 | 0.28 |  41.67% |  80.00% |  4.17% | 3 |   1.793
 300 | 0.35 |  54.17% |  69.23% | 12.50% | 7 |   1.746
1000 | 0.05 |   0.00% |   0.00% |  0.00% | 0 | 150.453
1000 | 0.08 |   0.00% |   0.00% |  0.00% | 0 | 155.738
1000 | 0.10 |   0.00% |   0.00% |  0.00% | 0 | 154.203
1000 | 0.12 |   0.00% |   0.00% |  0.00% | 0 | 150.111
1000 | 0.15 |   0.00% |   0.00% |  0.00% | 0 | 155.652
1000 | 0.18 |   0.00% |   0.00% |  0.00% | 0 | 156.908
1000 | 0.22 |   8.33% | 100.00% |  4.17% | 1 | 154.833
1000 | 0.28 |  41.67% | 100.00% | 12.50% | 3 | 158.511
1000 | 0.35 |  54.17% | 100.00% | 29.17% | 7 | 158.180

This also documents a known runner-order discrepancy: the compact table above reports ef_search=100 p95 values around 1.3–1.9 ms, while the committed result artifact reports ef_search=100 p95 values around 140–158 ms (and ef_search=300 around 1.5–1.9 ms). The runs used repetitions=1 and warmups=0; treat this as an ordering/warmup artifact for the latency display, not as a production claim. It does not change the NO-GO: correctness fails before latency can bind.

Interpretation: thresholds below 0.22 admit no positives in this corpus. At 0.22, only 2 of 24 positive exact distances are in-band and one hard negative is already an exact admission. Higher thresholds increase coverage while admitting hard negatives; ef_search=1000 restores approximate recall for some cells but exceeds the p95 latency bar and increases false hits. A false hit is a correctness failure, so this cannot be converted into a production default.

Plan and isolation evidence
---------------------------
The runner captured EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) for the lowest-false-hit safety point (ef_search=300, threshold=0.05, case pos-01) at the full seeded volume. The plan contains:
- Subquery Scan -> Limit -> Index Scan on idx_semantic_cache_embedding_hnsw.
- Provider lookup uses providers_name_key.
- HNSW Index Scan actual rows=1, actual total time=1.070 ms, shared hit blocks=294, shared read blocks=0.
- Total planning time=1.259 ms; execution time=1.168 ms.

This proves the filtered hot-path plan uses HNSW at traffic-shaped volume, but it does not override the NO-GO correctness result.

- Cross-tenant probes: 48; cross-tenant leakage hits: exactly 0.
- Compatibility mismatch probes: 240; returned rows: 0.
- The production seam refused an incomplete scope before opening a query: ValueError, "mandatory semantic lookup filters are required".
- Scratch database was removed after the run; no calibration rows remain in the application database.

Live-volume safety audit
------------------------
The live database was never used for calibration writes. Before and after snapshots were identical:
- requests: 5,875
- input_tokens_before: 1,941,640
- input_tokens_after: 1,652,230
- output_tokens: 3,413,598
- est_cost_before: 9.13040365
- est_cost_after: 9.04358065
- cache_savings: 0.00000000
- semantic_cache_entries: 0
- semantic_cache_responses: 0

A custom-format pg_dump was created and retained at /tmp/t3994978d_token_saver_20260921T021941Z.dump (172,673 bytes; SHA-256 3e2e34fac60a1ad9b06e3f06b188c8aada676bb6be437df2cf3216550e8dd216). It was restored to a disposable database, and all listed counts and independent sums matched exactly; the restored database reported vector extension 0.8.6 and five semantic-entry indexes before it was dropped. The committed base schema plus PC1 and PC2 migrations were also applied to a separate scratch database and verified before cleanup.

Runtime context
---------------
- Host: Raspberry Pi 5 Model B Rev 1.0, aarch64 Linux 6.18.34+rpt-2712, 4 CPUs, 7.9 GiB RAM.
- Container image: pgvector/pgvector:0.8.6-pg16-bookworm.
- The standing proxy container still reports SEMANTIC_CACHE_ENABLED=false after the transient pre-query guard probe; the output artifact records false.

Reproducibility
---------------
Run the committed runner with a pgvector Postgres admin/base DSN, live DSN, and an OpenAI-compatible embedding API key. The runner creates its own uniquely named database, applies committed schema files, requests the pinned model, seeds the traffic shape, records the JSON artifact, and drops the database in finally. Do not point calibration writes at token_saver.
