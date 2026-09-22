"""L1 lossless structural cleanup (Phase B, B2).

Pure deterministic transform per l1-taxonomy.md **v1.1**, amended by the
product-spec v1.2 independent eligibility gate. Implements the v1.1 §4 dead
list and §5 negative list exactly:

- C1: JSON whitespace compaction — eligible blocks (whole content parses as
  JSON object/array, or every non-empty line is a JSON object = JSON-lines)
  re-serialized compact, key order preserved, values byte-unchanged.
- C2: duplicate/empty system-block removal (adjacent byte-identical systems;
  empty/whitespace-only systems).
- C3: dead retrieval/RAG metadata drop — shape-gated: only inside objects
  that have a reserved content sibling (content/text/answer/passage/doc).
  Dead list (v1.1): scoring math, embedding/float-arrays >32, char/token
  locators, harness/query-log plumbing, and empty containers.

- Negative list (§5, v1.1 — L1 MUST NOT strip): prose, tool defs, images,
key order, non-adjacent systems, fields inside
non-RAG-shaped JSON, the user's literal `query` field, timestamps
(created_at/updated_at/modified_at/expires_at/retrieved_at — a question may
ask which is newest), provenance/citation fields (source/title/url/path/
filename/bucket/collection/index_name/page/page_number/chunk_id/chunk_index/
block_id/doc_id/passage_id/source_id/id), and object-valued wrappers
(metadata/attributes/tags/custom/raw) capable of carrying them.

Contract (§6): output depends only on input bytes; clean is idempotent;
clean(x) == y reproducible across runs/processes. No clocks, RNG, config
reads inside the clean path.
"""
from __future__ import annotations

import json
from typing import Any

from .tool_protocol import is_tool_protocol_message, is_tool_result_compressible

# Reserved answer-bearing keys (taxonomy §4) — never dropped, never recursed.
RESERVED_KEYS = {"content", "text", "answer", "passage", "doc", "query"}

# Shape-gate keys (taxonomy §4 eligible structure): ONLY these open RAG
# scope. `answer`/`doc`/`query` stay reserved (byte-preserved) but do NOT
# open the gate on their own — B2-c regression: `{"query":"…","score":0.9}`
# must keep `score` (a bare query is not a retrieval result).
GATE_KEYS = {"content", "text", "passage"}

# C3 dead fields (exact-name match), taxonomy v1.1 §4 — narrower than v1.0:
# provenance, identifiers, and timestamps were moved to the negative list.
DEAD_FIELDS = {
    # scoring math
    "score", "relevance", "relevance_score", "similarity", "distance",
    "cosine", "rerank_score",
    # embeddings / vectors (taxonomy v1.1 §4 row 2, by exact name regardless
    # of array length — an 8-element `embedding` is still an embedding)
    "embedding", "vector", "vector_score",
    # internal char/token locators (page locators are PROVENANCE, not here)
    "char_start", "char_end", "token_start", "length", "offset",
    # harness / query-log plumbing
    "retrieval", "pagination", "total", "limit", "next_page", "has_more",
    "request_id", "session_id", "generation", "query_id", "trace",
    "span_id", "latency_ms", "ts", "level",
}

# Provenance / citation fields — NEGATIVE LIST (v1.1 §5): conserved even
# inside RAG-shaped objects; a question may ask the model to cite them.
PROVENANCE_FIELDS = {
    "source", "title", "url", "path", "filename", "bucket", "collection",
    "index_name", "page", "page_number", "chunk_id", "chunk_index",
    "block_id", "doc_id", "passage_id", "source_id", "id",
}

# Timestamps — NEGATIVE LIST (v1.1 §5): "which is newest" is answer-bearing.
TIMESTAMP_FIELDS = {
    "timestamp", "created_at", "updated_at", "modified_at", "expires_at",
    "retrieved_at",
}

# Object-valued wrappers — NEGATIVE LIST (v1.1 §5): capable of carrying
# provenance; never stripped, never recursed for dead drops.
WRAPPER_FIELDS = {"metadata", "attributes", "tags", "custom", "raw"}

# Float-array threshold: any float-array field with >32 numeric elements is
# dead (taxonomy v1.1 §4 row 2).
_FLOAT_ARRAY_MIN_LEN = 32

_COMPACT = {"separators": (",", ":"), "ensure_ascii": False}


# Empty-container drop (taxonomy §4 row 5): empty string/array/object only.
# `null` is a DISTINCT value ("author: null" can be the answer to "who
# wrote it?") and is conserved — B2-c regression.
def _is_empty(v: Any) -> bool:
    return v == "" or v == [] or v == {}


def _is_big_float_array(v: Any) -> bool:
    return (
        isinstance(v, list)
        and len(v) > _FLOAT_ARRAY_MIN_LEN
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
    )


def _is_rag_shaped(obj: dict) -> bool:
    """Shape gate: object carries at least one §4 eligible-structure key
    (content/text/passage) — NOT answer/doc/query (B2-c)."""
    return any(k in GATE_KEYS for k in obj)


def _clean_obj(obj: Any, rag_scope: bool, c3: bool) -> Any:
    """Recursive C3 clean. rag_scope=True once inside a RAG-shaped object —
    dead fields drop anywhere in that object graph (taxonomy §4).
    Negative-list fields are conserved even in RAG scope (v1.1)."""
    if isinstance(obj, list):
        return [_clean_obj(x, rag_scope, c3) for x in obj]
    if not isinstance(obj, dict):
        return obj
    is_rag = _is_rag_shaped(obj)
    scope = c3 and (rag_scope or is_rag)
    out: dict = {}
    for k, v in obj.items():
        if k in RESERVED_KEYS:
            out[k] = v  # reserved: byte-for-byte, no recursion, no drop
            continue
        if scope:
            if k in PROVENANCE_FIELDS or k in TIMESTAMP_FIELDS:
                out[k] = _clean_obj(v, scope, c3)
                continue
            if k in WRAPPER_FIELDS:
                # v1.1: wrappers conserved whole (may carry provenance)
                out[k] = v
                continue
            if k in DEAD_FIELDS:
                continue
            cv = _clean_obj(v, scope, c3)
            if _is_empty(cv) or _is_big_float_array(cv):
                continue
            out[k] = cv
        else:
            out[k] = _clean_obj(v, False, c3)
    return out


def _dump(obj: Any, compact: bool) -> str:
    if compact:
        return json.dumps(obj, **_COMPACT)
    return json.dumps(obj, ensure_ascii=False)


def _clean_json_block(text: str, c1: bool, c3: bool) -> str | None:
    """C1+C3 on a whole-block JSON object/array. Returns None if not JSON."""
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")
            or t.startswith("[") and t.endswith("]")):
        return None
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, (dict, list)):
        return None
    cleaned = _clean_obj(obj, False, c3) if c3 else obj
    return _dump(cleaned, compact=c1)


def _is_json_line(line: str) -> bool:
    t = line.strip()
    if not t.startswith("{"):
        return False
    try:
        return isinstance(json.loads(t), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def _clean_json_lines(text: str, c1: bool, c3: bool) -> str | None:
    """C1+C3 on JSON-lines blocks: every non-empty line must be a JSON
    object, else the block is untouched (conservative)."""
    lines = text.splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty or not all(_is_json_line(ln) for ln in nonempty):
        return None
    out = []
    for ln in lines:
        if not ln.strip():
            out.append(ln)
            continue
        obj = json.loads(ln)
        cleaned = _clean_obj(obj, False, c3) if c3 else obj
        out.append(_dump(cleaned, compact=c1))
    return "\n".join(out)


# AC-P6j reuse: bounded scan bound, same spirit as grounded.py's
# _MAX_JSON_SCAN_ATTEMPTS — pathological inputs must not hang the proxy.
_MAX_EMBEDDED_SCAN_ATTEMPTS = 64


def _embedded_json_spans(text: str) -> list[tuple[int, int, dict]]:
    """Locate balanced JSON objects anywhere in ``text`` (AC-P6j's widened
    envelope scan, lifted for L1): left-to-right, deterministic, pure.
    Returns non-overlapping (start, end, obj) spans; the next search starts
    AFTER a successfully parsed span, so objects nested inside an accepted
    span are never returned separately (the recursive _clean_obj pass on the
    outer object already covers them)."""
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int, dict]] = []
    idx = text.find("{")
    attempts = 0
    while idx != -1 and attempts < _MAX_EMBEDDED_SCAN_ATTEMPTS:
        attempts += 1
        try:
            obj, end = decoder.raw_decode(text, idx)
        except (ValueError, RecursionError):
            obj, end = None, idx
        if isinstance(obj, dict):
            spans.append((idx, end, obj))
            idx = text.find("{", end)
        else:
            idx = text.find("{", idx + 1)
    return spans


def _clean_embedded_json(text: str, c1: bool, c3: bool) -> str:
    """C1+C3 on JSON spans located wherever they appear in the text
    (prose-wrapped objects, ```json fenced blocks, trailing questions in
    the same message). The prose around each located span is conserved
    byte-for-byte; only the span is re-dumped (compact if c1). Text with
    no parseable object returns unchanged."""
    spans = _embedded_json_spans(text)
    if not spans:
        return text
    out: list[str] = []
    prev = 0
    changed = False
    for start, end, obj in spans:
        out.append(text[prev:start])
        original = text[start:end]
        cleaned = _clean_obj(obj, False, c3) if c3 else obj
        dumped = _dump(cleaned, compact=c1)
        if dumped != original:
            changed = True
        out.append(dumped)
        prev = end
    out.append(text[prev:])
    result = "".join(out)
    return result if changed else text


def clean_text(text: str, c1: bool = True, c3: bool = True,
               embedded: bool = False) -> str:
    """Clean one string content block. Non-JSON text is returned
    byte-for-byte unchanged (negative list: prose is never touched).

    embedded=False (default, committed AC-P1e contract): only whole-block
    JSON and JSON-lines are cleaned — control-012 (prose-mixed) and
    control-013 (```json fenced) pin that embedded/fenced spans are
    conserved byte-for-byte. embedded=True additionally cleans JSON
    objects located anywhere in the text (AC-P6j scan reuse); surrounding
    prose is still conserved byte-for-byte. Requires a corpus re-pin
    ruling before the default flips — see clean_messages.
    """
    cleaned = _clean_json_block(text, c1, c3)
    if cleaned is not None:
        return cleaned
    cleaned = _clean_json_lines(text, c1, c3)
    if cleaned is not None:
        return cleaned
    if not embedded:
        return text
    return _clean_embedded_json(text, c1, c3)


def _clean_content(content: Any, c1: bool, c3: bool, embedded: bool) -> Any:
    if isinstance(content, str):
        return clean_text(content, c1, c3, embedded)
    if isinstance(content, list):
        return [
            ({**part, "text": clean_text(part["text"], c1, c3, embedded)}
             if isinstance(part, dict) and part.get("type") == "text"
             and isinstance(part.get("text"), str)
             else part)
            for part in content
        ]
    return content


def _compact_json_whitespace(text: str) -> str | None:
    """Validate and compact a JSON value without changing its token lexemes."""
    candidate = text.strip()
    if not (candidate.startswith(("{", "[")) and candidate.endswith(("}", "]"))):
        return None
    try:
        json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    out: list[str] = []
    in_string = False
    escaped = False
    for char in candidate:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            out.append(char)
            in_string = True
        elif not char.isspace():
            out.append(char)
    return "".join(out)


def _clean_protocol_content(content: Any, c1: bool) -> Any:
    """C1-only content cleanup for protocol-adjacent data.

    Decoding and re-encoding JSON can rewrite numeric precision, negative zero,
    and escape spelling. This scanner removes only structural whitespace after
    JSON validation, preserving every value lexeme byte-for-byte.
    """
    if not c1:
        return content
    if isinstance(content, str):
        compact = _compact_json_whitespace(content)
        return compact if compact is not None else content
    if isinstance(content, list):
        return [
            ({**part, "text": _clean_protocol_content(part["text"], c1)}
             if isinstance(part, dict) and part.get("type") == "text"
             and isinstance(part.get("text"), str)
             else part)
            for part in content
        ]
    return content


def clean_messages(
    messages: list[dict],
    c1: bool = True,
    c2: bool = True,
    c3: bool = True,
    embedded: bool = False,
    tool_result_compression_enabled: bool = True,
    tool_calling: bool = False,
) -> list[dict]:
    """L1-clean a message list. Pure: same input -> byte-identical output.

    c1/c2/c3 flags exist for the B2 decomposition measurement only; the
    production path uses the defaults (all on).

    embedded=False (default) keeps the committed AC-P1e contract: only
    whole-block JSON and JSON-lines are cleaned; prose-embedded and
    ```json-fenced spans are conserved byte-for-byte (control-012/-013
    pin this). embedded=True widens C1/C3 to JSON spans located anywhere
    in the text (AC-P6j's balanced-object scan lifted into the cleaner);
    the prose around each located span is still conserved byte-for-byte.
    Flipping the default requires a corpus re-pin ruling: two committed
    control fixtures encode the embedded=False contract, and the fixture
    checksum (AC-P1e) plus the published shape numbers re-base with it.
    """
    out: list[dict] = []
    for msg in messages:
        # Assistant tool-call envelopes are protocol-critical. A tool result
        # retains its envelope and tool_call_id, but its content is eligible
        # for the same lossless JSON cleanup as ordinary message content.
        if is_tool_protocol_message(msg):
            if (
                tool_result_compression_enabled
                and is_tool_result_compressible(msg)
            ):
                cleaned = _clean_protocol_content(msg.get("content"), c1)
                out.append(
                    {**msg, "content": cleaned}
                    if cleaned is not msg.get("content") else msg
                )
            else:
                out.append(msg)
            continue
        role = msg.get("role")
        content = msg.get("content")
        if tool_calling:
            # A tool-bearing request may clean non-protocol system content,
            # but never removes/reorders messages or rewrites user content.
            # C2/C3 are intentionally excluded to preserve its envelope.
            cleaned = (
                _clean_protocol_content(content, c1)
                if role == "system" else content
            )
            out.append({**msg, "content": cleaned} if cleaned is not content else msg)
            continue
        if c2 and role == "system" and isinstance(content, str):
            if content.strip() == "":
                continue  # empty/whitespace-only system block
            prev = out[-1] if out else None
            if (prev is not None and prev.get("role") == "system"
                    and prev.get("content") == content):
                continue  # adjacent byte-identical system block
        cleaned = _clean_content(content, c1, c3, embedded)
        out.append({**msg, "content": cleaned} if cleaned is not content
                   else msg)
    return out


def l1_eligible(messages: list[dict] | None, route: str) -> bool:
    """Taxonomy v1.2 §5 (ruling A): L1's own eligibility gate, independent
    of the lossy compress/passthrough router.

    L1 is a LOSSLESS transform, so it may run on passthrough-classified
    content — byte-identity to upstream is only guaranteed for lossy
    compression on all content and for L1 on CODE content; L1 on JSON/RAG
    carries the round-trip reversibility guarantee instead (AC-P1f).
    `route` is accepted (and asserted compress/passthrough) so callers pass
    the classifier output they already hold; re-introducing the route gate
    HERE (e.g. `and route != "passthrough"`) must drop --production-path
    end-to-end yield to 0.0% — that sensitivity is the point of the shared
    predicate: one gate, imported by main.py AND the benchmark harness.
    """
    if route not in ("compress", "passthrough"):
        return False
    return bool(messages)
