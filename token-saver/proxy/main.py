"""Token-saver proxy: OpenAI-compatible drop-in proxy (BYOK).

Pipeline: receive request -> classify (compress vs passthrough) ->
optionally compress prompt + inject conciseness -> forward to upstream with
the client's own Authorization header -> log token counts/costs to SQLite.

The proxy never stores API keys.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import stats
from .classifier import classify
from .classifier import has_tool_calling_state
from .compression import compress_messages, has_compressible_content
from .config import estimate_cost, get_settings, load_pricing, reasoning_control_for
from .counting import (
    count_messages,
    count_output,
    count_text,
    extract_output_text_from_sse_chunk,
    inject_conciseness,
    should_inject_conciseness,
)
from .grounded import DOSE_TIERS, envelope_shape_present, select_dose_tier
from .grounded import grounded_answer_risk
from .dashboard import _render_stats_html
from .dashboard_v2 import render_shell
from .kpis import kpis_endpoint
from .tripwire import tripwire_endpoint
from . import caching
from . import semantic_cache
from .semantic_cache import DEFAULT_TENANT_ID, SemanticLookupKind, SemanticLookupScope
from .version import __version__
from .providers.model import ProviderError
from .providers.anthropic import AnthropicAdapter
from .providers.base import error_from_status
from .providers.registry import PREFIX_ROUTES, DEFAULT_REGISTRY, ProviderRegistry


# Response reshaping is adapter-CLASS-driven (C4, B-24): any AnthropicAdapter
# needs /v1/messages -> OpenAI translation, whatever name its registry row
# carries (AC-A1 config rows must not lose reshaping because their name is
# not the built-in "anthropic"). OpenAI-compat providers pass through.


class UnknownProviderError(ValueError):
    """AC-A2: the model string names a provider the registry cannot serve
    (unregistered slug, or a registered-but-disabled provider)."""

    def __init__(self, model: str):
        self.model = model
        super().__init__(
            f"unknown provider for model '{model}': no enabled provider "
            "matches it; prefix the model with a registered provider "
            "(e.g. 'openrouter/...') or correct the model string"
        )


def _provider_for_model(model: str) -> str | None:
    """Route a model string to a provider name via the shared registry.

    AC-A2: ProviderRegistry.route() is the single source of truth — this no
    longer re-walks PREFIX_ROUTES with different fallback semantics (which
    could disagree with the adapter actually used for dispatch). Returns
    None exactly when route() has no enabled provider for the model.
    """
    adapter = ProviderRegistry().route(model)
    return adapter.name if adapter is not None else None

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
    _ensure_admin_token(app)
    load_pricing()  # startup: pricing.json is THE price source (PM ruling)
    _ensure_cache_seed_rows()
    try:
        app.state.semantic_embedding_version = semantic_cache.derive_embedding_version()
        app.state.semantic_quality_version = semantic_cache.derive_quality_version()
    except ValueError:
        # Invalid semantic configuration is a clean disabled/miss state.  Do
        # not make an optional cache prevent the proxy from starting.
        app.state.semantic_embedding_version = None
        app.state.semantic_quality_version = None
    app.state.http = httpx.AsyncClient(
        base_url=s.upstream_base_url.rstrip("/"),
        timeout=s.upstream_timeout_seconds,
    )
    yield
    for client in [app.state.http, *getattr(app.state, "http_clients", {}).values()]:
        if client:
            await client.aclose()


APP_VERSION = __version__

app = FastAPI(title="token-saver proxy", version=APP_VERSION, lifespan=lifespan)


def _ensure_admin_token(application: FastAPI) -> str:
    """Resolve the C-2 write token once for this process/app lifetime.

    Operators may supply ``ADMIN_TOKEN``.  In the safer no-config default, a
    high-entropy replacement is intentionally visible exactly once at boot so
    the local dashboard can perform a write without persisting a credential.
    """
    existing = getattr(application.state, "admin_token", None)
    if existing:
        return str(existing)
    configured = (get_settings().admin_token or "").strip()
    token = configured or secrets.token_urlsafe(32)
    application.state.admin_token = token
    if not configured:
        logger.info("ADMIN_TOKEN=%s (generated at boot; not persisted)", token)
    return token


def _unauthorized() -> HTTPException:
    """Keep the proxy's compact JSON error convention for bearer failures."""
    return HTTPException(status_code=401, detail="Unauthorized")


def _require_admin(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise _unauthorized()
    candidate = auth[7:]
    expected = _ensure_admin_token(request.app)
    if not secrets.compare_digest(candidate, expected):
        raise _unauthorized()


def _keys_dsn() -> str:
    dsn = os.environ.get("TOKEN_SAVER_PG_DSN")
    if not dsn:
        raise HTTPException(status_code=503, detail="Postgres ledger is not configured.")
    return dsn


def _tenant_id(value: object) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="tenant_id must be a UUID.")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="tenant_id must be a UUID.") from exc


def _key_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="key id must be a UUID.") from exc


def _create_scopes(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(scope, str) for scope in value):
        raise HTTPException(status_code=400, detail="scopes must be an array of non-empty strings.")
    scopes = [scope.strip() for scope in value]
    if any(not scope for scope in scopes) or len(set(scopes)) != len(scopes):
        raise HTTPException(status_code=400, detail="scopes must be unique, non-empty strings.")
    return scopes


def _spend_cap(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        cap = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail="spend_cap_usd must be a non-negative number.") from exc
    if not cap.is_finite() or cap < 0:
        raise HTTPException(status_code=400, detail="spend_cap_usd must be a non-negative number.")
    return cap


def _tenant_exists(conn, tenant_id: str) -> bool:
    return conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)).fetchone() is not None


PROXY_CONTROL_HEADERS = {
    "x-token-saver-conciseness",  # P1-1 benchmark A/B control — internal only
    "x-token-saver-dose-pin",  # P6-3 benchmark-only tier pin — never upstream
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
    minify_tools: bool,
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
    # ``httpx(..., json=...)`` always applies its own compact encoder, which
    # would make TOOL_SCHEMA_COMPRESSION_ENABLED ineffective on routed
    # providers. Serialize the translated wire body ourselves so only the
    # validated tools value is compacted when the feature is enabled.
    headers.setdefault("content-type", "application/json")
    req = client.build_request(
        "POST", f"{base}/{path}".rstrip("/") if path else base,
        content=_serialize_request_payload(wire.json_body, minify_tools=minify_tools),
        headers=headers,
    )
    resp = await client.send(req, stream=stream)
    return resp


def _upstream_credential(request: Request) -> str:
    """BYOK: extract the caller's credential (Authorization bearer or x-api-key)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return request.headers.get("x-api-key", "")


async def acquire_embedding(
    request: Request, model: str, canonical_input: str
) -> list[float] | None:
    """Acquire a semantic vector through the configured upstream credential.

    Embedding acquisition is deliberately best-effort.  It is never allowed
    to turn an optimization failure into a client-visible proxy failure.
    """
    settings = get_settings()
    if settings.embedding_dimensions != 1536:
        return None
    provider = _provider_for_model(model)
    try:
        payload = json.dumps(
            {"model": settings.embedding_model, "input": canonical_input},
            ensure_ascii=False,
        ).encode("utf-8")
        if settings.provider_routing and provider:
            adapter = ProviderRegistry().route(model)
            if adapter is None:
                return None
            registry = ProviderRegistry()
            base_url = (
                settings.provider_base_urls.get(adapter.name)
                or registry.base_url_for(adapter.name)
                or settings.upstream_base_url
            )
            client = _get_http_for(request, base_url)
            headers = {
                **adapter.auth_headers(_upstream_credential(request)),
                **{
                    key: value
                    for key, value in _forward_headers(request).items()
                    if key.lower() not in {"authorization", "x-api-key"}
                },
            }
            base = base_url.rstrip("/")
            url = f"{base}/embeddings"
            req = client.build_request("POST", url, content=payload, headers=headers)
        else:
            client = _get_http(request)
            req = client.build_request(
                "POST", "/embeddings", content=payload,
                headers=_forward_headers(request),
            )
        response = await client.send(req, stream=True)
        content = await response.aread()
        await response.aclose()
        if response.status_code >= 400:
            return None
        data = json.loads(content)
        vector = data["data"][0]["embedding"]
        if not isinstance(vector, list) or len(vector) != settings.embedding_dimensions:
            return None
        # _vector_literal performs the finite-number validation used by SQL.
        if semantic_cache._vector_literal(vector, settings.embedding_dimensions) is None:
            return None
        return [float(value) for value in vector]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError,
            json.JSONDecodeError):
        return None


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
                          stream: bool, *, minify_tools: bool = False) -> tuple[httpx.Response, str]:
    """PA-1 live-path dispatch: adapter + provider base URL for a model.

    Returns (response, provider_name). Falls back to legacy single-upstream
    _forward when provider_routing is off. Raises UnknownProviderError when
    routing is on and the registry has no enabled provider for the model
    (AC-A2: a clear 4xx, never a silent reroute to the default provider).
    """
    s = get_settings()
    registry = ProviderRegistry()
    adapter = registry.route(model)
    provider = adapter.name if adapter is not None else None
    if not s.provider_routing:
        # Key off the TRANSPORT ACTUALLY USED: with routing off the bytes go
        # to the legacy single upstream (OpenRouter), regardless of the
        # prefix-derived adapter name. Returning that name here (e.g. "google"
        # for a gemini slug) made the 400-reasoning-retry guard treat a
        # legacy-upstream 400 as a non-legacy provider response and skip the
        # retry (SD-gate defect found by QA/PM at 309f924). Ledger callers
        # already coerced to "legacy" when routing is off — this is the same
        # fact, declared once at the source.
        resp = await _forward(request, payload, "chat/completions")
        return resp, "legacy"
    if adapter is None:
        raise UnknownProviderError(model)
    # B-24 (AC-A1 x AC-A2): base URL precedence — the documented settings
    # override for built-ins, then the row's own base_url for config-added
    # providers (never the default upstream host with the caller's key),
    # then the legacy upstream.
    base_url = (s.provider_base_urls.get(adapter.name)
                or registry.base_url_for(adapter.name)
                or s.upstream_base_url)
    resp = await _forward_via_adapter(request, model, payload, adapter,
                                      base_url, stream, minify_tools)
    return resp, adapter.name


def _normalize_messages(value: object) -> list[dict]:
    """Make malformed chat-message text safe for every pipeline stage.

    The proxy's detector, counters, classifier, L1 cleaner, and compressor
    all consume the same message list. Normalize client JSON once at the
    boundary so a null/missing text part or a non-object list member cannot
    become a later 500. Text-bearing parts without a ``type`` discriminator
    are normalized to ``type: text`` instead of silently disappearing.
    """
    if not isinstance(value, list):
        return []
    normalized: list[dict] = []
    for message in value:
        if not isinstance(message, dict):
            normalized.append({"role": "unknown", "content": ""})
            continue
        clean_message = dict(message)
        content = message.get("content")
        if isinstance(content, list):
            parts: list[object] = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(part)
                    continue
                clean_part = dict(part)
                if "text" in part or part.get("type") == "text":
                    clean_part["text"] = (
                        part.get("text") if isinstance(part.get("text"), str) else ""
                    )
                    if clean_part.get("type") is None:
                        clean_part["type"] = "text"
                parts.append(clean_part)
            clean_message["content"] = parts
        normalized.append(clean_message)
    return normalized


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
    messages = _normalize_messages(body.get("messages", []))
    # Downstream pipeline stages receive the normalized representation too;
    # otherwise a safe counter/classifier could still forward the original
    # malformed shape into a later consumer.
    body = {**body, "messages": messages}
    streaming = bool(body.get("stream"))

    # Ledger baseline: input_tokens_before = RAW original, counted before
    # any transform, so compression AND L1 deltas are both visible in the
    # in_before -> in_after accounting (B2/B3).
    in_before = count_messages(messages, model)

    # --- AC-P6f live observation channel: envelope-shape flag on the RAW
    # request content (pre-L1, pre-compression — the content as received).
    # Recorded on every ledger row independently of the tier decision so the
    # missed-grounding tripwire covers requests the discriminator never ran
    # on (conciseness off / passthrough route), not only classified ones.
    envelope_shape = 1 if envelope_shape_present(messages) else 0
    dose_tier_ctx: str | None = None
    grounded_risk_ctx: str | None = None

    # --- Phase 4: task-aware routing (computed on RAW bytes, pre-L1) ---
    # B2 P0: classification MUST see the original messages. L1's C1
    # whitespace compaction strips newlines, which destroys the
    # classifier._STRUCTURED signal (^\s*[{[] + >=3 newlines) and flips
    # JSON/RAG-shaped prompts from passthrough to compress — cleaning
    # requests the taxonomy protects and manufacturing compress routes.
    # Classification is computed before any transform.
    # Tool-calling is a hard correctness boundary for lossy compression. Keep
    # this decision on the raw envelope so normalization cannot hide a partial
    # tool-call marker. The L1 path below is more granular: it may clean only
    # eligible tool-result content while preserving every protocol envelope.
    tool_calling = has_tool_calling_state(body)
    classify_needed = s.compression_enabled or s.l1_enabled
    route = (
        "passthrough"
        if tool_calling
        else (classify(messages) if classify_needed else "passthrough")
    )
    tool_messages_before = messages

    # --- V1.2.1: deterministic codebase-context optimization ---
    # Run before L1, while deliberately retaining the raw tool-protocol hard
    # boundary. The codebase optimizer only transforms text-bearing content;
    # tool calls and results remain fully byte-preserved.
    if s.codebase_optimization_enabled and not tool_calling:
        from .codebase_optimizer import optimize_codebase_content

        optimized_messages = optimize_codebase_content(messages)
        if optimized_messages != messages:
            messages = optimized_messages
            body = {**body, "messages": optimized_messages}

    # --- B2: L1 lossless structural cleanup (runs BEFORE the PA-4 cache key) ---
    # Taxonomy §1 pipeline ordering: L1 clean first, then the cache key is
    # computed on the CLEAN body, so an L1-stripped request can share a cache
    # entry with an identical clean prompt. l1_tokens_stripped is the
    # tokenizer delta of the clean step only (never blended with cache or
    # compression savings; DBA B3 attributes it separately in the ledger).
    # Taxonomy v1.2 §5 (ruling A): L1 has its OWN eligibility gate
    # (l1_eligible, shared with the benchmark harness) — independent of the
    # lossy router. The route variable governs lossy compression only.
    from .l1_clean import clean_messages as _l1_clean_messages
    from .l1_clean import l1_eligible as _l1_eligible

    l1_tokens_stripped = 0
    l1_applied = False
    if s.l1_enabled and _l1_eligible(messages, route):
        l1_before = count_messages(messages, model)
        l1_messages = _l1_clean_messages(
            messages,
            tool_result_compression_enabled=s.tool_result_compression_enabled,
            tool_calling=tool_calling,
        )
        l1_after = count_messages(l1_messages, model)
        if l1_messages != messages:
            messages = l1_messages
            body = {**body, "messages": l1_messages}
            l1_applied = True
        l1_tokens_stripped = max(0, l1_before - l1_after)

    # Tool attribution is a subset of L1's message delta plus the lossless
    # schema-serialization delta. It is deliberately separate from the L1
    # total so dashboards can report savings from tool-heavy traffic without
    # adding it to l1_tokens_stripped a second time.
    from .tool_protocol import (
        is_tool_result_compressible,
        is_tool_schema_compressible,
    )

    def count_tool_result_content(tool_messages: list[dict]) -> int:
        total = 0
        for message in tool_messages:
            if not is_tool_result_compressible(message):
                continue
            content = message.get("content")
            if isinstance(content, str):
                total += count_text(content, model)
            elif isinstance(content, list):
                total += sum(
                    count_text(part["text"], model)
                    for part in content
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
        return total

    tool_result_before = count_tool_result_content(tool_messages_before)
    tool_result_after = count_tool_result_content(messages)
    tool_result_saved = (
        max(0, tool_result_before - tool_result_after)
        if s.tool_result_compression_enabled else 0
    )
    tools = body.get("tools")
    tool_schema_minified = (
        s.tool_schema_compression_enabled
        and is_tool_schema_compressible(tools)
    )
    tool_schema_saved = 0
    if tool_schema_minified:
        tool_schema_saved = max(
            0,
            count_text(json.dumps(tools), model)
            - count_text(json.dumps(tools, separators=(",", ":")), model),
        )
    tool_compression_saved = tool_result_saved + tool_schema_saved

    # --- PA-4: exact-prefix cache detection ---
    # Cache key = CLEAN body (AC-P1f / taxonomy §1). L1 runs above; the key
    # is stable regardless of compression settings because L1 is a pure
    # deterministic transform of the same input bytes.
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

    # --- v1.1 semantic cache (exact-first, non-streaming only) ---
    # The semantic key is the clean request, before lossy compression or
    # response-control fields are injected.  Exact hits are terminal for cache
    # attribution: semantic lookup must not run after an exact hit.
    semantic_scope: SemanticLookupScope | None = None
    semantic_embedding: list[float] | None = None
    semantic_prompt_hash: str | None = None
    semantic_lookup_result = None
    if (
        s.semantic_cache_enabled
        and not streaming
        and cache_status == "miss"
    ):
        try:
            embedding_version = getattr(
                request.app.state, "semantic_embedding_version", None
            ) or semantic_cache.derive_embedding_version()
            quality_version = getattr(
                request.app.state, "semantic_quality_version", None
            ) or semantic_cache.derive_quality_version()
            if s.provider_routing and provider is None:
                raise ValueError("semantic cache requires a routed provider")
            if s.semantic_cache_max_cosine_distance is None:
                raise ValueError("semantic cache threshold is not ratified")
            effective_provider = provider if s.provider_routing else "legacy"
            semantic_scope = SemanticLookupScope(
                tenant_id=DEFAULT_TENANT_ID,
                provider=effective_provider,
                model=str(model),
                embedding_model=s.embedding_model,
                embedding_dimensions=s.embedding_dimensions,
                embedding_version=embedding_version,
                quality_version=quality_version,
                request_parameters_hash=semantic_cache.request_parameters_hash(body),
            )
            semantic_prompt_hash = semantic_cache.canonical_prompt_hash(body)
            semantic_embedding = await acquire_embedding(
                request, str(model), semantic_cache.canonical_request(body)
            )
            if semantic_embedding is not None:
                semantic_lookup_result = semantic_cache.lookup_result(
                    semantic_scope,
                    semantic_embedding,
                    max_cosine_distance=s.semantic_cache_max_cosine_distance,
                )
                if semantic_lookup_result.kind == SemanticLookupKind.THRESHOLD_MISS:
                    cache_status = "semantic_threshold_miss"
                elif semantic_lookup_result.kind == SemanticLookupKind.HIT:
                    response_body = (
                        semantic_lookup_result.response.body
                        if semantic_lookup_result.response is not None
                        else None
                    )
                    if response_body is not None:
                        try:
                            cached_output = count_output(json.loads(response_body), str(model))
                        except (json.JSONDecodeError, TypeError, ValueError):
                            cached_output = 0
                        _log(
                            str(model), route, in_before, in_before, cached_output,
                            (time.perf_counter() - started) * 1000, False, 200,
                            cache_status="semantic_hit",
                            cache_savings=estimate_cost(str(model), in_before, cached_output),
                            provider=provider if s.provider_routing else "legacy",
                            embedding_version=embedding_version,
                            quality_version=quality_version,
                            envelope_shape=envelope_shape,
                        )
                        return Response(
                            content=response_body,
                            status_code=200,
                            media_type="application/json",
                        )
                    # Integrity failures are already cleaned lazily by the
                    # seam; treat them as a normal miss and go upstream.
                    semantic_lookup_result = semantic_cache.SemanticLookupResult.not_attempted()
                elif semantic_lookup_result.kind == SemanticLookupKind.NOT_ATTEMPTED:
                    # A DB/threshold/config failure is not an eligible lookup
                    # miss, so do not populate a cache from that request.
                    semantic_scope = None
                    semantic_embedding = None
                    semantic_prompt_hash = None
        except Exception:  # noqa: BLE001 — semantic failures are clean misses
            # Version/config/embedding/DB failures are clean misses.  The
            # original request still reaches the upstream below.
            semantic_scope = None
            semantic_embedding = None
            semantic_prompt_hash = None
            semantic_lookup_result = semantic_cache.SemanticLookupResult.not_attempted()

    # --- Output-conciseness control (P1-1) ---
    # Precedence: per-request header (benchmark A/B arms) > config default.
    # The benchmark arms MUST be able to force baseline (off) vs treatment (on)
    # regardless of the deployment default; the header never leaks upstream.
    conciseness_on = s.output_conciseness_enabled
    hdr = request.headers.get("x-token-saver-conciseness")
    if hdr is not None:
        conciseness_on = hdr.strip().lower() in ("1", "true", "yes", "on")

    compressed = False

    if route == "compress":
        # --- Phase 3: input compression ---
        new_messages = compress_messages(messages)
        # --- Phase 5: output-side conciseness (P6-2 dose tiers) ---
        # The P6-1 discriminator (shared module proxy.grounded) selects the
        # MAX tier per request; the request path NEVER injects above that
        # selection (AC-P6b). Pre-calibration (grounded_calibration_green
        # False) grounded fidelity-critical traffic caps at tier "none".
        # The P1-1 length/overhead gate still applies UNDER the tier — it
        # may only reduce injection further, never raise it.
        # Gate on the ORIGINAL messages, not the compressed ones (issue #1):
        # the short-question heuristic was calibrated on real user prompts,
        # and compression can shrink a long prompt below the 400-char cap
        # while keeping its trailing '?' — flipping the gate and silently
        # voiding the benchmark A/B arms. Gating pre-compression also keeps
        # the toggle deterministic regardless of compression outcome.
        if conciseness_on:
            tier = select_dose_tier(messages, s)
            # P6-3 tier pin (PM ratification, spec 25173d1): benchmark-only
            # calibration instrument. Honored ONLY behind allow_dose_pin —
            # with the flag off (production default) the pin is ignored and
            # the request still resolves to the discriminator's selection,
            # so no client can self-raise a tier. An invalid value falls
            # back to the discriminator too.
            pin_hdr = request.headers.get("x-token-saver-dose-pin")
            if pin_hdr is not None and s.allow_dose_pin:
                pin_val = pin_hdr.strip().lower()
                if pin_val in DOSE_TIERS:
                    tier = pin_val
            if tier != "none" and should_inject_conciseness(messages):
                new_messages = inject_conciseness(
                    new_messages, instruction=s.dose_tier_instructions()[tier]
                )
            # AC-P6f: record the RESOLVED tier (after pin) and the
            # discriminator's risk so the tripwire can scope drift to
            # grounded bounded traffic and catch unprotected envelope rows.
            dose_tier_ctx = tier
            grounded_risk_ctx = grounded_answer_risk(messages, s)["risk"]
        if new_messages != messages:
            compressed = any(
                a.get("content") != b.get("content")
                for a, b in zip(messages, new_messages)
            ) or len(new_messages) != len(messages)
            body = {**body, "messages": new_messages}
        route = "compress"  # route stays for stats

    # --- Reasoning-token cost control (PM v4 ruling) ---
    # Inject the model-family control (Gemini family: thinking_level MINIMAL
    # flooring; others: reasoning disabled). Only if the client didn't
    # already set their own control — never override an explicit client
    # choice — and skip models we already know reject a control.
    injected_reasoning = False
    injected_keys: set[str] = set()
    if (
        s.disable_reasoning_by_default
        and "reasoning" not in body
        and "thinking_level" not in body
        and model not in _reasoning_mandatory_models
    ):
        control = reasoning_control_for(model)
        body = {**body, **control}
        injected_keys = set(control)
        injected_reasoning = True

    # Reasoning-evidence header (P1-1 SD gate): the runner must be able to
    # RECORD whether the injected control was actually sent upstream or
    # rejected it (the proxy's silent 400-retry would otherwise be invisible
    # to clients, and the gate would reward the failure mode). The value
    # names what was injected so the artifact says what the control was:
    #   injected:<keys>              -> control was sent upstream
    #   rejected_retry_without_override -> upstream 400'd it; retried bare
    #   rejected_400_relayed         -> upstream 400'd it; not retryable
    reasoning_headers: dict[str, str] = {}
    if injected_reasoning:
        reasoning_headers["x-token-saver-reasoning"] = (
            "injected:" + ",".join(sorted(injected_keys))
        )

    in_after = count_messages(body.get("messages") or [], model)
    payload = _serialize_request_payload(body, minify_tools=tool_schema_minified)

    # --- C7: upstream transport failures surface as normalized errors ---
    # AC-A9: transport failures and relayed provider errors share ONE
    # normalizer (_normalized_error) so every client-facing failure has the
    # same envelope shape regardless of where it originated.
    try:
        resp, provider = await _forward_routed(
            request, model, payload, stream=True,
            minify_tools=tool_schema_minified,
        )
    except UnknownProviderError as exc:
        _log(model, route or "passthrough", in_before, in_after, 0,
             (time.perf_counter() - started) * 1000, compressed, 400,
             cache_status=cache_status,
             tool_compression_saved=tool_compression_saved,
             provider=provider if s.provider_routing else "legacy",
             dose_tier=dose_tier_ctx, grounded_risk=grounded_risk_ctx,
             envelope_shape=envelope_shape)
        return JSONResponse(
            status_code=400,
            content={"error": {"message": str(exc),
                               "type": "unknown_provider", "code": 400}},
        )
    except httpx.TimeoutException as exc:
        _log(model, route or "passthrough", in_before, in_after, 0,
             (time.perf_counter() - started) * 1000, compressed, 504,
             cache_status=cache_status,
             tool_compression_saved=tool_compression_saved,
             provider=provider if s.provider_routing else "legacy",
             dose_tier=dose_tier_ctx, grounded_risk=grounded_risk_ctx,
             envelope_shape=envelope_shape)
        return JSONResponse(
            status_code=504,
            content=_normalized_error(504, f"upstream timeout: {exc}",
                                      kind="upstream_timeout"),
        )
    except httpx.ConnectError as exc:
        _log(model, route or "passthrough", in_before, in_after, 0,
             (time.perf_counter() - started) * 1000, compressed, 502,
             cache_status=cache_status,
             tool_compression_saved=tool_compression_saved,
             provider=provider if s.provider_routing else "legacy",
             dose_tier=dose_tier_ctx, grounded_risk=grounded_risk_ctx,
             envelope_shape=envelope_shape)
        return JSONResponse(
            status_code=502,
            content=_normalized_error(502, f"upstream unreachable: {exc}",
                                      kind="upstream_unreachable"),
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
            # remember that, and retry once without our control instead of
            # failing every request to it.
            _reasoning_mandatory_models.add(model)
            reasoning_headers["x-token-saver-reasoning"] = (
                "rejected_retry_without_override"
            )
            retry_body = {k: v for k, v in body.items()
                          if k not in injected_keys}
            in_after = count_messages(retry_body.get("messages") or [], model)
            resp, provider = await _forward_routed(
                request,
                model,
                _serialize_request_payload(
                    retry_body, minify_tools=tool_schema_minified
                ),
                stream=True,
                minify_tools=tool_schema_minified,
            )
        else:
            # Not the mandatory-reasoning error: relay the 400 raw, but the
            # evidence header must never read "injected" on a failed request
            # — record the rejection explicitly.
            return JSONResponse(
                content=json.loads(err_content) if err_content else {},
                status_code=400,
                headers={"x-token-saver-reasoning": "rejected_400_relayed"},
            )

    return await _relay(
        resp, started, model=model, route=route, in_before=in_before,
        in_after=in_after, compressed=compressed, streaming=streaming,
        cache_status=cache_status, l1_tokens_stripped=l1_tokens_stripped,
        tool_compression_saved=tool_compression_saved,
        # B-24/AC-A12: attribute the ledger to the row the request actually
        # served under. Routing off = legacy single-upstream: attribute to the
        # seeded 'legacy' providers row, NOT the prefix table — the prefix
        # table describes the model's vendor, not who egressed the bytes
        # (benchmark trap: an anthropic/-prefixed model served by the legacy
        # OpenRouter upstream would otherwise be ledgered as provider=anthropic).
        provider=provider if s.provider_routing else "legacy",
        extra_headers=reasoning_headers,
        # AC-P6f: tripwire context rides every ledger row the request writes.
        dose_tier=dose_tier_ctx, grounded_risk=grounded_risk_ctx,
        envelope_shape=envelope_shape,
        semantic_scope=semantic_scope,
        semantic_embedding=semantic_embedding,
        semantic_prompt_hash=semantic_prompt_hash,
    )


def _serialize_request_payload(body: dict, *, minify_tools: bool) -> bytes:
    """Serialize only a validated tools array compactly, preserving all else."""
    if not minify_tools:
        return json.dumps(body).encode()
    serialized_fields = []
    for key, value in body.items():
        serialized_value = (
            json.dumps(value, separators=(",", ":"))
            if key == "tools" else json.dumps(value)
        )
        # The compacted tools member must also drop the space after the
        # member colon, or the setting saves nothing on the wire.
        separator = ":" if key == "tools" else ": "
        serialized_fields.append(f"{json.dumps(key)}{separator}{serialized_value}")
    return ("{" + ", ".join(serialized_fields) + "}").encode()


def _normalized_error(status: int, message: str, *,
                      kind: str | None = None) -> dict:
    """ONE shared client-facing error envelope (AC-A9).

    Both the transport-failure branches and the non-streaming relay build
    errors through here, so upstream timeouts, connect failures, and relayed
    provider error statuses all surface the same normalized shape
    {error: {message, type, code}} instead of raw provider bodies.
    `kind` overrides error_from_status for transport-level failures (504
    timeout / 502 unreachable), whose kinds are not status-derived.
    """
    err = error_from_status(status, message)
    return {"error": {"message": message, "type": kind or err.kind,
                      "code": status}}


def _error_message_from_body(content: bytes) -> str:
    """Best-effort provider error message out of an upstream error body."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return content.decode(errors="replace")
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
    return content.decode(errors="replace")


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
    l1_tokens_stripped: int = 0,
    tool_compression_saved: int = 0,
    provider: str | None = None,
    extra_headers: dict[str, str] | None = None,
    dose_tier: str | None = None,
    grounded_risk: str | None = None,
    envelope_shape: int | None = None,
    semantic_scope: SemanticLookupScope | None = None,
    semantic_embedding: list[float] | None = None,
    semantic_prompt_hash: str | None = None,
):
    """Stream or buffer the upstream response back, then log stats.

    `provider` is the adapter-resolved registry row name from dispatch
    (B-24/AC-A12): the ledger must attribute the row the request actually
    served under, not a prefix-table guess. None keeps the historical
    prefix-table derivation in the ledger layer.

    `extra_headers` are proxy-generated response headers (e.g. the
    x-token-saver-reasoning evidence header) merged over the relayed set.
    """
    s = get_settings()
    latency_ms = (time.perf_counter() - started) * 1000
    # B3 attribution: L1 savings are computed at the clean step and logged
    # as their own column. A cache-hit request reports ONLY cache savings —
    # L1 tokens and cache savings are never summed on a single request
    # (taxonomy §1; UI/UX stated rule; QA reconciliation contract).
    l1_savings = (
        estimate_cost(model, l1_tokens_stripped, 0)
        if cache_status not in {"exact_hit", "semantic_hit"} and l1_tokens_stripped > 0
        else 0.0
    )
    out_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"
    }
    if extra_headers:
        out_headers.update(extra_headers)

    if streaming:
        # Provider-routed streaming needs SSE translation to the client's
        # OpenAI shape (C4); OpenAI-compat/legacy streams pass through raw.
        needs_stream_translation = (
            s.provider_routing
            and isinstance(ProviderRegistry().route(model), AnthropicAdapter)
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
                     cache_status=cache_status,
                     l1_tokens_stripped=l1_tokens_stripped,
                     l1_savings=l1_savings,
                     tool_compression_saved=tool_compression_saved,
                     provider=provider,
                     dose_tier=dose_tier, grounded_risk=grounded_risk,
                     envelope_shape=envelope_shape)

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
                     cache_status=cache_status,
                     l1_tokens_stripped=l1_tokens_stripped,
                     l1_savings=l1_savings,
                     tool_compression_saved=tool_compression_saved,
                     provider=provider,
                     dose_tier=dose_tier, grounded_risk=grounded_risk,
                     envelope_shape=envelope_shape)

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
        if (isinstance(ProviderRegistry().route(model), AnthropicAdapter)
                and resp.status_code == 200):
            try:
                reshaped = _normalize_to_openai(json.loads(content), model)
            except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
                reshaped = content  # never break the relay on reshaping

    output_tokens = 0
    reshaped_obj: dict | list | None = None
    # --- AC-A9: relayed provider errors surface the shared envelope ---
    # Every JSON error status (401/403/429/5xx/...) from any provider goes
    # through _normalized_error — the same builder the transport-failure
    # branches use — instead of relaying the provider body verbatim.
    # Non-JSON error bodies (HTML error pages) still relay raw.
    if resp.status_code >= 400:
        try:
            json.loads(content)
            json_error = True
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            json_error = False
        if json_error:
            _log(model, route, in_before, in_after, 0, latency_ms,
                 compressed, resp.status_code, cache_status=cache_status,
                 l1_tokens_stripped=l1_tokens_stripped, l1_savings=l1_savings,
                 tool_compression_saved=tool_compression_saved,
                 provider=provider,
                 dose_tier=dose_tier, grounded_risk=grounded_risk,
                 envelope_shape=envelope_shape)
            return JSONResponse(
                content=_normalized_error(
                    resp.status_code, _error_message_from_body(content)),
                status_code=resp.status_code,
                headers=out_headers,
            )
    try:
        parsed = json.loads(reshaped) if isinstance(reshaped, (str, bytes)) else reshaped
        reshaped_obj = parsed
        output_tokens = count_output(parsed, model)
    except (json.JSONDecodeError, AttributeError):
        pass  # non-JSON body (e.g. HTML error page): relay raw, 0 tokens
    client_body: bytes | None = None
    rendered_response: JSONResponse | None = None
    if reshaped_obj is not None:
        # Render once: these are the exact bytes visible to the storing client
        # and the only bytes allowed into the semantic response store.  Parsing
        # and later rendering twice would let JSON formatting drift between a
        # miss response and its replay.
        rendered_response = JSONResponse(
            content=reshaped_obj,
            status_code=resp.status_code,
            headers={k: v for k, v in out_headers.items() if k.lower() != "content-length"},
        )
        client_body = bytes(rendered_response.body)
    if (
        resp.status_code == 200
        and semantic_scope is not None
        and semantic_embedding is not None
        and semantic_prompt_hash is not None
        and client_body is not None
        and len(client_body) <= s.semantic_cache_max_response_bytes
    ):
        # Store only successful non-streaming JSON responses.  The response
        # store enforces the size and integrity constraints atomically.
        try:
            semantic_cache.store_response(
                semantic_scope, semantic_prompt_hash, semantic_embedding, client_body
            )
        except Exception:  # noqa: BLE001 — cache persistence never breaks relay
            logger.exception("semantic response store failed; continuing")
    _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, resp.status_code,
         cache_status=cache_status,
         l1_tokens_stripped=l1_tokens_stripped,
         l1_savings=l1_savings,
         tool_compression_saved=tool_compression_saved,
         provider=provider,
         dose_tier=dose_tier, grounded_risk=grounded_risk,
         envelope_shape=envelope_shape)
    if rendered_response is not None:
        return rendered_response
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


# B-1b: telemetry must never break a proxied request, but silent drops are
# unacceptable — count every failed ledger write and expose it in /metrics.
LEDGER_WRITE_FAILURES = 0


def _log(model, route, in_before, in_after, output_tokens,
         latency_ms, compressed, status, cache_status="miss",
         cache_savings=0.0, l1_tokens_stripped=0, l1_savings=0.0,
         tool_compression_saved=0,
         provider=None, dose_tier=None, grounded_risk=None,
         envelope_shape=None, embedding_version=None, quality_version=None):
    global LEDGER_WRITE_FAILURES
    try:
        cost_before = estimate_cost(model, in_before, output_tokens)
        cost_after = estimate_cost(model, in_after, output_tokens)
        stats.log_request(
            model=model, route=route, input_tokens_before=in_before,
            input_tokens_after=in_after, output_tokens=output_tokens,
            est_cost_before=cost_before, est_cost_after=cost_after,
            latency_ms=latency_ms, compressed=compressed, status=status,
            cache_status=cache_status, cache_savings=cache_savings,
            l1_tokens_stripped=l1_tokens_stripped,
            l1_savings=l1_savings,
            tool_compression_saved=tool_compression_saved,
            provider=provider,
            dose_tier=dose_tier, grounded_risk=grounded_risk,
            envelope_shape=envelope_shape,
            embedding_version=embedding_version,
            quality_version=quality_version,
        )
        saved = in_before - in_after
        if saved > 0:
            logger.info("model=%s route=%s input %d->%d tokens (saved %d)",
                        model, route, in_before, in_after, saved)
    except Exception:  # noqa: BLE001 — logging must never break the proxy
        LEDGER_WRITE_FAILURES += 1
        logger.exception("Failed to log request stats")


# --- Simple passthroughs for other OpenAI-compatible endpoints ---

@app.get("/v1/models")
async def list_models(request: Request):
    started = time.perf_counter()
    client = _get_http(request)
    resp = await client.get("/models", headers=_forward_headers(request))
    _log("unknown", "passthrough", 0, 0, 0,
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


def _prom_label_escape(value: str) -> str:
    """Prometheus text-format label escaping (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.get("/metrics")
async def metrics(format: str = "text"):
    """Return Prometheus text-format metrics (or a JSON summary).

    K-4a (spec §41): /api/kpis is the single contract both the dashboard AND
    the Prometheus path consume — /metrics now reads the Postgres KPI path,
    never SQLite aggregate_stats(). Label mapping (deliberate):
      - the four headline counters map to overview.{requests,
        input_tokens_saved, cost_saved, avg_latency_ms};
      - requests_by_day is derived from `series` (bucket=day), preserving the
        old series name;
      - requests_by_model / requests_by_provider come from the KPI contract's
        by_model / by_provider;
      - requests_by_route is DROPPED: the KPI contract has no route series
        (route-level counts live in the ledger only).
    When the ledger is unavailable (no DSN configured, Postgres down) the
    scrape FAILS (503) instead of reporting zeros that are indistinguishable
    from silently dropped ledger writes. token_saver_ledger_write_failures
    stays a module-global counter (it must survive even a ledger outage).
    """
    try:
        resp = await kpis_endpoint(bucket="day")
    except RuntimeError as exc:  # no DSN configured (get_pg_dsn)
        return JSONResponse({"error": "ledger unavailable", "detail": str(exc)},
                            status_code=503)
    if resp.status_code != 200:
        # Postgres unreachable/errored: propagate the 503 — fail the scrape
        # loudly rather than serving zeros.
        return resp
    data = json.loads(resp.body)
    o = data["overview"]
    if format != "text":
        return {
            "requests": o["requests"],
            "tokens_saved": o["input_tokens_saved"],
            "cost_saved": round(o["cost_saved"], 4),
            "avg_latency_ms": round(o["avg_latency_ms"], 1),
            "ledger_write_failures": LEDGER_WRITE_FAILURES,
        }
    lines: list[str] = [
        "# HELP token_saver_requests_total total requests logged",
        "# TYPE token_saver_requests_total counter",
        f'token_saver_requests_total {o["requests"]}',
        "# HELP token_saver_tokens_saved tokens saved via compression",
        "# TYPE token_saver_tokens_saved counter",
        f'token_saver_tokens_saved {o["input_tokens_saved"]}',
        "# HELP token_saver_cost_saved estimated dollar cost saved",
        "# TYPE token_saver_cost_saved gauge",
        f'token_saver_cost_saved {o["cost_saved"]:.4f}',
        "# HELP token_saver_latency_ms average latency ms",
        "# TYPE token_saver_latency_ms gauge",
        f'token_saver_latency_ms {o["avg_latency_ms"]:.1f}',
        "# HELP token_saver_ledger_write_failures ledger writes that failed and were swallowed (telemetry must not break the proxy)",
        "# TYPE token_saver_ledger_write_failures counter",
        f"token_saver_ledger_write_failures {LEDGER_WRITE_FAILURES}",
    ]
    for m in data["by_model"]:
        lines.append(
            f'token_saver_requests_by_model{{model="{_prom_label_escape(m["model"])}"}}'
            f' {m["requests"]}'
        )
    for p in data["by_provider"]:
        lines.append(
            f'token_saver_requests_by_provider{{provider="{_prom_label_escape(p["provider"])}"}}'
            f' {p["requests"]}'
        )
    for s in data["series"]:
        lines.append(f'token_saver_requests_by_day{{day="{s["bucket"][:10]}"}} {s["requests"]}')
    return PlainTextResponse("\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/health")
async def health_ok():
    """Simple health check that identifies the deployed release."""
    return {"status": "ok", "version": app.version}


# --- PA-3: 4-tab dashboard (server-rendered shell + Chart.js) ---

@app.get("/dashboard")
async def dashboard():
    """Four-tab dashboard shell; data loaded client-side from /api/kpis only."""
    return HTMLResponse(render_shell())


@app.get("/static/dashboard.js")
async def dashboard_js():
    js_path = Path(__file__).resolve().parent / "static" / "dashboard.js"
    return PlainTextResponse(js_path.read_text(), media_type="application/javascript")


# --- C-2: proxy-facing key and tenant management ---------------------------

@app.get("/api/tenants")
async def api_tenants():
    """List tenant management facts; proxy key hashes never join this path."""
    import psycopg

    with psycopg.connect(_keys_dsn()) as conn:
        rows = conn.execute(
            """SELECT id::text, name, plan, spend_cap_usd, created_at
               FROM tenants ORDER BY created_at, id"""
        ).fetchall()
    return [
        {"id": row[0], "name": row[1], "plan": row[2], "spend_cap_usd": row[3], "created_at": row[4]}
        for row in rows
    ]


@app.get("/api/keys")
async def api_keys(tenant_id: str | None = None):
    """List redacted proxy-key facts for exactly one tenant."""
    import psycopg

    tenant = _tenant_id(tenant_id)
    with psycopg.connect(_keys_dsn()) as conn:
        if not _tenant_exists(conn, tenant):
            raise HTTPException(status_code=400, detail="Unknown tenant.")
        rows = conn.execute(
            """SELECT id::text, tenant_id::text, key_last4, scopes, spend_cap_usd,
                      status, created_at, revoked_at
               FROM api_keys WHERE tenant_id = %s ORDER BY created_at, id""",
            (tenant,),
        ).fetchall()
    return [
        {
            "id": row[0], "tenant_id": row[1], "key_last4": row[2], "scopes": row[3],
            "spend_cap_usd": row[4], "status": row[5], "created_at": row[6], "revoked_at": row[7],
        }
        for row in rows
    ]


def _new_proxy_key() -> tuple[str, str, str]:
    """Return plaintext, hash and display suffix without logging any of them."""
    plaintext = f"tsk_{secrets.token_urlsafe(32)}"
    return plaintext, hashlib.sha256(plaintext.encode("utf-8")).hexdigest(), plaintext[-4:]


@app.post("/api/keys", status_code=201)
async def create_api_key(request: Request, payload: dict):
    """Create one proxy-facing key and reveal its plaintext exactly once."""
    import psycopg

    _require_admin(request)
    tenant = _tenant_id(payload.get("tenant_id"))
    scopes = _create_scopes(payload.get("scopes"))
    cap = _spend_cap(payload.get("spend_cap_usd"))
    plaintext, key_hash, last4 = _new_proxy_key()
    with psycopg.connect(_keys_dsn()) as conn:
        if not _tenant_exists(conn, tenant):
            raise HTTPException(status_code=400, detail="Unknown tenant.")
        row = conn.execute(
            """INSERT INTO api_keys (tenant_id, key_hash, key_last4, scopes, spend_cap_usd)
               VALUES (%s, %s, %s, %s, %s) RETURNING id::text""",
            (tenant, key_hash, last4, scopes, cap),
        ).fetchone()
    return {"id": row[0], "key_last4": last4, "key": plaintext}


@app.post("/api/keys/{key_id}/rotate")
async def rotate_api_key(key_id: str, request: Request):
    """Atomically replace an active key and immediately retire its predecessor."""
    import psycopg

    _require_admin(request)
    key = _key_id(key_id)
    plaintext, key_hash, last4 = _new_proxy_key()
    with psycopg.connect(_keys_dsn()) as conn:
        with conn.transaction():
            old = conn.execute(
                """SELECT tenant_id::text, scopes, spend_cap_usd, status
                   FROM api_keys WHERE id = %s FOR UPDATE""", (key,)
            ).fetchone()
            if old is None:
                raise HTTPException(status_code=400, detail="Unknown key.")
            if old[3] != "active":
                raise HTTPException(status_code=400, detail="Key is not active.")
            conn.execute("UPDATE api_keys SET status = 'rotated' WHERE id = %s", (key,))
            new = conn.execute(
                """INSERT INTO api_keys (tenant_id, key_hash, key_last4, scopes, spend_cap_usd)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id::text""",
                (old[0], key_hash, last4, old[1], old[2]),
            ).fetchone()
    return {"id": new[0], "key_last4": last4, "key": plaintext, "previous_status": "rotated"}


@app.post("/api/keys/{key_id}/revoke")
async def revoke_api_key(key_id: str, request: Request):
    """Revoke a key idempotently, preserving its original revocation timestamp."""
    import psycopg

    _require_admin(request)
    key = _key_id(key_id)
    with psycopg.connect(_keys_dsn()) as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT status, revoked_at FROM api_keys WHERE id = %s FOR UPDATE", (key,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=400, detail="Unknown key.")
            if row[0] == "revoked":
                revoked_at = row[1]
            else:
                revoked_at = conn.execute(
                    """UPDATE api_keys SET status = 'revoked', revoked_at = now()
                       WHERE id = %s RETURNING revoked_at""", (key,)
                ).fetchone()[0]
    return {"id": key, "status": "revoked", "revoked_at": revoked_at}


@app.get("/api/kpis")
async def api_kpis(
    bucket: str = "day",
    # B-8: the documented contract (spec v2 §3) is ?from=&to= — the aliases
    # MUST be bound here on the real FastAPI route; the wrapper is the single
    # binding site for every /api/kpis param.
    from_: str | None = Query(None, alias="from"),
    to_: str | None = Query(None, alias="to"),
    from_ts: str | None = None,  # undocumented legacy names, back-compat
    to_ts: str | None = None,
    tenant_id: str | None = None,
    api_key_id: str | None = None,
):
    """PA-2: time-bucketed KPI aggregation over the Postgres ledger.

    Single source of truth for the dashboard and Prometheus path; all math
    is SQL-side over `requests` (AC-A5/A12 — no client-side aggregation).
    AC-A7: tenant_id/api_key_id scope every aggregate before it is computed
    (absent selectors = aggregate across all tenants).
    """
    return await kpis_endpoint(bucket=bucket, from_ts=from_ or from_ts,
                               to_ts=to_ or to_ts,
                               tenant_id=tenant_id, api_key_id=api_key_id)


@app.get("/api/tripwire")
async def api_tripwire(days: int = 7):
    """AC-P6f live tripwire loop: dose-drift + missed-grounding rules over
    the request ledger window. Status red = re-trigger AC-P6c."""
    return await tripwire_endpoint(days=days)


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
