# Migrations — Postgres ledger and semantic cache

Runbook for bringing an existing token-saver Postgres deployment up to the
v1.2.1 schema (pgvector + semantic cache + cache-status taxonomy + tool-schema
ledger attribution). A **fresh**
Docker Compose volume needs none of this: the schema, extension, and entry
tables are created at container init (see
[README — Storage notes](README.md#storage-notes)).

All commands assume `TOKEN_SAVER_PG_DSN` is set (the Docker quickstart
configures it in `.env`) and are run from `token-saver/`.

## Migration inventory (apply in this order)

1. **`migrations/20260918_pc1_pgvector.sql`** — `CREATE EXTENSION vector`,
   `semantic_cache_entries`, HNSW index (`m=16, ef_construction=200`).
   Requires the pgvector image. Fire-once: fails loudly if preconditions
   are absent or already applied.
2. **`migrations/20260919_pc2_semantic_responses.sql`** —
   `semantic_cache_responses` (tenant-scoped, composite FK). Stores the
   exact relayed bytes (`payload_bytes`) so replay is byte-identical.
3. **`migrations/20260920_pc5_request_versions.sql`** —
   `requests.embedding_version`, `requests.quality_version`. NULL for
   miss/exact/threshold-miss rows; only semantic hits are stamped.
4. **`migrations/20260920_ac_pcui_cache_status.sql`** — replaces the
   pre-`b4baf47` `chk_cache_status` constraint. Do **not** skip on a legacy
   volume: the app must record all four literals including
   `semantic_threshold_miss`.
5. **`migrations/20260922_v121_tool_schema_ledger.sql`** — adds
   `requests.schema_cache_hit` and `requests.schema_bytes_saved`. Apply before
   a v1.2.1 proxy writes tool-schema telemetry; otherwise its fail-open ledger
   guard would drop the entire request row on an existing volume.

Every migration is fire-once and idempotent-hostile by design: they fail
loudly rather than silently repairing a partially-applied state. Apply each
with:

```bash
psql "$TOKEN_SAVER_PG_DSN" -v ON_ERROR_STOP=1 \
  -f migrations/<file>.sql
```

`ON_ERROR_STOP=1` is mandatory — never let a failed migration be followed by
the next one.

## Cutover procedure (existing `postgres-data` volume)

The Compose image moved from `postgres:16-alpine` (musl) to
`pgvector/pgvector:0.8.6-pg16-bookworm` (glibc). That is a **libc collation
change on an existing data directory** — pointing the old volume at the new
image directly is forbidden, and a `REINDEX` is not a cutover. Use
dump/restore into a **new** volume:

1. **Stop proxy writes and take a custom-format dump** to an absolute host
   path (not inside the container filesystem):

   ```bash
   docker compose stop proxy
   docker compose exec -T postgres pg_dump -U postgres -d token_saver \
     --format=custom > /absolute/path/token_saver_pre_pc1.dump
   ```

2. **Create a new Docker volume** and a temporary pgvector/16 container
   using it. Do not reuse `token-saver_postgres-data`. Restore:

   ```bash
   pg_restore --no-owner --exit-on-error \
     -d "<new-volume-dsn>" /absolute/path/token_saver_pre_pc1.dump
   ```

   then apply `migrations/20260918_pc1_pgvector.sql` (and the rest of the
   inventory above, in order) against the restored database.

3. **Verify old-vs-new data** before cutting traffic over (see verification
   queries below): row `COUNT(*)` and independent `SUM()` values for
   `requests.input_tokens_before`, `input_tokens_after`, `est_cost_before`,
   and `est_cost_after` must match exactly.

4. **Cut over:** run `/health` and `/api/kpis` against the restored
   database, then switch the Compose volume mapping to the new volume and
   restart the proxy.

**Rollback:** keep both the old volume and the pre-cutover dump until the
post-upgrade health/KPI checks pass. To roll back, stop the proxy, point the
Compose volume mapping back at the old volume, and restart — no data is
destroyed by the cutover itself. Never delete the rollback copy as part of
the restart.

## Verification queries

Schema/migration state (same query verifies a fresh-volume init and an
upgraded legacy volume):

```bash
psql "$TOKEN_SAVER_PG_DSN" -Atc \
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conrelid = 'requests'::regclass AND conname = 'chk_cache_status';"
# Expected: a CHECK containing miss, exact_hit, semantic_hit,
# semantic_threshold_miss.
```

```bash
psql "$TOKEN_SAVER_PG_DSN" -Atc \
  "SELECT column_name FROM information_schema.columns
    WHERE table_name = 'requests'
      AND column_name IN ('schema_cache_hit', 'schema_bytes_saved')
    ORDER BY column_name;"
# Expected: schema_bytes_saved, schema_cache_hit
```

Extension and indexes:

```bash
psql "$TOKEN_SAVER_PG_DSN" -Atc \
  "SELECT extname FROM pg_extension WHERE extname = 'vector';"
# Expected: vector

psql "$TOKEN_SAVER_PG_DSN" -Atc \
  "SELECT indexname FROM pg_indexes
    WHERE tablename IN ('semantic_cache_entries','semantic_cache_responses')
    ORDER BY indexname;"
# Expected: idx_semantic_cache_identity, idx_semantic_cache_scope,
#           idx_semantic_cache_embedding_hnsw (HNSW), plus the
#           response-store PK/FK indexes.
```

Row-count parity (run against both old and new; outputs must be identical):

```bash
psql "$TOKEN_SAVER_PG_DSN" -Atc \
  "SELECT COUNT(*),
          SUM(input_tokens_before), SUM(input_tokens_after),
          SUM(est_cost_before), SUM(est_cost_after)
     FROM requests;"
```

## Known traps

- **initdb scripts run only when the data directory is empty.** The Compose
  initdb bridge verifies the canonical schema on a fresh volume but cannot
  upgrade a legacy one — the explicit migration sequence above is the only
  upgrade path.
- **Stock-safety:** `postgres-schema-v2.sql` stays plain-Postgres
  compatible; all pgvector/HNSW DDL lives only in the pc1 migration. Never
  move `CREATE EXTENSION vector` into the bootstrap schema.
- **`ef_construction=200` is part of the ratified operating point.** The
  pc1 migration builds the HNSW index with `m=16, ef_construction=200`; the
  C1 semantic-cache operating point (threshold 0.18, ef_search 100) was
  measured on that build and does not hold for indexes built with the
  pgvector default of 64.
- **Back up the volume before any cutover or migration**, and verify the
  dump is readable (`pg_restore --list`) before touching the live volume.
