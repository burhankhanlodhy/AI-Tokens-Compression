"""v1.1 semantic-cache vertical-slice contract tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import semantic_cache


def test_canonical_hashes_are_stable_and_parameter_scoped():
    body_a = {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.2,
        "stream": False,
    }
    body_b = {
        "stream": False,
        "temperature": 0.2,
        "messages": [{"content": "hello", "role": "user"}],
        "model": "openai/gpt-4o",
    }
    body_c = {**body_a, "temperature": 0.7}

    assert semantic_cache.canonical_prompt_hash(body_a) == semantic_cache.canonical_prompt_hash(body_b)
    assert semantic_cache.request_parameters_hash(body_a) == semantic_cache.request_parameters_hash(body_b)
    assert semantic_cache.canonical_prompt_hash(body_a) == semantic_cache.canonical_prompt_hash(body_c)
    assert semantic_cache.request_parameters_hash(body_a) != semantic_cache.request_parameters_hash(body_c)


def test_versions_are_server_derived_and_dimensions_are_pinned(monkeypatch):
    monkeypatch.setenv("EMBEDDING_RELAY", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
    assert semantic_cache.derive_embedding_version() == "openai:text-embedding-3-small@1536"
    assert semantic_cache.derive_quality_version() == "1.0.1"


def test_lookup_result_kinds_are_explicit():
    assert [kind.value for kind in semantic_cache.SemanticLookupKind] == [
        "hit", "threshold_miss", "no_compatible_row", "not_attempted"
    ]
    assert semantic_cache.SemanticLookupResult.no_compatible_row().kind.value == "no_compatible_row"
    assert semantic_cache.SemanticLookupResult.not_attempted().kind.value == "not_attempted"
