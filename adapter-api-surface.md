# Provider Adapter Layer — API Surface Design (Phase A / PA-1)

Status: draft v0.1 by @application-developer. Feeds the QA contract matrix
(PA-1 gate) and the implementation that follows board-lock.

---

## 1. Design goals

1. **Adding a provider = a row, not a code change** — for OpenAI-compatible
   providers. Only *wire-shape* differences (Anthropic) need an adapter class.
2. **One internal request model.** The proxy's pipeline (classify → compress →
   inject → forward → relay) operates on a single normalized shape; adapters
   translate at the edges. Compression/counting logic stays provider-agnostic.
3. **BYOK preserved end-to-end.** Adapter receives the caller's credential
   material; the proxy never stores keys (Phase C managed keys aside).
4. **Every adapter satisfies the same contract** (Section 5) so QA can test
   providers against one matrix.

---

## 2. Internal request/response model (`proxy/providers/model.py`)

```python
@dataclass
class NormalizedRequest:
    model: str                 # canonical model id, e.g. "claude-sonnet-5"
    messages: list[Message]    # {role, content: str | list[ContentPart]}
    system: str | None         # extracted from messages (OpenAI shape) or param (Anthropic)
    tools: list[dict] | None   # normalized tool definitions
    stream: bool
    max_tokens: int | None
    temperature: float | None
    extra: dict                # provider-specific passthrough (reasoning, etc.)

@dataclass
class ContentPart:             # multimodal normalization
    type: str                  # "text" | "image" | "file"
    text: str | None
    source: dict | None        # normalized {media_type, data | url}

@dataclass
class NormalizedResponse:
    status: int
    content: bytes             # raw translated body (already provider-shaped)
    output_text: str           # extracted for token counting
    usage: Usage | None        # normalized token counts when provider reports them
    error: ProviderError | None

@dataclass
class ProviderError:
    kind: str                  # "auth" | "rate_limit" | "overloaded" | "invalid_request" | "upstream"
    message: str
    retry_after_s: float | None
    status: int
```

**Rule:** compression (`compress_messages`), counting (`count_messages` /
`count_text`), and conciseness injection operate on `NormalizedRequest` only.
No adapter ever sees compression logic; no pipeline stage ever sees a raw
provider payload.

---

## 3. Adapter interface (`proxy/providers/base.py`)

```python
class ProviderAdapter(Protocol):
    name: str                       # "anthropic", "openai", ...

    def matches(self, model: str) -> bool:
        """Does this adapter handle the given model string?"""

    async def translate_request(self, req: NormalizedRequest) -> AdapterRequest:
        """NormalizedRequest -> provider wire shape (path, headers, json body)."""

    async def translate_response(self, raw: httpx.Response, req: NormalizedRequest) -> NormalizedResponse:
        """Provider wire response -> NormalizedResponse (status, usage, errors)."""

    def translate_stream_chunk(self, chunk: bytes, req: NormalizedRequest) -> StreamEvent:
        """SSE line -> StreamEvent(delta_text | usage | done). Provider-agnostic consumers."""

    def normalize_model(self, model: str) -> str:
        """'anthropic/claude-sonnet-5' -> 'claude-sonnet-5' (provider-local id)."""

    def auth_headers(self, credential: str) -> dict[str, str]:
        """Where the BYOK credential goes for this provider."""
```

## 4. Routing & registry

- **`ProviderRegistry`** built at startup from the `providers` table
  (PA-0 schema) + config. Row = `{name, base_url, adapter_class, auth_style,
  enabled, pricing_json_url?}`.
- **Routing rule (PA-1):** `model` string prefix wins, then explicit
  `X-Token-Saver-Provider` header, then default provider in config:
  `"anthropic/claude-..."` → Anthropic, `"openai/gpt-..."` → OpenAI,
  `"xai/grok-..."` → xAI, bare ids fall back to default provider.
- **Fallbacks (P1, interface reserved now):** `route()` returns an ordered
  candidate list; Phase A uses only the first.

### Adapter classes for Phase A

| Class | Covers | Notes |
|---|---|---|
| `OpenAICompatAdapter` | OpenAI, OpenRouter, xAI, Google (OpenAI-compat endpoint), vLLM, Ollama | one class, registry rows differ by base_url/auth header style |
| `AnthropicAdapter` | Anthropic `/v1/messages` | system-as-param, `x-api-key`, different SSE event names, `cache_control` blocks (PA-4 hook) |

xAI/auth-style nuances (e.g. Google's `x-goog-api-key` on its compat endpoint)
are registry **auth_style** values: `bearer` | `x-api-key` | `api-key` | `query-param`.

---

## 5. Provider contract matrix (QA gate — per provider)

Every cell must pass for: **OpenAI, Anthropic, OpenRouter, xAI, Google,
vLLM, Ollama** (all six registry providers). The suite is parametrized by
adapter class: one `OpenAICompatAdapter` suite spanning OpenAI / OpenRouter /
xAI / Google / vLLM / Ollama (varying `auth_style` + `base_url`), plus a
separate `AnthropicAdapter` suite. Provider-specific quirks get dedicated
extra rows on top of the shared suite — notably **Ollama streaming**
(no standard `usage` chunk in some versions; `stream_options` unsupported)
and **Google compat** auth (`x-goog-api-key` header, no bearer).

| Contract | What is asserted |
|---|---|
| C1 Routing | model string routes to correct adapter; unknown prefix → default + warning logged |
| C2 Auth | credential lands in the provider-correct header/param; never logged or stored |
| C3 Request translation | normalized → wire shape byte-faithful (system, tools, max_tokens, temperature, multimodal parts) |
| C4 Streaming | SSE re-emitted in the *client's original* shape; deltas concatenate to full text; usage event captured |
| C5 Tool calls | tool definitions translate; streamed tool-call deltas reassemble; tool results round-trip |
| C6 Multimodal | image parts (base64 + URL) translate to provider shape; unsupported types → clean 400, not a crash |
| C7 Retries/timeouts | 429/5xx honored per `ProviderError.retry_after_s`; upstream timeout maps to 504 with normalized body |
| C8 Error normalization | every provider error maps to exactly one `ProviderError.kind`; client gets consistent error JSON |
| C9 Cost accounting | usage (input/output/cache-read/cache-write) recorded per request; NUMERIC precision survives |
| C10 Secret redaction | auth headers never appear in logs, stats DB, or error bodies |

## 6. Testing strategy

- **Contract tests** run each adapter against **recorded provider fixtures**
  (canned wire request/response pairs per C1–C10) — no network in CI.
- **Live smoke** (manual/scheduled): one real call per provider behind an env
  flag, asserting C1/C2/C9 end-to-end.
- **Golden-file diffing** for C3: normalized→wire translation snapshots
  reviewed once, then asserted byte-identical.

## 7. File layout

```
proxy/providers/
  __init__.py        # registry builder
  model.py           # NormalizedRequest/Response, ProviderError (Section 2)
  base.py            # ProviderAdapter protocol (Section 3)
  openai_compat.py   # OpenAICompatAdapter
  anthropic.py       # AnthropicAdapter
  registry.py        # routing: model string -> adapter
  fixtures/          # recorded wire payloads per provider (contract tests)
```

**Deliberately out of Phase A scope:** semantic caching, virtual keys/multi-tenant
auth (schema-ready only), semantic routing, guardrails — interfaces above leave
seams for them without committing code.
