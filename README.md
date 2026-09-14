# token-saver

`token-saver/` is the product: an OpenAI-compatible drop-in proxy that
compresses prompts before they hit the upstream LLM, injects a conciseness
instruction, suppresses hidden reasoning tokens by default, and tracks
token/cost savings in SQLite.

BYOK: the client's own `Authorization` header is forwarded per-request —
the proxy never stores API keys.

## Quickstart (Docker)

```bash
cd token-saver
cp .env.example .env            # review defaults; no keys needed (BYOK)
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
  -d '{"model":"z-ai/glm-5.3-flash","messages":[{"role":"user","content":"<long prompt>"}]}'
```

Note: `docker-compose.yml` publishes host port **8000**. If that port is
already taken on your machine, change the left side of the `ports:` mapping
in `docker-compose.yml` (e.g. `"8001:8000"`).

## Quickstart (local)

```bash
cd token-saver
python -m venv .venv && source .venv/bin/activate
pip install -r proxy/requirements.txt
cp .env.example .env
uvicorn proxy.main:app --port 8000
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | Main proxy path: classify → compress → forward (streaming supported) |
| `POST /v1/embeddings` | Passthrough to upstream `/embeddings` |
| `GET /v1/models` | Passthrough to upstream model list |
| `GET /stats[?format=text]` | Aggregate token/cost savings (JSON or text) |
| `GET /metrics` | Prometheus text format (`format=json` for a JSON summary) |
| `GET /health` | Liveness check |

### Reading metrics

- **Quick look:** `curl http://localhost:8000/stats` (JSON) or
  `curl http://localhost:8000/stats?format=text` (human-readable), or open
  `http://localhost:8000/stats?format=html` in a browser for the dashboard.
- **Prometheus/Grafana:** add `http://<proxy-host>:8000/metrics` as a scrape
  target. Exposed series:
  - `token_saver_requests_total` — total proxied requests
  - `token_saver_tokens_saved` — input tokens removed by compression
  - `token_saver_cost_saved` — estimated USD saved
  - `token_saver_latency_ms` — average upstream latency
  - `token_saver_requests_by_route{route=...}` / `token_saver_requests_by_day{day=...}` — breakdowns

## Configuration

All settings are env vars (see `.env.example` and `proxy/config.py`):

- `UPSTREAM_BASE_URL` — any OpenAI-compatible provider (default: OpenRouter)
- `COMPRESSION_ENABLED` / `OUTPUT_CONCISENESS_ENABLED` / `DISABLE_REASONING_BY_DEFAULT` — feature flags
- `LLMLINGUA_MODEL` / `COMPRESSION_RATE` — compression tuning
- `DATABASE_PATH` — SQLite location (default `<repo>/data/stats.db`; leave unset)

Cost estimates use the `model_prices_per_m` table in `proxy/config.py`
(USD per 1M tokens); unknown models fall back to `default_*_price_per_m`.

## Storage notes

Stats live in a single SQLite database. `init_db()` enables
`PRAGMA journal_mode=WAL`, so you will see `stats.db-wal` / `stats.db-shm`
sidecar files next to the database — do not copy the `.db` without them
while the proxy is running, and make sure any Docker volume/backups cover
the whole `data/` directory.

## Tests

```bash
.venv/bin/pytest test/test_proxy.py -q     # unit tests, mocked upstream
.venv/bin/python test/demo_client.py       # live demo through the proxy
.venv/bin/python test/compare.py           # direct vs proxied comparison
```

## Repo layout

- `token-saver/proxy/` — the FastAPI proxy application
- `token-saver/test/` — unit tests + demo/compare clients
- `ai-token-compression-proxy-plan.md` — original plan document
- `product-spec.md` — current product spec
