"""AC-PC3 pgvector semantic-cache lookup seam.

This module owns only the safe read boundary.  Embedding production, the
pgvector image/table migration, calibration corpus, and enablement remain
separate owner/gate responsibilities.  In particular, there is no fallback
threshold: callers must supply the value calibrated by AC-PC4.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

import psycopg

from .config import get_settings
from .db import get_pg_dsn


@dataclass(frozen=True)
class SemanticLookupScope:
    """Compatibility boundary that every semantic lookup must carry."""

    tenant_id: str
    provider: str
    model: str
    embedding_model: str
    embedding_dimensions: int
    embedding_version: str
    quality_version: str
    request_parameters_hash: str

    def complete(self) -> bool:
        return (
            all((
                self.tenant_id.strip(),
                self.provider.strip(),
                self.model.strip(),
                self.embedding_model.strip(),
                self.embedding_version.strip(),
                self.quality_version.strip(),
                self.request_parameters_hash.strip(),
            ))
            and self.embedding_dimensions > 0
        )


@dataclass(frozen=True)
class SemanticCacheHit:
    entry_id: int
    response_ref: str
    cosine_distance: float


def _connect() -> psycopg.Connection:
    return psycopg.connect(get_pg_dsn())


def _vector_literal(embedding: Sequence[float], expected_dimensions: int) -> str | None:
    """Return pgvector text only for a finite, dimension-compatible vector."""
    if len(embedding) != expected_dimensions:
        return None
    try:
        values = [float(value) for value in embedding]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return "[" + ",".join(str(value) for value in values) + "]"


def _valid_threshold(value: float | None) -> bool:
    # Cosine distance is bounded [0, 2].  The product contract deliberately
    # supplies the actual value from its calibration artifact; this only
    # rejects non-values/out-of-domain input, it does not pick a threshold.
    return value is not None and 0.0 <= value <= 2.0 and math.isfinite(value)


def lookup(
    scope: SemanticLookupScope,
    embedding: Sequence[float],
    *,
    max_cosine_distance: float | None,
) -> SemanticCacheHit | None:
    """Find one compatible unexpired pgvector candidate, or return no hit.

    A disabled deployment is a deliberate cache miss without a database
    query. A filter-less enabled lookup is a caller error: it is refused
    before opening a connection rather than being widened into an unsafe
    query. Invalid embeddings and absent calibrated thresholds remain cache
    misses. The WHERE clause repeats every mandatory isolation/compatibility
    filter before HNSW ordering; no filter may be optional or client-derived.
    """
    settings = get_settings()
    if not settings.semantic_cache_enabled:
        return None
    if not scope.complete():
        raise ValueError("mandatory semantic lookup filters are required")
    if not _valid_threshold(max_cosine_distance):
        return None
    vector = _vector_literal(embedding, scope.embedding_dimensions)
    if vector is None:
        return None

    try:
        with _connect() as pg:
            # Default ef_search=40 and generic prepared plans both selected a
            # Seq Scan at traffic-shaped volume. These are transaction-local
            # so the policy also holds if lookup later uses a connection pool.
            pg.execute(
                "SELECT set_config('hnsw.ef_search', %s, true)",
                (str(settings.semantic_cache_hnsw_ef_search),),
            )
            pg.execute(
                "SELECT set_config('plan_cache_mode', %s, true)",
                ("force_custom_plan",),
            )
            row = pg.execute(
                """
                WITH nearest AS (
                    SELECT id, response_ref, embedding <=> %s::vector AS cosine_distance
                      FROM semantic_cache_entries
                     WHERE tenant_id = %s
                       AND provider_id = (SELECT id FROM providers WHERE name = %s)
                       AND model = %s
                       AND embedding_model = %s
                       AND embedding_dimensions = %s
                       AND embedding_version = %s
                       AND quality_version = %s
                       AND request_parameters_hash = %s
                       AND expires_at > now()
                     ORDER BY embedding <=> %s::vector
                     LIMIT 1
                )
                SELECT id, response_ref, cosine_distance
                  FROM nearest
                 WHERE cosine_distance <= %s
                """,
                (
                    vector,
                    scope.tenant_id,
                    scope.provider,
                    scope.model,
                    scope.embedding_model,
                    scope.embedding_dimensions,
                    scope.embedding_version,
                    scope.quality_version,
                    scope.request_parameters_hash,
                    vector,
                    max_cosine_distance,
                ),
            ).fetchone()
    except (psycopg.Error, RuntimeError):
        # Semantic caching is an optional optimization.  A missing migration or
        # temporary database failure cannot degrade the proxied request path.
        return None

    if row is None:
        return None
    return SemanticCacheHit(
        entry_id=int(row[0]), response_ref=str(row[1]), cosine_distance=float(row[2])
    )
