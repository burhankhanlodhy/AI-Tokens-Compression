# B1 — L1 Lossless Structural-Cleanup: Frozen Strip Taxonomy

Author: @product-manager · Phase: **B** · Gates: B2 (Dev), B3 (DBA), B4 (QA), B5 (UI/UX data contract)
Source of truth: `product-spec-v2.md` **AC-P1e / AC-P1f** (§88–92). This file **freezes** what "lossless L1 structural cleanup" means such that B2 can be implemented without re-litigating scope, B3 can attribute savings, and B4 can test round-trip equivalence against a pinned contract.

---

## 1. Pipeline ordering (the one rule that makes attribution and caching consistent)

The proxy pipeline for a cache-enabled, compressible request **must** be:

```
raw request body
  └─> L1 clean (pure deterministic transform, §4)
        └─> compute PA-4 cache key on the CLEAN body   <- NOT on the original body
              ├─ hit  -> serve cached completion; attribute ONLY cache savings
              └─ miss -> send clean body upstream;   attribute L1 savings
```

Consequences (frozen):
- **Cache key = clean bytes** (AC-P1f). Today `main.py:327-328` keys the cache on the *pre-compression original* body — **this must be reordered** so L1 runs first and the key is computed on the cleaned body, otherwise an L1-stripped request can never share a cache entry with an identical clean prompt.
- A request that is L1-stripped **and** cache-hit reports **only cache savings** for that call — the two are never summed on one request (UI/UX B3 note states this visibly; DB B3 must reconcile to it).
- `raw -> clean` must be a reproducible pure function (same bytes in ⟹ byte-identical clean bytes out) so a cache hit can serve an identical clean prompt.
- L1 is **off** for `passthrough` requests and never touches tool definitions, image parts, or the answer path.

## 2. Success bar (from AC-P1e — not renegotiated here)

- **≥15% input-token reduction** on RAG/JSON-heavy categories.
- **Byte-identical answer semantics** via round-trip equivalence (no judge).
- **Deterministic**: stable cache prefix; the same input cleans to the same bytes every time.

## 3. What "lossless" means (the standing rule)

A strip is permitted only if the model receives **semantically identical information**. A field is content only if it can influence the answer. Whitespace, formatting, bookkeeping, and retrieval/LLM plumbing metadata never influence an answer and are therefore strippable. Any human-authored instruction, user question, tool definition, or answer text is **never** stripped.

**v1.1 conservation principle (PM audit ruling):** conservation is the default for any field a question could plausibly ask about — provenance, identifiers, dates/timestamps, and metadata wrappers. Because the proxy cannot tell from a request which attributes a downstream question will reference, C3 may strip **only** fields that can *never* be quoted or attributed in an answer (scoring math, embeddings, internal byte-locators, pure harness plumbing — §4). Everything else that names, locates, or dates a passage is conserved via the §5 negative list.

## 4. Permit list — exactly what L1 MAY strip

Stripping applies to JSON documents and structured metadata **embedded in string content blocks**. A content block is eligible only if its trimmed form parses as JSON (object or array), or it is a JSON-lines/JSON-array continuation within a block. Non-JSON text is untouched.

**C1 — JSON whitespace/formatting normalization.**
Compact eligible JSON blocks: emit with `separators=(',', ':')`, two-space intent removed, no trailing whitespace, keys **preserved in original order** (`sort_keys=False`), values byte-for-byte unchanged. Empty objects/arrays stay as `{}`/`[]`. This is pure re-serialization — no data removed.

**C2 — Duplicate / empty system blocks.**
- Remove any `system` message whose content is empty or whitespace-only (after trim).
- Remove a `system` message that is **byte-identical** to the immediately preceding `system` message (keep the first occurrence). Not-adjacent duplicates stay (dedup only runs, deterministic, across the contiguous system-prefix run).
- Never merge, reorder, or truncate system content text.

**C3 — Dead retrieval/RAG metadata (embedded JSON RAG payloads).**
Within an eligible JSON object, **drop by exact field name** any of these (present anywhere in the object graph). *(v1.1: this is now a **conserved-only** list — fields in §5 are off-limits even inside eligible objects.)*

| Field name(s) | Rationale |
|---|---|
| `score`, `relevance`, `relevance_score`, `similarity`, `distance`, `cosine`, `rerank_score` | scoring math — never quoted in an answer |
| `embedding`, `vector`, `vector_score`, and any float-array field with >32 numeric elements | not human-readable; never answer content |
| `char_start`, `char_end`, `token_start`, `length`, `offset` | internal byte/char locators that never surface in an answer |
| `retrieval`, `pagination`, `total`, `limit`, `next_page`, `has_more`, `request_id`, `session_id`, `generation`, `query_id`, `trace`, `span_id`, `latency_ms`, `ts`, `level` | harness / query-log plumbing |
| Empty strings, empty arrays, empty objects anywhere in an eligible JSON object | no signal |

`content` / `text` / `answer` / `passage` / `doc` fields whose value is the actual answer-bearing text are **reserved — never stripped**, even under a `metadata`-like parent. Under v1.1 the §5 negative list (provenance, identifiers, timestamps, wrappers, `query`) is equally reserved and is stripped by nothing.

**Eligible-structure rule for C3:** apply C3 only within objects that are RAG/retrieval-shaped — an object containing at least one `content`/`text`/`passage` key **sibling to** the dead metadata. A bare config/document with `metadata` and no sibling content is **not** an RAG payload; leave it intact (conservative).

## 5. Negative list — L1 MUST NOT strip (frozen)

- Any `role` field, `role:user` question text, `role:assistant` reply, or `role:tool` output that is not an eligible JSON block.
- `system`/`user` instruction prose, any sentence content — even if it "looks" like metadata.
- **Provenance / citation fields (v1.1):** `source`, `title`, `url`, `path`, `filename`, `bucket`, `collection`, `index_name`, `page`, `page_number`, `chunk_id`, `chunk_index`, `block_id`, `doc_id`, `passage_id`, `source_id`, `id`, and any object-valued wrapper capable of carrying them (`metadata`, `attributes`, `tags`, `custom`, `raw`). A question may ask the model to attribute or cite these; the proxy cannot predict which — so they are **conserved even inside a RAG-shaped object**, never stripped. (This reverses v1.0, which segregated these into dead C3.)
- **Timestamps (v1.1):** `timestamp`, `created_at`, `updated_at`, `modified_at`, `expires_at`, `retrieved_at` — a question may ask "which is newest / when was it retrieved", so dates are conserved.
- The user's literal question wherever it appears — including a `query` field inside an eligible RAG JSON object.
- Tool definitions (`tools`), function schemas, `function_call`, response formats, `max_tokens`, `stream`, `temperature`, model headers.
- Image parts (`type:image`), any base64/data-URI payload.
- Key *order* changes to user-visible JSON configs (this could alter model-followed semantics in adversarial cases); order is preserved at all times.
- Any field inside a non-eligible (non-RAG-shaped) JSON document, even if its name appears in the C3 table. **C3 is shape-gated, not name-only.**
- Truncation, summarization, dedup of *non-adjacent* system blocks, removal of the *last* system message.
- Anything under a `passthrough` route.

## 6. Determinism contract (for B2/B4)

- Output depends only on input bytes. No clocks, RNG, concurrency, or config flags read inside the clean path.
- Field drop (C3) is by exact-name match within a shape-gated object; key order preserved; C1 compact is canonical.
- `clean(clean(x)) == clean(x)` (idempotent). `clean(x) == y` reproducible across runs and processes.
- Round-trip equivalence definition for B4: semantic `answer(clean(p)) == answer(p)` is replaced by a **structural equivalence** check (no judge):
  1. Every reserved answer-bearing value is preserved **byte-for-byte**: `role` text, `content`/`text`/`passage`/`answer`/`doc` values, prose, and any `query`/user-question field.
  2. **Every field on the v1.1 §5 negative list that is present in the input is preserved byte-for-byte in the output** — provenance names, identifiers, dates/timestamps, metadata/tag wrappers, tool defs, image parts. *(v1.1 fix: the §6 contract must assert provenance survival, not merely answer-content survival; v1.0's check could not detect a provenance strip and this was a real gap — see `rag-005`.)*
  3. Only §4 C1/C3 permitted fields and whitespace are removed; key order preserved; no content added beyond canonical compaction. A cleaner that strips any §5 field **fails B4**.

## 7. Fixture corpus (B1 deliverable #2)

Location: `token-saver/benchmark/fixtures/l1_prompts.json`
Generator: `token-saver/benchmark/gen_l1_fixtures.py` (committed; deterministic — rerun reproduces the same file).
Checksum: `token-saver/benchmark/fixtures/l1_prompts.json.sha256` — **B2 and B4 runners MUST fail on any checksum mismatch** (pinned-fixture integrity).

| category | count | purpose |
|---|---|---|
| `rag` | 12 | retrieval context w/ dead metadata (score/source/page/embedding) |
| `json_doc` | 12 | pretty-printed JSON documents (logs, configs, API responses) |
| `system_dup` | 8 | duplicate/empty system blocks + retrieval context |
| `log_trace` | 8 | JSON-lines telemetry with harness plumbing fields |
| `control` | 6 | **negative controls** — plain prose / non-RAG JSON that L1 must leave byte-identical |

≥40 total = **46 prompts** (≥15% reduction target is measured on `rag`+`json_doc`+`system_dup`+`log_trace`; `control` must be ~0% to prove the negative list holds).

---

*Version 1.1 — amended 2026-09-15 by @product-manager per @project-manager's audit ruling on `c0b3565`. **What changed from v1.0:** §4 C3 is now a conserved-only list — C3 may strip scoring math, embeddings, byte-locators, and pure harness plumbing, but **may no longer strip anything a question could plausibly attribute or cite**; provenance names, identifiers, metadata/tag wrappers, timestamps, and `query` moved to the §5 negative list (v1.0 treated several of these as dead). §6 now asserts provenance survival to close the round-trip detection gap that let the `rag-005` counterexample pass. Net fixture effect: strippable-4 reduction ≈86% (v1.0) → amended (see PM's measured 83.2%); gate still clears 15% with wide margin; customer-facing claim is C1-only ≈30% with C3 upside stated separately, per PM decomposition ruling. Amendments require an audit note, not a silent edit.*