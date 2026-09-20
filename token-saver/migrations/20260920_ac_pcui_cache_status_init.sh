#!/usr/bin/env bash
# Fresh-volume bridge for the existing-volume-only 20260920 migration.
# postgres-schema-v2.sql already contains the widened taxonomy, so initdb must
# not replay the strict fire-once migration on a fresh volume.  Legacy volumes
# still get the exact reviewed migration when their old constraint is present.
set -Eeuo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

migration=/opt/token-saver-migrations/20260920_ac_pcui_cache_status.sql
constraint_definition=$(psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --tuples-only --no-align \
  --command "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'requests'::regclass AND conname = 'chk_cache_status';")

case "$constraint_definition" in
  *semantic_threshold_miss*)
    printf '%s\n' '20260920 cache-status migration already represented by the canonical fresh schema; skipping strict replay.'
    ;;
  *"CHECK"*)
    printf '%s\n' '20260920 cache-status migration: applying legacy-volume upgrade.'
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --set ON_ERROR_STOP=1 --file "$migration"
    ;;
  *)
    printf '20260920 cache-status migration: chk_cache_status is missing or unreadable: %s\n' "$constraint_definition" >&2
    exit 1
    ;;
esac
