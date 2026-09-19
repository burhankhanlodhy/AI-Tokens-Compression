# Phase C-1 pgvector cutover runbook

This runbook covers AC-PC1/AC-PC2 for the Token Saver Postgres deployment. The
base schema remains usable on stock `postgres:16`; the semantic-cache objects
must be applied only on the pinned `pgvector/pgvector:0.8.6-pg16-bookworm`
service.

## Safety boundary

A Postgres data directory must not be restarted on a different libc/image
family in place. A musl-to-glibc (or glibc-to-musl) transition is a logical
cutover: quiesce writes, take a custom-format dump, restore into a fresh
volume, apply the schema/migration, compare the ledger invariants, and retain
the old volume and dump until acceptance passes. `REINDEX` is not a substitute
for that procedure.

If an image swap has already happened on the existing volume, stop application
writes immediately and create the dump before any further schema change. The
exceptional in-place recovery below is a risk decision, not the normal
procedure.

## Preflight and rollback point

Run from the repository checkout. Do not put passwords on the command line;
source the ignored `.env` through Compose or use the container's local
administrative authentication.

```bash
docker compose stop proxy
mkdir -p token-saver/backups

docker exec token-saver-postgres-1 pg_dump \
  -U postgres -d token_saver --format=custom \
  --file=/tmp/token_saver_phase_c1.dump
docker cp token-saver-postgres-1:/tmp/token_saver_phase_c1.dump \
  token-saver/backups/token_saver_phase_c1_UTCSTAMP.dump
docker cp token-saver/backups/token_saver_phase_c1_UTCSTAMP.dump \
  token-saver-postgres-1:/tmp/token_saver_phase_c1.dump
docker exec token-saver-postgres-1 \
  pg_restore --list /tmp/token_saver_phase_c1.dump
```

The dump must be non-empty and `pg_restore --list` must show a custom-format
TOC before proceeding. Keep the backup directory out of Git; it is a local
rollback artifact.

Record these values before and after the cutover:

```sql
SELECT current_setting('server_version'), current_setting('server_encoding'),
       datcollate, datctype
  FROM pg_database WHERE datname = current_database();
SELECT extname, extversion FROM pg_extension ORDER BY 1;
SELECT count(*) AS requests,
       sum(input_tokens_before) AS input_tokens,
       sum(est_cost_before) AS cost_before,
       sum(est_cost_after) AS cost_after
  FROM requests;
```

Also record the image digest, volume name, and container creation time with
`docker inspect`. A clean ASCII-only probe does not prove that a cross-libc
swap is safe; it only informs the recovery decision.

## Recovery decision for an already-swapped volume

The preferred path remains dump/restore into a fresh volume. An in-place
recovery is acceptable only when all of these are true: writes are quiesced,
the custom-format dump is verified, independent row/token/cost totals are
recorded, all relevant indexes pass `amcheck`, and the operator accepts that
the old data directory was already crossed by the image boundary.

For the Phase C incident, the dump was verified, the ledger invariants were
recorded, the btree integrity check passed, and the existing data had no
non-ASCII `model`/`route` values. The selected recovery was therefore
`REINDEX DATABASE token_saver` in place. This does not change the default
recommendation for a future image-family transition: restore into a fresh
volume instead.

## Apply PC1/PC2

The migration is deliberately fire-once for the table and indexes. Pipe the
committed host file into `psql`; do not execute a possibly stale copy from
`/docker-entrypoint-initdb.d/`.

```bash
docker exec -i token-saver-postgres-1 \
  psql -U postgres -d token_saver -v ON_ERROR_STOP=1 -f - \
  < token-saver/migrations/20260918_pc1_pgvector.sql
```

Verify the extension and object set directly in the live database:

```sql
SELECT extname, extversion
  FROM pg_extension WHERE extname IN ('vector', 'pgcrypto') ORDER BY 1;
SELECT to_regclass('public.semantic_cache_entries');
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE schemaname = 'public' AND tablename = 'semantic_cache_entries'
 ORDER BY indexname;
```

Acceptance requires `vector` version `0.8.6`, the table, the identity unique
index, HNSW cosine index, scope index, expiry index, and primary key. The table
must retain its fixed `vector(1536)` dimension and compatibility-scope
columns.

## Integrity and idempotency checks

After an in-place recovery, reindex the database during the write pause:

```bash
docker exec token-saver-postgres-1 \
  psql -U postgres -d token_saver -v ON_ERROR_STOP=1 \
  -c 'REINDEX DATABASE token_saver;'
```

Install `amcheck` only for the check, run `bt_index_check` on every user btree
index, and drop the extension afterward. The check must report no failures.

For a disposable database on the same pinned image, apply the committed base
schema followed by `20260918_pc1_pgvector.sql`; apply the migration a second
time and require a loud `relation already exists` failure. Read back the
extension, table, and all five indexes after the failed transaction to prove
that the schema is intact rather than relying on `IF NOT EXISTS` everywhere.
The reproducible CI implementation is
`token-saver/test/pc1_pgvector_acceptance.sh`.

## AC-PC4 filtered-HNSW EXPLAIN/latency gate

This gate must be rerun after the semantic-cache writer has populated the table
and before `SEMANTIC_CACHE_ENABLED` is changed. The production database had
`0` semantic-cache rows at the time of the initial probe (the ledger had `5,540`
rows), so the probe below used a disposable database on the same pinned
`pgvector/pgvector:0.8.6-pg16-bookworm` image with `20,000` real indexed rows
(`308 MB` table/index footprint). It used the exact CTE, compatibility filters,
`ORDER BY embedding <=> ... LIMIT 1`, and outer threshold predicate from
`proxy/semantic_cache.py`.

The first result is a release blocker, not an acceptable benchmark:

- With the server default `hnsw.ef_search = 40`, `EXPLAIN (ANALYZE, BUFFERS)`
  chose a **Seq Scan** over `semantic_cache_entries`, scanning `20,000` rows;
  observed execution time was `130.939 ms`.
- With a session override `hnsw.ef_search = 100`, the same parameterized query
  chose `Index Scan using idx_semantic_cache_embedding_hnsw`; observed execution
  time was `0.606 ms` and the plan reported `Buffers: shared hit=210 read=1`.
  Across 200 custom-plan executions on the 20,000-row fixture, compatible-hit
  latency was p50 `812.4 us`, p95 `1,036.6 us`, p99 `1,289.7 us`, max
  `1,607.3 us`; a compatible threshold miss was p95 `815.4 us`.
- A persistent psycopg connection that crossed its automatic prepare threshold
  also fell to a generic **Seq Scan** (`126.491 ms`). The current seam opens a
  connection per lookup, but a future pool must either keep custom planning or
  explicitly pin the HNSW session setting; do not assume the first custom plan
  is representative.

**Acceptance condition:** the live application query must show the HNSW index
scan at the intended traffic row volume and record hit, threshold-miss, and
no-compatible-row latency percentiles. Until the application sets and verifies
an HNSW-safe session plan (or an equivalent reviewed query/connection policy),
AC-PC4 remains red and the semantic-cache flag stays off. The disposable probe
is evidence of the planner behavior and is not a claim that production-volume
latency has been ratified while the live semantic table is empty.

## Restore path when the preferred cutover is required

1. Stop the proxy and preserve the original volume; never delete it as part of
   the first rollback attempt.
2. Create a new volume and a fresh container using the pinned image.
3. Create `token_saver` from the base schema, restore the custom-format dump
   with `pg_restore`, and then apply the committed PC1/PC2 migration.
4. Compare row count, token sum, cost sums at `NUMERIC(14,8)` scale, extension
   versions, required indexes, and `amcheck` results with the pre-cutover
   record.
5. Start the proxy only after health, KPI, and ledger read-back checks pass;
   retain both the old volume and dump until the acceptance window closes.

## CI lane

The stock `postgres:16` service in `.github/workflows/ci.yml` is unchanged.
The separate `pc1-pgvector` job starts the exact pinned image and runs the
acceptance script, including a transactional second-application failure
check. This prevents a green stock-Postgres lane from being mistaken for
proof that `vector` is installed or that PC1/PC2 is live.
