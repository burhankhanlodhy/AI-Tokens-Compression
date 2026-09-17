#!/usr/bin/env bash
# C-5 acceptance gate: clean-clone Docker smoke against the real Postgres ledger.
#
# This intentionally uses no upstream account.  The request carries a clearly
# invalid BYOK key and must receive 401 while still producing one Postgres row.
# Host prerequisites: bash, git, curl, and Docker Compose v2.
set -Eeuo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PROJECT_NAME="token-saver-c5-${$}"
PROXY_PORT="${C5_PROXY_PORT:-18000}"
PG_PORT="${C5_PG_PORT:-15433}"
WORKTREE=$(mktemp -d "${TMPDIR:-/tmp}/token-saver-c5.XXXXXX")
OVERRIDE="$WORKTREE/compose.c5.override.yml"
LOG_FILE="$WORKTREE/compose.log"

COMPOSE=(
  docker compose
  --project-directory "$WORKTREE/token-saver"
  --project-name "$PROJECT_NAME"
  --env-file "$WORKTREE/token-saver/.env"
  -f "$WORKTREE/token-saver/docker-compose.yml"
  -f "$OVERRIDE"
)

compose() { "${COMPOSE[@]}" "$@"; }

cleanup() {
  local status=$?
  if (( status != 0 )); then
    printf '\nC-5 BLOCKED/FAIL: compose logs\n' >&2
    compose logs --no-color >"$LOG_FILE" 2>&1 || true
    printf '%s\n' "--- $LOG_FILE ---" >&2
    # Do not print environment files or command lines: they contain the test password.
    printf '%s\n' "$(<"$LOG_FILE")" >&2
  fi
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORKTREE"
  exit "$status"
}
trap cleanup EXIT INT TERM

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'C-5 BLOCKED: required command not found: %s\n' "$1" >&2
    exit 2
  }
}

require_command git
require_command curl
require_command docker

docker compose version >/dev/null 2>&1 || {
  printf 'C-5 BLOCKED: Docker Compose v2 is unavailable\n' >&2
  exit 2
}

# Archive HEAD into a disposable directory so ignored local .env/data files and
# untracked edits cannot contaminate the advertised "fresh clone" acceptance.
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -xf - -C "$WORKTREE"

# Replace both published host ports, rather than appending a second mapping.
# This also exercises the README requirement that 8000 and 5433 can be remapped.
printf '%s\n' \
  'services:' \
  '  postgres:' \
  '    ports: !override' \
  "      - \"$PG_PORT:5432\"" \
  '  proxy:' \
  '    ports: !override' \
  "      - \"$PROXY_PORT:8000\"" > "$OVERRIDE"

# Before choosing a password, prove the unmodified example is fail-closed.  Run
# Compose with inherited credential variables removed so a developer's shell
# cannot accidentally make this negative assertion pass.
cp "$WORKTREE/token-saver/.env.example" "$WORKTREE/token-saver/.env"
set +e
GUARD_OUTPUT=$(env -u POSTGRES_PASSWORD -u TOKEN_SAVER_PG_DSN -u TOKEN_SAVER_PG_BASE \
  "${COMPOSE[@]}" config --quiet 2>&1)
GUARD_STATUS=$?
set -e
if (( GUARD_STATUS == 0 )); then
  printf 'C-5 FAIL: unmodified .env.example unexpectedly passed compose config\n' >&2
  exit 1
fi
if [[ "$GUARD_OUTPUT" != *'POSTGRES_PASSWORD must be set in .env'* ]]; then
  printf 'C-5 FAIL: compose guard error did not name POSTGRES_PASSWORD\n' >&2
  exit 1
fi
printf 'C-5: unmodified .env.example fails Compose guard as expected\n'

# Use a test-only password chosen here, never the example value.  The example
# must remain safe to copy; this is the explicit quickstart "choose your own
# password" step performed by the gate.
PG_PASSWORD="c5_${RANDOM}_${RANDOM}_password"
sed "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$PG_PASSWORD/" \
  "$WORKTREE/token-saver/.env.example" > "$WORKTREE/token-saver/.env"

# Validate the merged configuration before booting.  Compose redacts URL
# passwords, so this is only a syntax/guard check; raw credential auditing is a
# separate C-1 check and must inspect .env.example directly.
compose config --quiet
printf 'C-5: merged Compose configuration accepted (proxy=%s, postgres=%s)\n' \
  "$PROXY_PORT" "$PG_PORT"

compose up -d --build

ready=0
for _attempt in $(seq 1 120); do
  if curl -fsS --max-time 5 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if (( ! ready )); then
  printf 'C-5 FAIL: proxy did not become healthy within 240 seconds\n' >&2
  exit 1
fi
printf 'C-5: proxy health endpoint is ready\n'

# The request must reach the real configured upstream.  No provider account is
# needed: the deliberately invalid credential is expected to be rejected.
REQUEST_BODY='{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"C5 acceptance smoke: reply with one word."}]}'
RESPONSE_FILE="$WORKTREE/chat-response.json"
HTTP_CODE=$(curl -sS --connect-timeout 10 --max-time 90 \
  -o "$RESPONSE_FILE" -w '%{http_code}' \
  -X POST "http://127.0.0.1:$PROXY_PORT/v1/chat/completions" \
  -H 'Authorization: Bearer c5-invalid-key' \
  -H 'Content-Type: application/json' \
  --data "$REQUEST_BODY")
if [[ "$HTTP_CODE" != 401 ]]; then
  printf 'C-5 FAIL: invalid-key request returned HTTP %s (expected 401)\n' "$HTTP_CODE" >&2
  exit 1
fi
printf 'C-5: invalid-key request returned HTTP 401\n'

# Read the exact row back from Postgres.  This catches the silent-swallow path
# even if a dashboard or aggregate endpoint happens to report an empty state.
ROW=$(compose exec -T postgres psql -U postgres -d token_saver -Atqc \
  "SELECT route || '|' || status::text || '|' || cache_status FROM requests ORDER BY id DESC LIMIT 1;")
ROW=${ROW//$'\r'/}
if [[ "$ROW" != 'compress|401|miss' ]]; then
  printf 'C-5 FAIL: latest Postgres ledger row was %q (expected compress|401|miss)\n' "$ROW" >&2
  exit 1
fi
printf 'C-5: Postgres ledger row is route=compress, status=401, cache_status=miss\n'

json_field() {
  local json=$1
  local scope=$2
  local field=$3
  # Parse inside the proxy container, avoiding a host jq/venv dependency.  The
  # KPI assertions explicitly descend through .overview; they must not depend
  # on whichever key happens to appear first in the response document.
  compose exec -T proxy python -c \
    'import json, sys; value=json.loads(sys.argv[1]); value=value["overview"] if sys.argv[2] == "overview" else value; print(value[sys.argv[3]])' \
    "$json" "$scope" "$field" | tr -d '\\r'
}
assert_json_number() {
  local json=$1
  local scope=$2
  local field=$3
  local expected=$4
  local actual
  actual=$(json_field "$json" "$scope" "$field") || {
    printf 'C-5 FAIL: JSON field %s.%s was not found\n' "$scope" "$field" >&2
    exit 1
  }
  [[ "$actual" == "$expected" ]] || {
    printf 'C-5 FAIL: %s.%s=%s (expected %s)\n' "$scope" "$field" "$actual" "$expected" >&2
    exit 1
  }
  printf 'C-5: %s.%s=%s\n' "$scope" "$field" "$actual"
}

METRICS=$(curl -fsS --max-time 15 \
  "http://127.0.0.1:$PROXY_PORT/metrics?format=json")
assert_json_number "$METRICS" top ledger_write_failures 0

KPIS=$(curl -fsS --max-time 15 \
  "http://127.0.0.1:$PROXY_PORT/api/kpis?bucket=day")
assert_json_number "$KPIS" overview requests 1
assert_json_number "$KPIS" overview errors 1
assert_json_number "$KPIS" overview error_rate_pct 100.0

printf 'C-5 PASS: one real 401 request is persisted and Postgres KPIs reconcile; ledger_write_failures=0\n'
