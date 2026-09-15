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
from .kpis import kpis_endpoint

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    stats.init_db()
    s = get_settings()
    app.state.http = httpx.AsyncClient(
        base_url=s.upstream_base_url.rstrip("/"),
        timeout=s.upstream_timeout_seconds,
    )
    yield
    await app.state.http.aclose()


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
    (e.g. when the app is used without running its lifespan)."""
    client = getattr(request.app.state, "http", None)
    if client is None:
        s = get_settings()
        client = httpx.AsyncClient(
            base_url=s.upstream_base_url.rstrip("/"),
            timeout=s.upstream_timeout_seconds,
        )
        request.app.state.http = client
    return client


async def _forward(request: Request, body: bytes, path: str):
    """Forward raw bytes to upstream and return the streaming response."""
    url = f"/{path.lstrip('/')}"  # relative: client carries the upstream base URL
    client = _get_http(request)
    req = client.build_request(
        "POST", url, content=body, headers=_forward_headers(request)
    )
    resp = await client.send(req, stream=True)
    return resp


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

    resp = await _forward(request, payload, "chat/completions")

    if injected_reasoning and resp.status_code == 400:
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
                     latency_ms, compressed, resp.status_code)

        return StreamingResponse(
            streamer(), status_code=resp.status_code,
            headers=out_headers, media_type=resp.headers.get("content-type"),
        )

    content = await resp.aread()
    await resp.aclose()
    output_tokens = 0
    try:
        output_tokens = count_output(json.loads(content), model)
    except (json.JSONDecodeError, AttributeError):
        pass
    _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, resp.status_code)
    return JSONResponse(
        content=json.loads(content) if content else {},
        status_code=resp.status_code,
        headers=out_headers,
    )


def _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, status):
    try:
        cost_before = estimate_cost(model, in_before, output_tokens)
        cost_after = estimate_cost(model, in_after, output_tokens)
        stats.log_request(
            model=model, route=route, input_tokens_before=in_before,
            input_tokens_after=in_after, output_tokens=output_tokens,
            est_cost_before=cost_before, est_cost_after=cost_after,
            latency_ms=latency_ms, compressed=compressed, status=status,
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
