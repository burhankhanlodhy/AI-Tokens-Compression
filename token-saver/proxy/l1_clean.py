"""L1 lossless structural cleanup (Phase B, B2).

Pure deterministic transform per l1-taxonomy.md **v1.1** (B1, commits
c0b3565 + 105a832). Implements the v1.1 §4 dead list and §5 negative list
exactly:

- C1: JSON whitespace compaction — eligible blocks (whole content parses as
  JSON object/array, or every non-empty line is a JSON object = JSON-lines)
  re-serialized compact, key order preserved, values byte-unchanged.
- C2: duplicate/empty system-block removal (adjacent byte-identical systems;
  empty/whitespace-only systems).
- C3: dead retrieval/RAG metadata drop — shape-gated: only inside objects
  that have a reserved content sibling (content/text/answer/passage/doc).
  Dead list (v1.1): scoring math, embedding/float-arrays >32, char/token
  locators, harness/query-log plumbing, and empty containers.

Negative list (§5, v1.1 — L1 MUST NOT strip): prose, tool defs, images,
key order, non-adjacent systems, passthrough routes, fields inside
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

# Reserved answer-bearing keys (taxonomy §4) — never dropped, never recursed.
RESERVED_KEYS = {"content", "text", "answer", "passage", "doc", "query"}

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


def _is_empty(v: Any) -> bool:
    return v == "" or v == [] or v == {} or v is None


def _is_big_float_array(v: Any) -> bool:
    return (
        isinstance(v, list)
        and len(v) > _FLOAT_ARRAY_MIN_LEN
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
    )


def _is_rag_shaped(obj: dict) -> bool:
    """Shape gate: object carries at least one reserved content sibling."""
    return any(k in RESERVED_KEYS for k in obj)


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


def clean_text(text: str, c1: bool = True, c3: bool = True) -> str:
    """Clean one string content block. Non-JSON text is returned
    byte-for-byte unchanged (negative list: prose is never touched)."""
    cleaned = _clean_json_block(text, c1, c3)
    if cleaned is not None:
        return cleaned
    cleaned = _clean_json_lines(text, c1, c3)
    if cleaned is not None:
        return cleaned
    return text


def _clean_content(content: Any, c1: bool, c3: bool) -> Any:
    if isinstance(content, str):
        return clean_text(content, c1, c3)
    if isinstance(content, list):
        return [
            ({**part, "text": clean_text(part["text"], c1, c3)}
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
) -> list[dict]:
    """L1-clean a message list. Pure: same input -> byte-identical output.

    c1/c2/c3 flags exist for the B2 decomposition measurement only; the
    production path uses the defaults (all on).
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if c2 and role == "system" and isinstance(content, str):
            if content.strip() == "":
                continue  # empty/whitespace-only system block
            prev = out[-1] if out else None
            if (prev is not None and prev.get("role") == "system"
                    and prev.get("content") == content):
                continue  # adjacent byte-identical system block
        cleaned = _clean_content(content, c1, c3)
        out.append({**msg, "content": cleaned} if cleaned is not content
                   else msg)
    return out
