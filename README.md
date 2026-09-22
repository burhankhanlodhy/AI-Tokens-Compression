# token-saver

[![Tests](https://github.com/burhankhanlodhy/AI-Tokens-Compression/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/burhankhanlodhy/AI-Tokens-Compression/actions/workflows/ci.yml)

`token-saver/` is the product: an OpenAI-compatible drop-in proxy that
compresses prompts before they hit the upstream LLM, injects a conciseness
instruction, suppresses hidden reasoning tokens by default, and records
token/cost savings in a Postgres ledger (SQLite remains the local fallback —
see [Storage notes](#storage-notes)).

**What it saves:** the honest, headline claim is **lossless L1 structural
cleanup** — 29.9%–70.4% input-token reduction depending on payload shape
(~80% on RAG context, ~79% on duplicated system blocks, ~19% on log/trace,
~32% on JSON docs, 0% on prose, which is left untouched). Neither number is
a per-request floor. On top of that, an **exact-prefix cache** (on by
default) and an opt-in **pgvector semantic cache** (off by default, see
[Tuning](TUNING.md)) serve verbatim replayed responses for repeated prompts.

BYOK: the client's own `Authorization` header is forwarded per-request —
the proxy never stores API keys.

## Quickstart (Docker)

```bash
cd token-saver
cp .env.example .env            # no API keys needed (BYOK)
# REQUIRED: set your own Postgres password in .env (POSTGRES_PASSWORD=...).
# It cannot be left empty — Compose refuses to start until it is set.
docker compose up -d --build
curl http://localhost:8000/health   # -> {"status":"ok"}
```

Point any OpenAI-compatible client at the proxy. The only change most
clients need is the **base URL** — swap the provider URL for the proxy and
keep your existing API key:

```bash
# Before (direct to OpenRouter):
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"

# After (through token-saver — same key, same models):
export OPENAI_BASE_URL="http://localhost:8000/v1"
```

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"z-ai/glm-5.3-flash",
  "messages":[{"role":"user","content":"<long prompt>"}]}'
```

> **First build:** pulls the Python base image and downloads the
> LLMLingua-2 model weights, so expect **~10 minutes and ~9.4 GB of disk**
> (image layers + model cache volume). Later starts are fast.

Note: Compose publishes host ports **8000** (proxy) and **5433** (Postgres).
If either is already taken on your machine, change the left side of the
corresponding `ports:` mapping in `docker-compose.yml` (e.g. `"8001:8000"`
and/or `"5434:5432"`).

## Quickstart (local)

```bash
cd token-saver
python -m venv .venv && source .venv/bin/activate
pip install -r proxy/requirements.txt
cp .env.example .env             # local runs may leave POSTGRES_PASSWORD empty;
                                 # SQLite is used unless TOKEN_SAVER_PG_DSN is set
uvicorn proxy.main:app --port 8000
```

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/chat/completions` | Main proxy path: classify → compress → forward |
| `POST /v1/embeddings` | Passthrough to upstream `/embeddings` |
| `GET /v1/models` | Passthrough to upstream model list |
| `GET /stats[?format=text]` | Savings aggregates from the **SQLite** stats DB |
| `GET /dashboard` | Four-tab dashboard; data sourced from `/api/kpis` |
| `GET /api/kpis[?bucket=…&tenant_id=…]` | Time-bucketed ledger KPIs |
| `GET /api/tenants` | List tenants known to the ledger (Keys & Tenants tab) |
| `GET /api/keys` | List API keys; unauthenticated reads (see `ADMIN_TOKEN`) |
| `POST /api/keys`, `…/rotate`, `…/revoke` | Key writes; require `ADMIN_TOKEN` |
| `GET /api/tripwire` | Dose-drift / missed-grounding tripwire status |
| `GET /metrics` | Prometheus text format (`format=json` for a JSON summary) |
| `GET /health` | Liveness check |

### Reading metrics

- **Dashboard (recommended):** open `http://localhost:8000/dashboard` in a
  browser. It aggregates the **Postgres ledger** via `/api/kpis`, so it shows
  everything the proxy recorded — cache hits, L1 savings, per-route and
  per-day breakdowns — in four tabs.
- **JSON/text KPIs:** `curl "http://localhost:8000/api/kpis?bucket=day"` for
  the same Postgres-backed aggregates without a browser (`from=`/`to=` bound
  the window, `tenant_id=`/`api_key_id=` scope the aggregates).
- **Quick look (`/stats`):** `curl http://localhost:8000/stats` (JSON) or
  `?format=text` (human-readable) reads the **SQLite** stats DB only. When
  the Postgres ledger is configured (`TOKEN_SAVER_PG_DSN` set — always the
  case under Docker), requests are recorded in Postgres and `/stats` reports
  zeros; use `/dashboard` or `/api/kpis` for the real numbers.
- **Prometheus/Grafana:** add `http://<proxy-host>:8000/metrics` as a scrape
  target. Like `/api/kpis` (which it is bound to — same Postgres source of
  truth), it requires `TOKEN_SAVER_PG_DSN`; with the ledger unavailable the
  scrape **fails with 503** rather than reporting zeros, so a stalled target
  is visible in Prometheus instead of looking like zero traffic. Exposed
  series (label sets match the `/api/kpis` contract):
  - `token_saver_requests_total` — total proxied requests
  - `token_saver_tokens_saved` — input tokens removed by compression
  - `token_saver_cost_saved` — estimated USD saved
  - `token_saver_latency_ms` — average upstream latency
  - `token_saver_requests_by_model{model=...}` /
    `token_saver_requests_by_provider{provider=...}` /
    `token_saver_requests_by_day{day=...}` — breakdowns
  - `token_saver_ledger_write_failures` — ledger writes that failed and were
    swallowed (must stay 0; nonzero means telemetry is being lost)

## Configuration

All settings are env vars (see `.env.example` and `proxy/config.py`):

- `UPSTREAM_BASE_URL` — any OpenAI-compatible provider (default: OpenRouter)
- `COMPRESSION_ENABLED` / `OUTPUT_CONCISENESS_ENABLED` /
  `DISABLE_REASONING_BY_DEFAULT` — feature flags
- `L1_ENABLED` — lossless L1 structural cleanup, **on by default** (since
  B-26): whitespace-compacts JSON, removes duplicate/empty system blocks and
  dead RAG metadata. On the passthrough path, disabling L1 keeps the input
  byte-identical; the separate lossy compressor may rewrite eligible
  compress-route content. With L1 enabled, the checksum-pinned standalone
  CODE fixture remains byte-identical, while eligible JSON/RAG is deliberately
  cleaned and instead has the checksum-pinned
  structural round-trip guarantee:
  deterministic, idempotent raw-to-clean bytes with the cache keyed on clean
  bytes. The l1-taxonomy §6 contract tests pin these classes on the
  checksum-pinned corpus, whose 13 control fixtures cover code fences,
  user-authored YAML, `tool_calls` turns, multimodal parts arrays, markdown
  tables, mixed prose+JSON, and JSON inside a fence — classes beyond those
  are not individually pinned. One caveat: a message
  whose *entire* content is pretty-printed JSON with no surrounding prose
  gets whitespace-compacted — if you send "reformat this" as bare JSON, set
  `L1_ENABLED=false`.
- `TOOL_RESULT_COMPRESSION_ENABLED` — lossless cleanup of tool-result content
  (`role=tool` messages), **on by default** (v1.2.1): whitespace-compacts
  JSON tool results after validating them, preserving every value lexeme
  (numbers, escapes) byte-for-byte. The message envelope (`tool_call_id`,
  `name`) and assistant `tool_calls`/`tool_choice` are never rewritten. Set
  `false` to restore the pre-v1.2.1 conservative bypass.
- `TOOL_SCHEMA_COMPRESSION_ENABLED` — wire-level minification of the `tools`
  schema array, **on by default** (v1.2.1): re-serializes only the validated
  `tools` value with compact JSON separators on every provider path (legacy
  and routed). The decoded schema is semantically identical. Set `false` to
  send the schema with the standard spacing.
- `SEMANTIC_CACHE_ENABLED` — pgvector semantic lookup of semantically
  similar prompts, **off by default** (ratified GO as of v1.1 — the C1
  operating point below passed the calibration, tenant-isolation,
  deterministic-invalidation, and filtered-HNSW plan/latency gates — but
  enablement stays a deliberate deployment choice; no client header can
  enable it). Requires the pgvector Postgres image and migrations (see
  [Storage notes](#storage-notes)).
- `SEMANTIC_CACHE_MAX_COSINE_DISTANCE` — the similarity threshold, expressed
  as **cosine distance** (a hit must be at least this close). **Default:
  unset, which fails closed — every lookup is a clean miss.** The ratified
  production operating point is **0.18** (C1 ruling, PC5 recalibration:
  100% coverage, 95.83% recall, 0 false hits, p95 4.74 ms at 11,500-row
  traffic shape). Set `SEMANTIC_CACHE_MAX_COSINE_DISTANCE=0.18` when you
  enable the cache.
- `SEMANTIC_CACHE_HNSW_EF_SEARCH` — HNSW `ef_search` for semantic lookups
  (default **100**, the ratified value; keep ≤ 300 — ef=1000 failed the
  100 ms p95 latency bar at 10k+ rows). Applied per-lookup via
  `set_config('hnsw.ef_search', ...)`, never globally. Query-time knobs
  never invalidate cache entries (version-namespace policy in
  `proxy/semantic_cache.py`).
- `SEMANTIC_CACHE_TTL_SECONDS` — lifetime of a semantic entry **and** its
  paired response payload, which expire as one unit (default **300**,
  range 1–86400). Deliberately not request-configurable.
- `SEMANTIC_CACHE_MAX_RESPONSE_BYTES` — responses larger than this are
  clean semantic-cache misses and are never truncated (default **1048576**
  = 1 MiB); cache replay preserves the provider's bytes exactly.
- `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` — semantic-embedding namespace
  (defaults `text-embedding-3-small` @ **1536**; other dimensions are
  refused — embedding acquisition is best-effort and returns nothing rather
  than failing the request). Changing the model or dimensions bumps the
  `embedding_version` cache namespace and quarantines old entries until
  they expire; reverting restores them with zero migration.
- `ADMIN_TOKEN` — bearer token required by the dashboard key-management
  **write** endpoints (`POST /api/keys`, rotate, revoke). When blank or
  unset, a token is generated once at boot and printed once to the proxy's
  startup log (never persisted, never re-printed). Read endpoints stay
  unauthenticated under the self-host trust model.
- `LLMLINGUA_MODEL` / `COMPRESSION_RATE` — compression tuning
- `DATABASE_PATH` — SQLite location (default `<repo>/data/stats.db`; leave unset)
- `POSTGRES_PASSWORD` — **your own** Postgres credential; required before
  first start (Compose refuses to start while it is empty). The two DSN
  variables in `.env.example` interpolate `${POSTGRES_PASSWORD}`, so it is
  set in exactly one place.
- `TOKEN_SAVER_PG_DSN` — full Postgres ledger DSN. When set (Compose sets it
  in the proxy container automatically), every request is logged to the
  Postgres ledger and `/dashboard` + `/api/kpis` read it; when unset, the
  proxy falls back to SQLite for both logging and `/stats`.
- `TOKEN_SAVER_PG_BASE` — base DSN used only by the Postgres acceptance
  tests (`pytest` skips those tests when it is unavailable); not needed to
  run the proxy.
- `ALLOW_DOSE_PIN` — benchmark/calibration **only** (default `false`):
  when false, the `x-token-saver-dose-pin` control header is silently
  ignored, so no client can raise its own conciseness dose tier. Leave
  false in every standing deployment.
- `TOKEN_SAVER_MEASUREMENT_TAG` — benchmark-instance stamp: tags every
  ledger row the deployment writes and excludes tagged rows from the
  `/api/tripwire` live population. Leave unset in production (untagged =
  organic traffic).

Cost estimates use `pricing.json` (loaded at startup; USD per 1M tokens,
seeded from live OpenRouter rates); unknown models fall back to the
`default_*_price_per_m` values in `proxy/config.py`.

## Storage notes

The proxy has two storage layers with distinct roles:

**Postgres ledger** (`TOKEN_SAVER_PG_DSN` set — the Docker quickstart
configures this automatically): every request is recorded in the Postgres
`requests` ledger, including cache and L1 attribution columns, and
`/dashboard` + `/api/kpis` aggregate it SQL-side. Data lives in the
`postgres-data` Docker volume, initialized from
`postgres-schema-v2.sql`; back up the volume, and remap host port **5433**
if it is taken. On a fresh volume the schema is created at container init
and the provider seed rows are inserted at proxy startup.

**SQLite** (`data/stats.db`): remains the local fallback when
`TOKEN_SAVER_PG_DSN` is unset (single-user/local mode), and is what the
`/stats` endpoint reads. Mind the split: with Postgres configured, new
requests are written to the Postgres ledger, so `/stats` (SQLite) stays at
zeros — read the ledger through `/dashboard` or `/api/kpis`. `init_db()`
enables `PRAGMA journal_mode=WAL`, so you will see `stats.db-wal` /
`stats.db-shm` sidecar files next to the database — do not copy the `.db`
without them while the proxy is running, and make sure any Docker
volume/backups cover the whole `data/` directory.

### Existing Postgres volume upgrade (Phase C-1)

A fresh Compose volume gets the Phase A ledger from
`../postgres-schema-v2.sql`, the pgvector extension and semantic-cache entry
table from `migrations/20260918_pc1_pgvector.sql`, and the response store from
`migrations/20260919_pc2_semantic_responses.sql`. The canonical base schema
already contains the final four-value `chk_cache_status` taxonomy. Compose also
mounts the strict `20260920_ac_pcui_cache_status.sql` outside initdb and runs a
small initdb bridge: on a fresh volume it verifies that the canonical schema is
already widened and does not replay a fire-once migration; on a legacy volume,
apply the same SQL explicitly as described below.

For an existing `postgres-data` volume, take a backup, complete the libc-safe
cutover below, then apply the one-shot upgrades in order with
`psql -v ON_ERROR_STOP=1`. The full cutover runbook — including rollback
procedures and verification queries — lives in
[MIGRATIONS.md](MIGRATIONS.md); the short form:

```bash
psql "$TOKEN_SAVER_PG_DSN" -v ON_ERROR_STOP=1 \
  -f migrations/20260918_pc1_pgvector.sql
psql "$TOKEN_SAVER_PG_DSN" -v ON_ERROR_STOP=1 \
  -f migrations/20260919_pc2_semantic_responses.sql
psql "$TOKEN_SAVER_PG_DSN" -v ON_ERROR_STOP=1 \
  -f migrations/20260920_pc5_request_versions.sql
psql "$TOKEN_SAVER_PG_DSN" -v ON_ERROR_STOP=1 \
  -f migrations/20260920_ac_pcui_cache_status.sql
```

The first migration creates pgvector and `semantic_cache_entries`; the second
creates the tenant-scoped `semantic_cache_responses` table and its composite
foreign key; the third adds the semantic-hit version namespace columns; the
fourth replaces the pre-`b4baf47` `chk_cache_status` constraint. Do not skip
the fourth step on a volume created before `b4baf47`: the application
must be able to record all four literals, including `semantic_threshold_miss`.
These migrations are intentionally fire-once and fail loudly if their reviewed
preconditions are absent or they have already been applied.

Verify the upgrade and a fresh-volume initialization with the same query:

```bash
psql "$TOKEN_SAVER_PG_DSN" -Atc \\
  "SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conrelid = 'requests'::regclass AND conname = 'chk_cache_status';"
# Expected: a CHECK containing miss, exact_hit, semantic_hit,
# semantic_threshold_miss.
```

The fresh-volume bridge is not a replacement for the explicit legacy-volume
upgrade: initdb scripts run only when the data directory is empty. Back up the
volume before any cutover or migration, and retain the rollback copy until the
post-upgrade health/KPI checks pass.

### Existing-volume libc-safe cutover

Do **not** point the current `postgres:16-alpine` data directory directly at
the `pgvector/...-bookworm` image. A `REINDEX` is not the cutover: it does not
make a cross-libc data-directory transition safe. Use this sequence instead:

1. Stop proxy writes and take a custom-format dump to an absolute host path:
   `docker compose stop proxy` followed by
   `docker compose exec -T postgres pg_dump -U postgres -d token_saver
   --format=custom > /absolute/path/token_saver_pre_pc1.dump`.
2. Create a **new** Docker volume and a temporary pgvector/16 container using
   that volume; do not reuse `token-saver_postgres-data`. Restore the dump with
   `pg_restore --no-owner --exit-on-error`, then apply
   `migrations/20260918_pc1_pgvector.sql` using `psql -v ON_ERROR_STOP=1`.
3. Verify old-versus-new `COUNT(*)` and independent `SUM()` values for
   `requests.input_tokens_before`, `input_tokens_after`, `est_cost_before`,
   and `est_cost_after`; also verify `pg_extension` contains `vector`, the
   ledger indexes exist, and the semantic HNSW index/table exist.
4. Run `/health` and `/api/kpis` against the restored database, then switch
   the Compose volume mapping to the new volume and restart the proxy. Keep
   the old volume and dump until post-cutover checks pass; never delete the
   rollback copy as part of the restart.

Semantic caching is ratified for production use (C1 operating point,
v1.1) but ships **disabled by default** — enablement is a deployment
decision made by the operator; the deployment flag cannot be enabled by a
client request. See [TUNING.md](TUNING.md) before enabling.

## Tests

```bash
.venv/bin/pytest test/test_proxy.py -q     # unit tests, mocked upstream
.venv/bin/python test/demo_client.py       # live demo through the proxy
.venv/bin/python test/compare.py           # direct vs proxied comparison
```

## License

Released under the [MIT License](LICENSE): free to use, modify, and
redistribute, including for commercial purposes. Report security issues per
[SECURITY.md](SECURITY.md) — please use the private advisory flow, not a
public issue.

## Repo layout

- `token-saver/proxy/` — the FastAPI proxy application
- `token-saver/test/` — unit tests + demo/compare clients
- `token-saver/migrations/` — one-shot Postgres migrations (see [MIGRATIONS.md](MIGRATIONS.md))
- `MIGRATIONS.md` — pgvector cutover runbook for existing deployments
- `TUNING.md` — semantic-cache and L1 performance tuning guide
- `ai-token-compression-proxy-plan.md` — original plan document
- `product-spec-v2.md` — current product spec (supersedes `product-spec.md`)
