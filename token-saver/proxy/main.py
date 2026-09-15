"""Token-saver proxy: OpenAI-compatible drop-in proxy (BYOK).

Pipeline: receive request -> classify (compress vs passthrough) ->
optionally compress prompt + inject conciseness -> forward to upstream with
the client's own Authorization header -> log token counts/costs to SQLite.

The proxy never stores API keys.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import stats
from .classifier import classify
from .compression import compress_messages, has_compressible_content
from .config import estimate_cost, get_settings
from .counting import (
    count_messages,
    count_output,
    count_text,
    extract_output_text_from_sse_chunk,
    inject_conciseness,
)
from .dashboard import _render_stats_html
from .dashboard_v2 import render_shell
from .kpis import kpis_endpoint
from . import caching
from .providers.model import ProviderError
from .providers.registry import PREFIX_ROUTES, DEFAULT_REGISTRY, ProviderRegistry


def _provider_for_model(model: str) -> str | None:
    """Route a model string to a provider name via the registry prefixes."""
    lowered = model.lower()
    for prefix, provider in PREFIX_ROUTES.items():
        if lowered.startswith(prefix) and any(
            r.name == provider for r in DEFAULT_REGISTRY
        ):
            return provider
    return None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("token-saver")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Models that have told us they reject a disabled `reasoning` field (e.g.
# "Reasoning is mandatory for this endpoint and cannot be disabled"). Once a
# model lands here we stop injecting the override for it, so we don't pay a
# failed-request round trip on every subsequent call.
_reasoning_mandatory_models: set[str] = set()


def _ensure_cache_seed_rows() -> None:
    """Bootstrap default tenant + provider rows in Postgres so cache
    record/lookup FKs resolve. Idempotent; failures swallowed (cache must
    never break the proxy)."""
    import os as _os

    import psycopg

    if not _os.environ.get("TOKEN_SAVER_PG_DSN"):
        return
    try:
        with psycopg.connect(
            _os.environ["TOKEN_SAVER_PG_DSN"], connect_timeout=3
        ) as conn:
            conn.execute(
                "INSERT INTO tenants (id, name, plan) VALUES "
                "('00000000-0000-0000-0000-000000000000', 'default', 'self_host') "
                "ON CONFLICT (id) DO NOTHING"
            )
            for row in DEFAULT_REGISTRY:
                conn.execute(
                    "INSERT INTO providers (name, base_url, adapter_class, auth_style) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                    (row.name, row.base_url, row.adapter_class, row.auth_style),
                )
            conn.execute(
                "INSERT INTO providers (name, base_url, adapter_class, auth_style) "
                "VALUES ('legacy', 'https://openrouter.ai/api/v1', "
                "'OpenAICompatAdapter', 'bearer') ON CONFLICT (name) DO NOTHING"
            )
    except Exception:
        logger.exception("cache seed bootstrap failed; continuing")


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    stats.init_db()
    s = get_settings()
    _ensure_cache_seed_rows()
    app.state.http = httpx.AsyncClient(
        base_url=s.upstream_base_url.rstrip("/"),
        timeout=s.upstream_timeout_seconds,
    )
    yield
    for client in [app.state.http, *getattr(app.state, "http_clients", {}).values()]:
        if client:
            await client.aclose()


app = FastAPI(title="token-saver proxy", lifespan=lifespan)


def _forward_headers(request: Request) -> dict[str, str]:
    """Pass through auth + content type; drop hop-by-hop headers."""
    headers = {}
    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP:
            headers[name] = value
    return headers


def _get_http(request: Request) -> httpx.AsyncClient:
    """Return the shared upstream client, creating it lazily if needed
    (e.g. when the app is used without running its lifespan). Honors the
    same `_client_factory` test hook as `_get_http_for`."""
    client = getattr(request.app.state, "http", None)
    if client is None:
        s = get_settings()
        factory = globals().get("_client_factory")
        if factory:
            client = factory(s.upstream_base_url, s.upstream_timeout_seconds)
        else:
            client = httpx.AsyncClient(
                base_url=s.upstream_base_url.rstrip("/"),
                timeout=s.upstream_timeout_seconds,
            )
        request.app.state.http = client
    return client


def _get_http_for(request: Request, base_url: str) -> httpx.AsyncClient:
    """Per-provider upstream client, cached on app.state by base URL.

    A module-level `_client_factory` hook lets tests inject a transport
    (real calls use the default httpx.AsyncClient).
    """
    cache = getattr(request.app.state, "http_clients", None)
    if cache is None:
        cache = {}
        request.app.state.http_clients = cache
    client = cache.get(base_url)
    if client is None:
        s = get_settings()
        factory = globals().get("_client_factory")
        if factory:
            client = factory(base_url.rstrip("/"), s.upstream_timeout_seconds)
        else:
            client = httpx.AsyncClient(base_url=base_url.rstrip("/"),
                                       timeout=s.upstream_timeout_seconds)
        cache[base_url] = client
    return client


async def _forward_via_adapter(
    request: Request,
    model: str,
    payload: bytes,
    adapter: ProviderAdapter,
    base_url: str,
    stream: bool,
) -> httpx.Response:
    """PA-1 live path: translate the (already-processed) OpenAI-shaped body to
    the provider's wire shape, send it to that provider's base URL, and return
    the raw provider response (re-emitted to the client unchanged — the client
    spoke OpenAI shape, so responses are relayed as the provider returned them
    until response re-shaping lands with the contract-matrix suite)."""
    client = _get_http_for(request, base_url)
    wire = adapter.translate_request(_to_normalized(model, payload))
    fwd = {k: v for k, v in _forward_headers(request).items()
           if k.lower() not in ("authorization", "x-api-key")}
    headers = {**adapter.auth_headers(_upstream_credential(request)),
               **fwd, **wire.headers}
    req = client.build_request(
        "POST", wire.path, json=wire.json_body, headers=headers
    )
    resp = await client.send(req, stream=stream)
    return resp


def _upstream_credential(request: Request) -> str:
    """BYOK: extract the caller's credential (Authorization bearer or x-api-key)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return request.headers.get("x-api-key", "")


def _to_normalized(model: str, payload: bytes):
    """OpenAI-shaped request body -> NormalizedRequest for the adapters."""
    import json as _json

    from .providers.model import ContentPart, Message, NormalizedRequest

    body = _json.loads(payload)
    messages = []
    system = None
    for m in body.get("messages") or []:
        if m.get("role") == "system" and system is None:
            system = m.get("content")
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = [
                ContentPart(type=p.get("type", "text"),
                            text=p.get("text"),
                            source=(p.get("source") or p.get("image_url")
                                    and {"url": p["image_url"]["url"]}) or None)
                for p in content
            ]
        messages.append(Message(role=m.get("role", "user"), content=content,
                                tool_calls=m.get("tool_calls"),
                                tool_call_id=m.get("tool_call_id"),
                                name=m.get("name")))
    return NormalizedRequest(
        model=model,
        messages=messages,
        system=system,
        tools=body.get("tools"),
        stream=bool(body.get("stream")),
        max_tokens=body.get("max_tokens"),
        temperature=body.get("temperature"),
        extra={k: v for k, v in body.items()
               if k not in ("model", "messages", "tools", "stream",
                            "max_tokens", "temperature")},
    )


async def _forward(request: Request, body: bytes, path: str):
    """Forward raw bytes to upstream and return the streaming response."""
    url = f"/{path.lstrip('/')}"  # relative: client carries the upstream base URL
    client = _get_http(request)
    req = client.build_request(
        "POST", url, content=body, headers=_forward_headers(request)
    )
    resp = await client.send(req, stream=True)
    return resp


async def _forward_routed(request: Request, model: str, payload: bytes,
                          stream: bool) -> tuple[httpx.Response, str]:
    """PA-1 live-path dispatch: adapter + provider base URL for a model.

    Returns (response, provider_name). Falls back to legacy single-upstream
    _forward when provider_routing is off or the model maps to no provider.
    """
    s = get_settings()
    provider = _provider_for_model(model)
    if not s.provider_routing or not provider:
        resp = await _forward(request, payload, "chat/completions")
        return resp, (provider or "legacy")
    registry = ProviderRegistry()
    adapter = registry.route(model)
    base_url = s.provider_base_urls.get(provider, s.upstream_base_url)
    resp = await _forward_via_adapter(request, model, payload, adapter,
                                      base_url, stream)
    return resp, provider


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    started = time.perf_counter()
    s = get_settings()
    raw = await request.body()

    # --- Parse; on parse failure just forward untouched ---
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        resp = await _forward(request, raw, "chat/completions")
        return await _relay(resp, started, model="unknown", route="passthrough",
                            in_before=0, in_after=0, compressed=False)

    model = body.get("model", "unknown")
    messages = body.get("messages") or []
    streaming = bool(body.get("stream"))

    # --- PA-4: exact-prefix cache detection ---
    # Determined on the *original* (pre-compression) body so the cache key is
    # stable regardless of compression settings.
    provider = _provider_for_model(model)
    cache_status = "miss"
    try:
        if s.cache_enabled and s.provider_routing and provider:
            if caching.lookup(provider, model, body):
                cache_status = "exact_hit"
            else:
                caching.record(provider, model, body)
    except Exception:  # noqa: BLE001 — cache must never break the proxy path
        logger.exception("cache lookup failed; continuing with cache_status=miss")
        cache_status = "miss"

    # --- Phase 4: task-aware routing ---
    route = classify(messages) if s.compression_enabled else "passthrough"

    in_before = count_messages(messages, model)
    compressed = False

    if route == "compress":
        # --- Phase 3: input compression ---
        new_messages = compress_messages(messages)
        # --- Phase 5: output-side conciseness ---
        # Only worth the extra system-message tokens when there's actually
        # compressible content — otherwise it's pure input-token overhead.
        if s.output_conciseness_enabled and has_compressible_content(messages):
            new_messages = inject_conciseness(new_messages)
        if new_messages != messages:
            compressed = any(
                a.get("content") != b.get("content")
                for a, b in zip(messages, new_messages)
            ) or len(new_messages) != len(messages)
            body = {**body, "messages": new_messages}
        route = "compress"  # route stays for stats

    # --- Reasoning-token cost control ---
    # Only add this if the client didn't already specify their own
    # `reasoning` field — never override an explicit client choice — and
    # skip models we already know reject a disabled reasoning field.
    injected_reasoning = False
    if (
        s.disable_reasoning_by_default
        and "reasoning" not in body
        and model not in _reasoning_mandatory_models
    ):
        body = {**body, "reasoning": {"enabled": False}}
        injected_reasoning = True

    in_after = count_messages(body.get("messages") or [], model)
    payload = json.dumps(body).encode()

    resp, provider = await _forward_routed(request, model, payload, stream=True)

    if injected_reasoning and resp.status_code == 400 and provider in ("openrouter", "openai", "legacy"):
        err_content = await resp.aread()
        await resp.aclose()
        try:
            err_msg = json.loads(err_content).get("error", {}).get("message", "")
        except json.JSONDecodeError:
            err_msg = ""

        if "reasoning" in err_msg.lower() and "mandatory" in err_msg.lower():
            # This model requires reasoning and won't allow disabling it —
            # remember that, and retry once without our override instead of
            # failing every request to it.
            _reasoning_mandatory_models.add(model)
            retry_body = {k: v for k, v in body.items() if k != "reasoning"}
            in_after = count_messages(retry_body.get("messages") or [], model)
            resp = await _forward(
                request, json.dumps(retry_body).encode(), "chat/completions"
            )
        else:
            return JSONResponse(
                content=json.loads(err_content) if err_content else {},
                status_code=400,
            )

    return await _relay(
        resp, started, model=model, route=route, in_before=in_before,
        in_after=in_after, compressed=compressed, streaming=streaming,
        cache_status=cache_status,
    )


async def _relay(
    resp: httpx.Response,
    started: float,
    *,
    model: str,
    route: str,
    in_before: int,
    in_after: int,
    compressed: bool,
    streaming: bool = False,
    cache_status: str = "miss",
):
    """Stream or buffer the upstream response back, then log stats."""
    s = get_settings()
    latency_ms = (time.perf_counter() - started) * 1000
    out_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"
    }

    if streaming:
        collected: list[str] = []

        async def streamer():
            try:
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            collected.append(
                                extract_output_text_from_sse_chunk(json.loads(line[6:]))
                            )
                        except json.JSONDecodeError:
                            pass
                    yield (line + "\n").encode()
            finally:
                await resp.aclose()
                output_tokens = count_text("".join(collected), model)
                _log(model, route, in_before, in_after, output_tokens,
                     latency_ms, compressed, resp.status_code,
                     cache_status=cache_status)

        return StreamingResponse(
            streamer(), status_code=resp.status_code,
            headers=out_headers, media_type=resp.headers.get("content-type"),
        )

    content = await resp.aread()
    await resp.aclose()

    # --- Response normalization (C4/C8): provider shape -> client shape ---
    # The client always spoke OpenAI shape. Provider-routed responses must be
    # re-shaped before relay; OpenAI-compat providers pass through as-is.
    s2 = get_settings()
    reshaped = content
    if s2.provider_routing:
        provider = _provider_for_model(model)
        if provider and provider not in (None, "legacy") and resp.status_code == 200:
            try:
                reshaped = _normalize_to_openai(json.loads(content), model)
            except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
                reshaped = content  # never break the relay on reshaping

    output_tokens = 0
    try:
        output_tokens = count_output(reshaped if isinstance(reshaped, dict)
                                     else json.loads(reshaped), model)
    except (json.JSONDecodeError, AttributeError):
        pass
    _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, resp.status_code,
         cache_status=cache_status)
    return JSONResponse(
        content=json.loads(reshaped) if isinstance(reshaped, (str, bytes)) and reshaped else reshaped,
        status_code=resp.status_code,
        headers=out_headers,
    )


def _normalize_to_openai(data: dict, model: str) -> dict:
    """Anthropic /v1/messages response -> OpenAI chat.completion shape (C4)."""
    if "choices" in data:  # already OpenAI shape (or unknown) — pass through
        return data
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    u = data.get("usage") or {}
    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": int(u.get("input_tokens", 0)),
            "completion_tokens": int(u.get("output_tokens", 0)),
            # cache fields surface natively in OpenAI detail shape (AC-A6)
            "prompt_tokens_details": {"cached_tokens": int(u.get("cache_read_input_tokens", 0))},
        },
    }


def _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, status, cache_status="miss",
         cache_savings=0.0):
    try:
        cost_before = estimate_cost(model, in_before, output_tokens)
        cost_after = estimate_cost(model, in_after, output_tokens)
        stats.log_request(
            model=model, route=route, input_tokens_before=in_before,
            input_tokens_after=in_after, output_tokens=output_tokens,
            est_cost_before=cost_before, est_cost_after=cost_after,
            latency_ms=latency_ms, compressed=compressed, status=status,
            cache_status=cache_status, cache_savings=cache_savings,
        )
        saved = in_before - in_after
        if saved > 0:
            logger.info("model=%s route=%s input %d->%d tokens (saved %d)",
                        model, route, in_before, in_after, saved)
    except Exception:  # noqa: BLE001 — logging must never break the proxy
        logger.exception("Failed to log request stats")


# --- Simple passthroughs for other OpenAI-compatible endpoints ---

@app.get("/v1/models")
async def list_models(request: Request):
    started = time.perf_counter()
    client = _get_http(request)
    resp = await client.get("/models", headers=_forward_headers(request))
    _log("unknown", "models", 0, 0, 0,
         (time.perf_counter() - started) * 1000, False, resp.status_code)
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    raw = await request.body()
    resp = await _forward(request, raw, "embeddings")
    return await _relay(resp, time.perf_counter(), model="unknown",
                        route="passthrough", in_before=0, in_after=0,
                        compressed=False)


# Metrics & health endpoints for monitoring systems.


@app.get("/metrics")
async def metrics(format: str = "text"):
    """Return Prometheus text-format metrics (or a JSON summary)."""
    data = stats.aggregate_stats()
    t = data["totals"]
    if format != "text":
        return {
            "requests": t["requests"],
            "tokens_saved": t["input_tokens_saved"],
            "cost_saved": round(t["cost_saved"], 4),
            "avg_latency_ms": round(t["avg_latency_ms"], 1),
        }
    lines: list[str] = [
        "# HELP token_saver_requests_total total requests logged",
        "# TYPE token_saver_requests_total counter",
        f'token_saver_requests_total {t["requests"]}',
        "# HELP token_saver_tokens_saved tokens saved via compression",
        "# TYPE token_saver_tokens_saved counter",
        f'token_saver_tokens_saved {t["input_tokens_saved"]}',
        "# HELP token_saver_cost_saved estimated dollar cost saved",
        "# TYPE token_saver_cost_saved gauge",
        f'token_saver_cost_saved {t["cost_saved"]:.4f}',
        "# HELP token_saver_latency_ms average latency ms",
        "# TYPE token_saver_latency_ms gauge",
        f'token_saver_latency_ms {t["avg_latency_ms"]:.1f}',
    ]
    for r in data["by_route"]:
        lines.append(f'token_saver_requests_by_route{{route="{r["route"]}"}} {r["requests"]}')
    for d in data.get("by_day") or []:
        lines.append(f'token_saver_requests_by_day{{day="{d["day"]}"}} {d["requests"]}')
    return PlainTextResponse("\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/health")
async def health_ok():
    """Simple health check; return 200."""
    return {"status": "ok"}


# --- PA-3: 4-tab dashboard (server-rendered shell + Chart.js) ---

@app.get("/dashboard")
async def dashboard():
    """Four-tab dashboard shell; data loaded client-side from /api/kpis only."""
    return HTMLResponse(render_shell())


@app.get("/static/dashboard.js")
async def dashboard_js():
    js_path = Path(__file__).resolve().parent / "static" / "dashboard.js"
    return PlainTextResponse(js_path.read_text(), media_type="application/javascript")


@app.get("/api/kpis")
async def api_kpis(
    bucket: str = "day",
    from_ts: str | None = None,
    to_ts: str | None = None,
):
    """PA-2: time-bucketed KPI aggregation over the Postgres ledger.

    Single source of truth for the dashboard and Prometheus path; all math
    is SQL-side over `requests` (AC-A5/A12 — no client-side aggregation).
    """
    return await kpis_endpoint(bucket=bucket, from_ts=from_ts, to_ts=to_ts)


@app.get("/stats")
async def stats_endpoint(format: str = "json"):
    """Phase 8: aggregate token/cost savings from the SQLite log."""
    data = stats.aggregate_stats()
    if format == "text":
        t = data["totals"]
        return PlainTextResponse(
            f"Requests: {t['requests']}\n"
            f"Input tokens: {t['input_before']} -> {t['input_after']} "
            f"(saved {t['input_tokens_saved']}, {t['input_savings_pct']}%)\n"
            f"Output tokens: {t['output_tokens']}\n"
            f"Est. cost: ${t['cost_before']:.4f} -> ${t['cost_after']:.4f} "
            f"(saved ${t['cost_saved']:.4f})\n"
            f"Avg latency: {t['avg_latency_ms']} ms\n"
        )
    if format == "html":
        return HTMLResponse(_render_stats_html(data))
    return data
