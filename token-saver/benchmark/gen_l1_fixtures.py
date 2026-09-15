"""B1: deterministic generator for the L1 lossless-cleanup fixture corpus.

Singleton source of truth for `benchmark/fixtures/l1_prompts.json`.
Rerunning this script MUST reproduce the file byte-for-byte (no clocks, no
RNG, no filesystem iteration) so the pinned SHA-256 checksum stays stable.
Run:  python3 benchmark/gen_l1_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "l1_prompts.json"
SHA = Path(__file__).parent / "fixtures" / "l1_prompts.json.sha256"


# --------------------------------------------------------------------------
# Reusable pieces. Fixed string constants only -> deterministic.
# --------------------------------------------------------------------------

_QUESTION_POOL = [
    "Based on the retrieved passages, what is the supported way to add a custom tokenizer to a reranker?",
    "Summarize the schema of the JSON configuration below and list every required field.",
    "Which integration was flagged as the highest-risk regression in the returned log events?",
    "From the API response embedded below, what HTTP status should the client retry on and with what backoff?",
    "Looking only at the retrieved chunks, state the exact default value of the cache TTL and cite its section.",
    "The log trace below contains a crash signature. Identify the component and the error code.",
    "Given the JSON document, produce a normalized config that omits nothing of substance.",
    "Which of the retrieved passages contradicts the default quota limit, and which one supports it?",
    "From the telemetry JSON-lines, compute the median latency across the critical-path spans.",
    "Extract the re-run CLI flags that the release notes describe as safe to pass with --strict.",
    "What retention policy do the retrieved documents describe for access audit logs?",
    "The SQL query embedded in the JSON below references a dropped column. Name the column.",
]

_RAG_DOCS = [
    ("Retriever Setup", "Configure the reranker with the mmarco multilingual tokenizer; do not use the default word-piece model for CJK corpora."),
    ("Quota Policy", "The default cache TTL is 3600 seconds (section 4.1). Webhook deliveries use a 30s timeout with exponential backoff."),
    ("Retention", "Access audit logs are retained for 90 days and purged nightly; full-text backups are kept for 12 months."),
    ("Rollout", "Flagged integration 'v3-clickstream' shows a 2.1x regression in p95 under synthetic load and should be gated before general release."),
    ("Error Handling", "A 429 response must be retried with backoff starting at 1s doubling to 60s; a 503 with a Retry-After header is honored verbatim."),
    ("Cluster Quotas", "The free tier default quota is 1,000 requests/hour per key; the pro tier raises it to 10,000 with burst to 2,000."),
]


def _retrieval_hits(index: int, doc_title: str, doc_text: str) -> dict:
    """One retrieval hit: dead metadata siblings + one reserved content field."""
    n_tokens = max(8, len(doc_text) // 4)
    return {
        "index": index,
        "chunk_id": f"chunk-{index:03d}",
        "source": f"corpus/{doc_title.replace(' ', '_').lower()}.md",
        "page": 1 + (index % 40),
        "score": round(0.90 - index * 0.02, 2),
        "relevance_score": round(0.90 - index * 0.02, 2),
        "distance": round(0.10 + index * 0.02, 2),
        "token_start": index * n_tokens,
        "char_start": index * 512,
        "char_end": index * 512 + 200,
        "timestamp": f"2026-09-1{index % 5}T10:{index:02d}:00Z",
        "embedding": [round((i * 0.001) % 1, 4) for i in range(64)],
        "metadata": {"doc_id": f"doc-{index:04d}", "author": "jane", "lang": "en"},
        "content": doc_text,
    }


def _retrieval_block(prompt_idx: int) -> str:
    hits = []
    for h in range(3):
        title, text = _RAG_DOCS[(prompt_idx + h) % len(_RAG_DOCS)]
        hits.append(_retrieval_hits(h, title, text))
    payload = {
        "query": _QUESTION_POOL[prompt_idx % len(_QUESTION_POOL)],
        "top_k": 3,
        "retrieval": {"total": 3, "limit": 3, "offset": 0, "next_page": None, "latency_ms": 142},
        "hits": hits,
    }
    return json.dumps({"retrieved_documents": payload}, indent=2)


def _json_doc(prompt_idx: int) -> str:
    """Pretty-printed JSON document with a config/named-worthy schema."""
    docs = [
        {
            "service": "reranker",
            "version": "3.4.1",
            "name": "mmarco-reranker",
            "model": "bge-reranker-v2",
            "tokenizer": "mmarco-multilingual",
            "timeout_ms": 30_000,
            "retries": {"max_attempts": 3, "base": 1.0, "ceiling": 60.0},
            "cache": {"ttl_seconds": 3600, "enabled": True, "max_entries": 50_000},
            "resources": {"workers": 8, "max_batch": 128, "gpu": "A10"},
            "flags": ["strict", "no_cache_write", "tls_verify"],
        },
        {
            "api_response": {
                "status": 503,
                "headers": {"Retry-After": "30"},
                "body": {"error": {"code": "over_capacity", "retryable": True, "detail": "region us-east over provisioned"}},
            }
        },
        {
            "release_notes": {
                "version": "2.9.0",
                "date": "2026-09-01",
                "highlights": ["native re-rank batching", "JSON-mode output", "strict flag parity"],
                "breaking": ["--tokenizer renamed to --encoder"],
            }
        },
    ]
    # Rotate a couple of docs; re-serialize with pretty indent to give C1 real whitespace to strip.
    return json.dumps(docs[(prompt_idx * 7) % len(docs)], indent=4)


def _system_dup(prompt_idx: int) -> list[dict]:
    base = _retrieval_block(prompt_idx)
    question = _QUESTION_POOL[prompt_idx % len(_QUESTION_POOL)]
    # Duplicate (byte-identical) system blocks + one empty system block.
    dup = {
        "role": "system",
        "content": "You are a RAG assistant. Answer strictly from the retrieved documents; if the answer is not present, reply 'NOT_FOUND'. Cite the passage content verbatim when possible.",
    }
    empty = {"role": "system", "content": "   "}
    return [dup, dict(dup), {"role": "system", "content": ""}, empty, {"role": "user", "content": base}, {"role": "user", "content": question}]


def _log_trace(prompt_idx: int) -> str:
    events = []
    for i in range(6):
        events.append(
            {
                "ts": f"2026-09-14T10:{prompt_idx % 10}{i}:00.000Z",
                "level": "error" if i % 3 == 0 else "info",
                "span_id": f"span-{prompt_idx}-{i}",
                "trace_id": f"trace-{prompt_idx:03d}",
                "trace": {"root": "handler", "depth": i, "flags": 0x0},
                "metric": {"latency_ms": 40 + i * 63, "cpu": 0.3 + i * 0.11},
                "msg": "component=reranker phase=score err=ECONNRESET code=52" if i % 3 == 0
                       else f"component=ingest phase=parse records={i*10} ok=true",
            }
        )
    return "\n".join(json.dumps(e) for e in events)


def _control(prompt_idx: int) -> str:
    """Negative controls: must be left byte-identical by L1."""
    controls = [
        "Please rewrite the following paragraph to be clearer, keeping all facts: The hybrid search merges sparse and dense rankings using a weighted sum.",
        "Write a unit test for a function that retries a network call with exponential backoff.",
        "Explain how exact-prefix caching differs from semantic caching in one sentence.",
        "Given these notes, draft a short changelog entry: \n- Fixed double-counting of cache savings.\n- Added per-model SD gating to the benchmark harness.",
        "What is the capital of France and what river flows through it?",
        "Translate this sentence into Spanish exactly: The proxy strips only structural metadata, never answer content.",
    ]
    return controls[prompt_idx % len(controls)]


# --------------------------------------------------------------------------
# Assemble
# --------------------------------------------------------------------------

def build() -> dict:
    prompts: list[dict] = []
    ctr = {"rag": 0, "json_doc": 0, "system_dup": 0, "log_trace": 0, "control": 0}

    def add(cat: str, messages: list[dict]) -> None:
        ctr[cat] += 1
        prompts.append(
            {"id": f"{cat}-{ctr[cat]:03d}", "category": cat, "messages": messages}
        )

    for i in range(12):
        # Strip-able JSON is a STANDALONE message content (whole block parses
        # as JSON -> C1/C3 eligibility per taxonomy §4); question is a separate
        # user turn. Real RAG requests read exactly this way (ctx + instruction).
        add("rag", [{"role": "user", "content": _retrieval_block(i)},
                    {"role": "user", "content": _QUESTION_POOL[i % len(_QUESTION_POOL)]}])
        add("json_doc", [{"role": "user", "content": _json_doc(i)},
                         {"role": "user", "content": _QUESTION_POOL[(i + 4) % len(_QUESTION_POOL)]}])
    for i in range(8):
        add("system_dup", _system_dup(i))
        add("log_trace", [{"role": "system", "content": "You extract facts from telemetry. Only report what is present."},
                          {"role": "user", "content": _log_trace(i)},
                          {"role": "user", "content": _QUESTION_POOL[(i + 8) % len(_QUESTION_POOL)]}]),
    for i in range(6):
        add("control", [{"role": "user", "content": _control(i)}])

    return {"version": 1, "description": "B1 L1 lossless structural-cleanup fixture (AC-P1e). Categories rag/json_doc/system_dup/log_trace are strippable; control must be untouched.", "prompts": prompts}


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    OUT.write_text(raw, encoding="utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    SHA.write_text(f"{digest}  {OUT.name}\n", encoding="utf-8")
    from collections import Counter
    print(json.dumps(Counter(p["category"] for p in data["prompts"])))
    print(f"total prompts: {len(data['prompts'])}")
    print(f"wrote: {OUT}")
    print(f"SHA-256: {digest}")


if __name__ == "__main__":
    main()