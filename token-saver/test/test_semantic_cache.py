"""AC-PC3 contract tests for the pgvector semantic-cache lookup seam.

The table/image migration is DBA-owned.  These tests pin the Dev-owned
boundary: lookup only runs behind the deployment flag, and every query scopes
by tenant, provider, model, and embedding/quality compatibility parameters.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import get_settings
from proxy import semantic_cache


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return _Cursor(self.row)


def _scope(**overrides):
    values = {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "provider": "openrouter",
        "model": "google/gemini-3.5-flash-lite",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 2,
        "embedding_version": "2026-09-18",
        "quality_version": "pc1-calibration-v1",
        "request_parameters_hash": "3f3a4d0c",
    }
    values.update(overrides)
    return semantic_cache.SemanticLookupScope(**values)


def test_semantic_cache_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SEMANTIC_CACHE_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().semantic_cache_enabled is False
    finally:
        get_settings.cache_clear()


def test_lookup_does_not_contact_postgres_when_flag_is_off(monkeypatch):
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "false")
    get_settings.cache_clear()
    called = False

    def _connect():
        nonlocal called
        called = True
        raise AssertionError("disabled semantic cache must not contact Postgres")

    monkeypatch.setattr(semantic_cache, "_connect", _connect)
    try:
        assert semantic_cache.lookup(_scope(), [0.1, 0.2], max_cosine_distance=0.12) is None
        assert called is False
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "scope",
    [
        _scope(tenant_id=""),
        _scope(provider=""),
        _scope(model=""),
        _scope(embedding_model=""),
        _scope(embedding_dimensions=0),
        _scope(embedding_version=""),
        _scope(quality_version=""),
        _scope(request_parameters_hash=""),
    ],
)
def test_lookup_rejects_filterless_scope_without_contacting_postgres(monkeypatch, scope):
    """AC-PC3: a call missing any mandatory filter is refused, not a miss."""
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    called = False

    def _connect():
        nonlocal called
        called = True
        raise AssertionError("incomplete semantic scope must not query Postgres")

    monkeypatch.setattr(semantic_cache, "_connect", _connect)
    try:
        with pytest.raises(ValueError, match="mandatory semantic lookup filters"):
            semantic_cache.lookup(scope, [0.1, 0.2], max_cosine_distance=0.12)
        assert called is False
    finally:
        get_settings.cache_clear()


def test_lookup_binds_every_tenant_provider_model_and_parameter_filter(monkeypatch):
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    conn = _Connection((42, "response-v7", 0.08))
    monkeypatch.setattr(semantic_cache, "_connect", lambda: conn)
    scope = _scope()

    try:
        hit = semantic_cache.lookup(scope, [0.1, 0.2], max_cosine_distance=0.12)
    finally:
        get_settings.cache_clear()

    assert hit == semantic_cache.SemanticCacheHit(
        entry_id=42, response_ref="response-v7", cosine_distance=0.08
    )
    assert len(conn.calls) == 3
    sql, params = conn.calls[2]
    normalized = " ".join(sql.split())
    for required in (
        "tenant_id = %s",
        "provider_id = (SELECT id FROM providers WHERE name = %s)",
        "model = %s",
        "embedding_model = %s",
        "embedding_dimensions = %s",
        "embedding_version = %s",
        "quality_version = %s",
        "request_parameters_hash = %s",
        "expires_at > now()",
    ):
        assert required in normalized
    assert "embedding <=> %s::vector" in normalized
    assert "cosine_distance <= %s" not in normalized
    assert params == (
        "[0.1,0.2]",
        scope.tenant_id,
        scope.provider,
        scope.model,
        scope.embedding_model,
        scope.embedding_dimensions,
        scope.embedding_version,
        scope.quality_version,
        scope.request_parameters_hash,
        "[0.1,0.2]",
    )


def test_lookup_pins_hnsw_and_custom_plan_for_its_transaction(monkeypatch):
    """AC-PC3: every lookup must defeat the default/generic Seq Scan plan."""
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_CACHE_HNSW_EF_SEARCH", "100")
    get_settings.cache_clear()
    conn = _Connection((42, "response-v7", 0.08))
    monkeypatch.setattr(semantic_cache, "_connect", lambda: conn)

    try:
        assert semantic_cache.lookup(_scope(), [0.1, 0.2], max_cosine_distance=0.12)
    finally:
        get_settings.cache_clear()

    assert conn.calls[0] == (
        "SELECT set_config('hnsw.ef_search', %s, true)", ("100",)
    )
    assert conn.calls[1] == (
        "SELECT set_config('plan_cache_mode', %s, true)", ("force_custom_plan",)
    )


@pytest.mark.parametrize("threshold", [None, -0.01, 2.01])
def test_lookup_requires_a_calibrated_cosine_threshold(monkeypatch, threshold):
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        semantic_cache,
        "_connect",
        lambda: pytest.fail("an uncalibrated threshold must not query Postgres"),
    )
    try:
        assert semantic_cache.lookup(_scope(), [0.1, 0.2], max_cosine_distance=threshold) is None
    finally:
        get_settings.cache_clear()
