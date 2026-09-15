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
from fastapi import FastAPI, Request, Response
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
    should_inject_conciseness,
)
from .dashboard import _render_stats_html
from .dashboard_v2 import render_shell
from .kpis import kpis_endpoint
from . import caching
from .providers.model import ProviderError
from .providers.anthropic import AnthropicAdapter
from .providers.registry import PREFIX_ROUTES, DEFAULT_REGISTRY, ProviderRegistry


# Providers whose wire shape differs from the client's OpenAI shape — only
# these need response/stream translation. OpenAI-compat providers pass through.
_RESHAPE_PROVIDERS = {"anthropic"}


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


PROXY_CONTROL_HEADERS = {
    "x-token-saver-conciseness",  # P1-1 benchmark A/B control — internal only
}


def _forward_headers(request: Request) -> dict[str, str]:
    """Pass through auth + content type; drop hop-by-hop and proxy-control
    headers (control headers must never leak upstream, C2)."""
    headers = {}
    for name, value in request.headers.items():
        if name.lower() not in HOP_BY_HOP and name.lower() not in PROXY_CONTROL_HEADERS:
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
    # URL join: base URLs may or may not end in /v1 (config), adapter paths
    # may or may not start with /v1 — join without ever doubling the segment.
    base = base_url.rstrip("/")
    path = wire.path.lstrip("/")
    if base.endswith("/v1") and path.startswith("v1/"):
        path = path[3:]
    elif base.endswith("/v1") and path == "v1":
        path = ""
    req = client.build_request(
        "POST", f"{base}/{path}".rstrip("/") if path else base,
        json=wire.json_body, headers=headers
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
        content_src = m.get("content")
        if isinstance(content_src, list):
            content = []
            for p in content_src:
                ptype = p.get("type", "text")
                if ptype == "image_url":  # OpenAI client shape -> normalized "image"
                    src = p.get("image_url") or {}
                    url = src.get("url", "")
                    if url.startswith("data:"):
                        # data URI: media_type + base64 payload
                        head, _, data = url.partition(",")
                        media = head.removeprefix("data:").partition(";")[0]
                        norm = ContentPart(type="image", source={"media_type": media, "data": data})
                    else:
                        norm = ContentPart(type="image", source={"url": url})
                elif ptype == "image":
                    norm = ContentPart(type="image", source=p.get("source"))
                else:
                    norm = ContentPart(type="text", text=p.get("text"))
                content.append(norm)
        else:
            content = content_src
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

    # --- Output-conciseness control (P1-1) ---
    # Precedence: per-request header (benchmark A/B arms) > config default.
    # The benchmark arms MUST be able to force baseline (off) vs treatment (on)
    # regardless of the deployment default; the header never leaks upstream.
    conciseness_on = s.output_conciseness_enabled
    hdr = request.headers.get("x-token-saver-conciseness")
    if hdr is not None:
        conciseness_on = hdr.strip().lower() in ("1", "true", "yes", "on")

    # --- Phase 4: task-aware routing ---
    route = classify(messages) if s.compression_enabled else "passthrough"

    in_before = count_messages(messages, model)
    compressed = False

    if route == "compress":
        # --- Phase 3: input compression ---
        new_messages = compress_messages(messages)
        # --- Phase 5: output-side conciseness ---
        # Category/length-aware gate (P1-1 evidence): inject only when the
        # user's actual request is long enough for the instruction to pay
        # for itself; short prompts are net-negative.
        if conciseness_on and should_inject_conciseness(new_messages):
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

    # --- C7: upstream transport failures surface as normalized errors ---
    try:
        resp, provider = await _forward_routed(request, model, payload, stream=True)
    except httpx.TimeoutException as exc:
        _log(model, route or "passthrough", in_before, in_after, 0,
             (time.perf_counter() - started) * 1000, compressed, 504,
             cache_status=cache_status)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": f"upstream timeout: {exc}",
                               "type": "upstream_timeout", "code": 504}},
        )
    except httpx.ConnectError as exc:
        _log(model, route or "passthrough", in_before, in_after, 0,
             (time.perf_counter() - started) * 1000, compressed, 502,
             cache_status=cache_status)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"upstream unreachable: {exc}",
                               "type": "upstream_unreachable", "code": 502}},
        )

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
        # Provider-routed streaming needs SSE translation to the client's
        # OpenAI shape (C4); OpenAI-compat/legacy streams pass through raw.
        needs_stream_translation = (
            s.provider_routing
            and (p := _provider_for_model(model)) in _RESHAPE_PROVIDERS
            and resp.status_code == 200
            and "text/event-stream" in resp.headers.get("content-type", "")
        )
        collected: list[str] = []

        async def raw_streamer():
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

        async def translated_streamer():
            """Anthropic SSE -> OpenAI chat.completion.chunk SSE (C4).

            Every upstream event is handled explicitly: content deltas become
            OpenAI delta chunks, usage events are preserved, [DONE] terminates,
            and malformed/unknown events are surfaced as comment lines rather
            than silently dropped. The client's original `model` string is
            echoed in every chunk.
            """
            anthropic = AnthropicAdapter()
            usage_acc: dict = {}
            done_sent = False
            openai_tool_index = 0  # OpenAI tool_calls index counter
            try:
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    ev = anthropic.translate_stream_chunk(line, None)
                    if ev.kind == "drop":
                        continue  # anthropic framing lines never reach the client
                    if ev.kind == "malformed":
                        # surfaced as an SSE comment so nothing is silently lost
                        yield (f": tokensaver: unparseable upstream event dropped\n\n").encode()
                        continue
                    if ev.kind == "tool_start":
                        # content_block_start with tool_use -> OpenAI tool_call header
                        chunk = {
                            "id": "chatcmpl-tokensaver",
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {
                                "tool_calls": [{"index": openai_tool_index,
                                                 "id": ev.delta_text or "",
                                                 "type": "function",
                                                 "function": {"name": ev.raw_line or "",
                                                              "arguments": ""}}]},
                                "finish_reason": None}],
                        }
                        openai_tool_index += 1
                        yield f"data: {json.dumps(chunk)}\n\n".encode()
                        continue
                    if ev.kind == "tool_delta" and ev.delta_text is not None:
                        # argument fragment -> OpenAI tool_calls arguments delta
                        chunk = {
                            "id": "chatcmpl-tokensaver",
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {
                                "tool_calls": [{"index": max(openai_tool_index - 1, 0),
                                                 "function": {"arguments": ev.delta_text}}]},
                                "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n".encode()
                        continue
                    if ev.kind == "delta" and ev.delta_text:
                        collected.append(ev.delta_text)
                        chunk = {
                            "id": "chatcmpl-tokensaver",
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0,
                                         "delta": {"content": ev.delta_text},
                                         "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n".encode()
                    elif ev.kind == "usage" and ev.usage:
                        # merge without zeroing: message_delta only carries
                        # output_tokens; message_start only input_tokens
                        if ev.usage.input_tokens:
                            usage_acc["prompt_tokens"] = ev.usage.input_tokens
                        if ev.usage.output_tokens:
                            usage_acc["completion_tokens"] = ev.usage.output_tokens
                        if ev.usage.cache_read_tokens:
                            usage_acc.setdefault("prompt_tokens_details", {})
                            usage_acc["prompt_tokens_details"]["cached_tokens"] = \
                                ev.usage.cache_read_tokens
                    elif ev.kind == "error" and ev.error:
                        # provider error events are surfaced, never dropped (C4/C8)
                        err_chunk = {
                            "id": "chatcmpl-tokensaver",
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {},
                                         "finish_reason": "stop"}],
                            "error": {"message": ev.error.message,
                                      "type": ev.error.kind, "code": None},
                        }
                        yield f"data: {json.dumps(err_chunk)}\n\n".encode()
                    elif ev.kind == "done" and not done_sent:
                        final = {"id": "chatcmpl-tokensaver",
                                 "object": "chat.completion.chunk",
                                 "model": model,
                                 "choices": [{"index": 0, "delta": {},
                                              "finish_reason": "stop"}]}
                        if usage_acc:
                            final["usage"] = usage_acc
                        yield f"data: {json.dumps(final)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        done_sent = True
                if not done_sent:
                    # upstream ended without message_stop: still terminate cleanly
                    yield b"data: [DONE]\n\n"
            finally:
                await resp.aclose()
                output_tokens = count_text("".join(collected), model)
                _log(model, route, in_before, in_after, output_tokens,
                     latency_ms, compressed, resp.status_code,
                     cache_status=cache_status)

        return StreamingResponse(
            translated_streamer() if needs_stream_translation else raw_streamer(),
            status_code=resp.status_code,
            headers=out_headers,
            media_type=resp.headers.get("content-type"),
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
        if provider in _RESHAPE_PROVIDERS and resp.status_code == 200:
            try:
                reshaped = _normalize_to_openai(json.loads(content), model)
            except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
                reshaped = content  # never break the relay on reshaping

    output_tokens = 0
    reshaped_obj: dict | list | None = None
    try:
        parsed = json.loads(reshaped) if isinstance(reshaped, (str, bytes)) else reshaped
        reshaped_obj = parsed
        output_tokens = count_output(parsed, model)
    except (json.JSONDecodeError, AttributeError):
        pass  # non-JSON body (e.g. HTML error page): relay raw, 0 tokens
    _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, resp.status_code,
         cache_status=cache_status)
    if reshaped_obj is not None:
        return JSONResponse(content=reshaped_obj, status_code=resp.status_code,
                            headers=out_headers)
    # non-JSON body: pass through raw with the provider's content-type
    media = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=reshaped or b"", status_code=resp.status_code,
                    headers=out_headers, media_type=media)


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
