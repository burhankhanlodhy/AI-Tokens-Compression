#!/usr/bin/env bash
# AC-PC1/AC-PC2 acceptance gate for the explicitly pinned pgvector lane.
# The stock postgres:16 CI service remains a separate, untouched job.
set -Eeuo pipefail

: "${TOKEN_SAVER_PG_ADMIN_DSN:?TOKEN_SAVER_PG_ADMIN_DSN is required}"
: "${TOKEN_SAVER_PG_DSN:?TOKEN_SAVER_PG_DSN is required}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
MIGRATION="$REPO_ROOT/token-saver/migrations/20260918_pc1_pgvector.sql"
REAPPLY_LOG=$(mktemp "${TMPDIR:-/tmp}/pc1-reapply.XXXXXX.log")
trap 'rm -f "$REAPPLY_LOG"' EXIT

psql_admin=(psql "$TOKEN_SAVER_PG_ADMIN_DSN" -v ON_ERROR_STOP=1)
psql_app=(psql "$TOKEN_SAVER_PG_DSN" -v ON_ERROR_STOP=1)

"${psql_admin[@]}" -c 'CREATE DATABASE token_saver;'
"${psql_app[@]}" -f "$REPO_ROOT/postgres-schema-v2.sql" >/dev/null
"${psql_app[@]}" -f "$MIGRATION" >/dev/null

vector_version=$("${psql_app[@]}" -Atc "SELECT extversion FROM pg_extension WHERE extname='vector';")
[[ "$vector_version" == "0.8.6" ]] || {
  printf 'PC1 FAIL: vector extension version was %q, expected 0.8.6\n' "$vector_version" >&2
  exit 1
}

table_name=$("${psql_app[@]}" -Atc "SELECT to_regclass('public.semantic_cache_entries');")
[[ "$table_name" == 'semantic_cache_entries' ]] || {
  printf 'PC1 FAIL: semantic_cache_entries was not created\n' >&2
  exit 1
}

index_count=$("${psql_app[@]}" -Atc "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND tablename='semantic_cache_entries';")
[[ "$index_count" == '5' ]] || {
  printf 'PC1 FAIL: semantic cache index count was %s, expected 5\n' "$index_count" >&2
  exit 1
}

# The migration is deliberately fire-once. A second application must fail and
# its transaction must leave the first application fully intact.
set +e
"${psql_app[@]}" -f "$MIGRATION" >"$REAPPLY_LOG" 2>&1
reapply_status=$?
set -e
if (( reapply_status == 0 )); then
  printf 'PC1 FAIL: second migration application unexpectedly succeeded\n' >&2
  exit 1
fi
if ! grep -q 'relation "semantic_cache_entries" already exists' "$REAPPLY_LOG"; then
  printf 'PC1 FAIL: second application did not fail at the existing table\n' >&2
  cat "$REAPPLY_LOG" >&2
  exit 1
fi

post_fail_index_count=$("${psql_app[@]}" -Atc "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND tablename='semantic_cache_entries';")
[[ "$post_fail_index_count" == '5' ]] || {
  printf 'PC1 FAIL: failed reapplication changed the schema (indexes=%s)\n' "$post_fail_index_count" >&2
  exit 1
}

printf 'PC1/PC2 PASS: vector=%s, semantic_cache_entries present, indexes=%s, fire-once rollback verified\n' \
  "$vector_version" "$post_fail_index_count"
