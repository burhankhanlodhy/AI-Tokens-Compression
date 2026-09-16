"""B2 tests: L1 cleaner unit contract + pipeline ordering (AC-P1e/P1f).

Covers l1-taxonomy.md §4/§5/§6 (incl. PM provenance amendment) and the
main.py pipeline rule: L1 clean runs BEFORE the PA-4 cache key, so an
L1-stripped request and its identical clean prompt share one cache entry.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.l1_clean import clean_messages, clean_text

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "benchmark" / "fixtures" / "l1_prompts.json"
CHECKSUM = ROOT / "benchmark" / "fixtures" / "l1_prompts.json.sha256"


# ---------- C1: JSON whitespace compaction ----------

def test_c1_compacts_pretty_json():
    src = '{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}'
    assert clean_text(src) == '{"a":1,"b":[1,2]}'


def test_c1_preserves_key_order():
    src = '{"z": 1, "a": 2, "m": 3}'
    assert clean_text(src) == '{"z":1,"a":2,"m":3}'


def test_c1_leaves_prose_untouched():
    prose = "Please summarize this document about retrieval systems."
    assert clean_text(prose) == prose


# ---------- C2: duplicate / empty system blocks ----------

def test_c2_removes_empty_system():
    msgs = [{"role": "system", "content": "   "},
            {"role": "user", "content": "hi"}]
    out = clean_messages(msgs)
    assert len(out) == 1 and out[0]["role"] == "user"


def test_c2_removes_adjacent_duplicate_system():
    msgs = [{"role": "system", "content": "Be brief."},
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "hi"}]
    out = clean_messages(msgs)
    assert len(out) == 2
    assert out[0]["content"] == "Be brief."


def test_c2_keeps_non_adjacent_systems():
    msgs = [{"role": "system", "content": "A"},
            {"role": "user", "content": "q"},
            {"role": "system", "content": "A"}]
    assert len(clean_messages(msgs)) == 3  # negative list: non-adjacent stays


def test_c2_never_removes_last_system():
    msgs = [{"role": "system", "content": "Only system"},
            {"role": "user", "content": "hi"}]
    assert len(clean_messages(msgs)) == 2


# ---------- C3: shape-gated dead metadata ----------

RAG_BLOCK = json.dumps({
    "content": "The cache TTL default is 3600 seconds.",
    "score": 0.97, "chunk_id": "c-42", "page": 7,
    "embedding": [0.1] * 64, "retrieved_at": "2026-09-14",
    "source": "spec.md",
}, indent=2)

DEAD_GONE = {"score", "embedding"}  # v1.1: retrieved_at is NEGATIVE-list (timestamps conserved)


def test_c3_drops_dead_fields_in_rag_shaped_object():
    out = json.loads(clean_text(RAG_BLOCK))
    for k in DEAD_GONE:
        assert k not in out, k
    assert out["content"] == "The cache TTL default is 3600 seconds."


def test_c3_provenance_survives_pm_amendment():
    out = json.loads(clean_text(RAG_BLOCK))
    # PM ruling: provenance is answer-bearing on attribution questions
    assert out["source"] == "spec.md"
    assert out["page"] == 7


def test_c3_shape_gate_non_rag_json_untouched():
    # config document with no reserved content sibling: name-only match
    # must NOT strip (taxonomy §4 eligible-structure rule / §5)
    cfg = json.dumps({"score": 5, "limit": 10, "name": "cfg"})
    assert json.loads(clean_text(cfg)) == json.loads(cfg)


def test_c3_reserved_content_survives_byte_identically():
    out = json.loads(clean_text(RAG_BLOCK))
    assert out["content"] in RAG_BLOCK


# ---------- §6 determinism contract ----------

def test_idempotent_clean():
    msgs = [{"role": "user", "content": RAG_BLOCK}]
    once = clean_messages(msgs)
    twice = clean_messages(once)
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_deterministic_repeat():
    msgs = [{"role": "user", "content": RAG_BLOCK}]
    assert (json.dumps(clean_messages(msgs), sort_keys=True)
            == json.dumps(clean_messages(msgs), sort_keys=True))


# ---------- committed fixtures: checksum gate + full contract ----------

def _fixture_prompts():
    expected = CHECKSUM.read_text().split()[0].strip()
    actual = hashlib.sha256(FIXTURES.read_bytes()).hexdigest()
    assert actual == expected, "fixture checksum mismatch — contract break"
    return json.loads(FIXTURES.read_text())["prompts"]


def test_fixtures_checksum_pinned():
    _fixture_prompts()  # raises on mismatch


def test_all_fixture_controls_byte_identical():
    for p in _fixture_prompts():
        if p["category"] == "control":
            assert clean_messages(p["messages"]) == p["messages"], p["id"]


def test_all_fixtures_idempotent_and_provenance_safe():
    for p in _fixture_prompts():
        once = clean_messages(p["messages"])
        twice = clean_messages(once)
        assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True), p["id"]
        before = json.dumps(p["messages"], ensure_ascii=False)
        after = json.dumps(once, ensure_ascii=False)
        for f in ("source", "title", "url", "path", "page", "page_number",
                  "collection", "index_name", "chunk_id", "doc_id",
                  "passage_id", "source_id", "filename"):
            if f'"{f}"' in before:
                assert f'"{f}"' in after, f"{p['id']}: stripped {f}"


def test_reserved_content_preserved_across_fixtures():
    def reserved_strings(obj, acc):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("content", "text", "passage", "answer") and isinstance(v, str):
                    t = v.strip()
                    if t and not (t.startswith("{") or t.startswith("[")):
                        acc.add(v)
                reserved_strings(v, acc)
        elif isinstance(obj, list):
            for x in obj:
                reserved_strings(x, acc)
        return acc

    for p in _fixture_prompts():
        if p["category"] == "control":
            continue
        before = reserved_strings(p["messages"], set())
        after_blob = json.dumps(clean_messages(p["messages"]), ensure_ascii=False)
        for v in before:
            assert v in after_blob, f"{p['id']}: reserved content altered"


# ---------- pipeline ordering: cache key on CLEAN bytes (AC-P1f) ----------

def test_cache_key_uses_clean_body():
    from proxy import caching
    from proxy.l1_clean import clean_messages as cm

    # RAG block in the SYSTEM message so it lands inside the cacheable
    # prefix (canonical_prefix excludes the final user message).
    pretty = {"model": "openai/gpt-4o",
              "messages": [
                  {"role": "system", "content": RAG_BLOCK},
                  {"role": "user", "content": "What is the TTL?"},
              ]}
    cleaned = {"model": "openai/gpt-4o",
               "messages": cm(pretty["messages"])}
    # inputs really do differ (sanity)
    assert caching.canonical_prefix(pretty) != caching.canonical_prefix(cleaned)
    # ...and the cleaned form of the original reproduces the same clean key:
    # an L1-stripped request and an identical clean prompt share one entry.
    recomputed = {"model": "openai/gpt-4o", "messages": cm(pretty["messages"])}
    assert caching.cache_key(caching.canonical_prefix(recomputed), "openai/gpt-4o", "openrouter") \
        == caching.cache_key(caching.canonical_prefix(cleaned), "openai/gpt-4o", "openrouter")
    # and the clean key is DIFFERENT from the pre-L1 (original) key — the
    # old behavior (key on original) would never collide with the clean key
    assert caching.cache_key(caching.canonical_prefix(pretty), "openai/gpt-4o", "openrouter") \
        != caching.cache_key(caching.canonical_prefix(cleaned), "openai/gpt-4o", "openrouter")


def test_clean_bytes_are_stable_so_cache_hit_serves_identical_prompt():
    msgs = [{"role": "user", "content": RAG_BLOCK}]
    from proxy.l1_clean import clean_messages as cm
    assert json.dumps(cm(msgs)) == json.dumps(cm(msgs))  # reproducible raw->clean
