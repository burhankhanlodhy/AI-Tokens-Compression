"""AC-PC3 pgvector semantic-cache lookup seam.

This module owns only the safe read boundary.  Embedding production, the
pgvector image/table migration, calibration corpus, and enablement remain
separate owner/gate responsibilities.  In particular, there is no fallback
threshold: callers must supply the value calibrated by AC-PC4.

PM rulings (ratified 2026-09-19 for the PC5 vertical slice; do not re-litigate):
- embedding_version = "{relay}:{model}@{dims}", derived once at process start
  from the effective embedding config; default "openai:text-embedding-3-small@1536".
  Bump on any input-side change that can shift the vector for identical text:
  relay/upstream class that produced the vectors, model identifier, dimensions,
  or the text-preparation pipeline (template/prefix, truncation, future
  chunking). Query-time knobs (ef_search, threshold, index parameters) never
  change vectors and never bump.
- quality_version = the app release semver at write time (version()).
  Bump on any release whose diff touches response-shaping behavior: compression
  engine, token counting, conciseness/grounded gates (P6, AC-P6c tiers),
  response templates. Bias to bump: over-quarantine costs a cold cache;
  under-quarantine serves bytes the current pipeline would not produce.
- Both columns are cache namespaces: lookup filters WHERE <col> = current and
  the unique identity index includes both, so a bump silently quarantines old
  entries until expires_at / the re-insert sweep, and a config or release
  rollback restores their readability without a migration. Guardrail tests:
  same text + bumped version -> clean miss; rollback -> old version readable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Any, Sequence
from uuid import uuid4

import psycopg

from .config import get_settings
from .db import get_pg_dsn
from .version import __version__


DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"


class SemanticLookupKind(str, Enum):
    HIT = "hit"
    THRESHOLD_MISS = "threshold_miss"
    NO_COMPATIBLE_ROW = "no_compatible_row"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class SemanticLookupResult:
    """Structured outcome used by the request path and KPI attribution."""

    kind: SemanticLookupKind
    hit: "SemanticCacheHit | None" = None
    response: "SemanticCacheResponse | None" = None

    @classmethod
    def hit_result(
        cls, hit: "SemanticCacheHit", response: "SemanticCacheResponse | None" = None
    ) -> "SemanticLookupResult":
        return cls(SemanticLookupKind.HIT, hit, response)

    @classmethod
    def threshold_miss(cls) -> "SemanticLookupResult":
        return cls(SemanticLookupKind.THRESHOLD_MISS)

    @classmethod
    def no_compatible_row(cls) -> "SemanticLookupResult":
        return cls(SemanticLookupKind.NO_COMPATIBLE_ROW)

    @classmethod
    def not_attempted(cls) -> "SemanticLookupResult":
        return cls(SemanticLookupKind.NOT_ATTEMPTED)


@dataclass(frozen=True)
class SemanticCacheResponse:
    body: bytes
    sha256: str
    content_length: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_request(body: dict[str, Any]) -> str:
    """Return the stable semantic-cache input representation.

    Version fields are never accepted from a request body.  The request path
    supplies them from process configuration when building the lookup scope.
    """
    clean = {
        key: value
        for key, value in body.items()
        if key in {"model", "messages", "tools"}
    }
    return _canonical_json(clean)


def canonical_prompt_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_request(body).encode("utf-8")).hexdigest()


def request_parameters_hash(body: dict[str, Any]) -> str:
    """Hash model parameters separately from the canonical prompt text."""
    params = {
        key: value
        for key, value in body.items()
        if key not in {"messages", "model", "embedding_version", "quality_version", "tenant_id"}
    }
    return hashlib.sha256(_canonical_json(params).encode("utf-8")).hexdigest()


def derive_embedding_version() -> str:
    settings = get_settings()
    relay = settings.embedding_relay.strip()
    model = settings.embedding_model.strip()
    dimensions = settings.embedding_dimensions
    if not relay or not model or dimensions != 1536:
        raise ValueError("semantic embeddings require a non-empty relay/model and 1536 dimensions")
    return f"{relay}:{model}@{dimensions}"


def derive_quality_version() -> str:
    if not __version__.strip():
        raise ValueError("application quality version is empty")
    return __version__


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


def _candidate(
    scope: SemanticLookupScope,
    embedding: Sequence[float],
) -> tuple[int, str, float] | None:
    """Return the nearest compatible candidate without applying a threshold."""
    settings = get_settings()
    vector = _vector_literal(embedding, scope.embedding_dimensions)
    if vector is None:
        return None
    with _connect() as pg:
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
            ),
        ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), float(row[2])


def _cleanup_candidate(scope: SemanticLookupScope, entry_id: int, response_ref: str) -> None:
    """Best-effort lazy cleanup; cleanup errors remain cache misses."""
    try:
        with _connect() as pg:
            with pg.transaction():
                pg.execute(
                    "DELETE FROM semantic_cache_entries WHERE id = %s AND tenant_id = %s",
                    (entry_id, scope.tenant_id),
                )
                pg.execute(
                    "DELETE FROM semantic_cache_responses WHERE tenant_id = %s AND response_ref = %s",
                    (scope.tenant_id, response_ref),
                )
    except (psycopg.Error, RuntimeError):
        return


def _read_response(scope: SemanticLookupScope, entry_id: int, response_ref: str) -> SemanticCacheResponse | None:
    """Read and verify the tenant-scoped exact bytes behind a hit."""
    try:
        with _connect() as pg:
            row = pg.execute(
                """
                SELECT payload_bytes, sha256, content_length
                  FROM semantic_cache_responses
                 WHERE tenant_id = %s
                   AND response_ref = %s
                   AND expires_at > now()
                """,
                (scope.tenant_id, response_ref),
            ).fetchone()
            if row is None:
                _cleanup_candidate(scope, entry_id, response_ref)
                return None
            body = bytes(row[0])
            sha256 = str(row[1])
            content_length = int(row[2])
            if (
                len(body) != content_length
                or hashlib.sha256(body).hexdigest() != sha256
            ):
                _cleanup_candidate(scope, entry_id, response_ref)
                return None
            return SemanticCacheResponse(body, sha256, content_length)
    except (psycopg.Error, RuntimeError, TypeError, ValueError):
        _cleanup_candidate(scope, entry_id, response_ref)
        return None


def lookup_result(
    scope: SemanticLookupScope,
    embedding: Sequence[float],
    *,
    max_cosine_distance: float | None,
) -> SemanticLookupResult:
    """Classify the nearest compatible row before applying the threshold.

    ``lookup`` remains the legacy hit-only seam for the AC-PC3 gates; the
    request path uses this structured method so threshold pressure and a true
    empty compatibility window remain distinct ledger outcomes.
    """
    settings = get_settings()
    if not settings.semantic_cache_enabled:
        return SemanticLookupResult.not_attempted()
    if not scope.complete():
        raise ValueError("mandatory semantic lookup filters are required")
    if scope.embedding_dimensions != 1536:
        return SemanticLookupResult.not_attempted()
    if not _valid_threshold(max_cosine_distance):
        return SemanticLookupResult.not_attempted()
    if _vector_literal(embedding, scope.embedding_dimensions) is None:
        return SemanticLookupResult.not_attempted()
    try:
        candidate = _candidate(scope, embedding)
    except (psycopg.Error, RuntimeError):
        return SemanticLookupResult.not_attempted()
    if candidate is None:
        return SemanticLookupResult.no_compatible_row()
    threshold = max_cosine_distance if max_cosine_distance is not None else -1.0
    entry_id, response_ref, distance = candidate
    if distance > threshold:
        return SemanticLookupResult.threshold_miss()
    response = _read_response(scope, entry_id, response_ref)
    if response is None:
        return SemanticLookupResult.not_attempted()
    try:
        with _connect() as pg:
            pg.execute(
                "UPDATE semantic_cache_entries SET hit_count = hit_count + 1, last_hit_at = now() WHERE id = %s AND tenant_id = %s",
                (entry_id, scope.tenant_id),
            )
    except (psycopg.Error, RuntimeError):
        # A hit remains safe to serve when metadata accounting is unavailable.
        pass
    return SemanticLookupResult.hit_result(
        SemanticCacheHit(entry_id, response_ref, distance), response
    )


def read_response(scope: SemanticLookupScope, hit: SemanticCacheHit) -> SemanticCacheResponse | None:
    """Public integrity-checked replay seam for a previously classified hit."""
    return _read_response(scope, hit.entry_id, hit.response_ref)


# Explicit aliases make the structured seam discoverable without changing the
# legacy hit-only ``lookup`` contract used by the PC1/PC4 gate fixtures.
structured_lookup = lookup_result
semantic_lookup = lookup_result


def store_response(
    scope: SemanticLookupScope,
    canonical_prompt_hash: str,
    embedding: Sequence[float],
    response_body: bytes,
    *,
    ttl_seconds: int | None = None,
) -> str | None:
    """Atomically store one semantic entry plus its verbatim provider response.

    Semantic caching is an optional optimization: any malformed cache candidate,
    database failure, or unavailable migration is a clean miss.  The response
    bytes are kept separately from their JSONB projection because Postgres JSONB
    deliberately normalizes the representation needed for exact replay.
    """
    settings = get_settings()
    if (
        not settings.semantic_cache_enabled
        or not scope.complete()
        or scope.embedding_dimensions != 1536
    ):
        return None
    if not isinstance(response_body, bytes) or len(response_body) > settings.semantic_cache_max_response_bytes:
        return None
    if not isinstance(canonical_prompt_hash, str) or not canonical_prompt_hash.strip():
        return None
    vector = _vector_literal(embedding, scope.embedding_dimensions)
    if vector is None:
        return None
    if ttl_seconds is None:
        ttl_seconds = settings.semantic_cache_ttl_seconds
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        return None
    try:
        payload = json.loads(response_body)
        payload_json = json.dumps(payload, ensure_ascii=False)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    response_ref = f"semantic-{uuid4().hex}"
    try:
        with _connect() as pg:
            # Separate statements are intentional: a data-modifying CTE shares
            # one statement snapshot, so its INSERT cannot see its own DELETE
            # for the entry identity unique index. The explicit transaction
            # preserves atomic delete-then-reinsert semantics across both tables.
            with pg.transaction():
                old_pairs = pg.execute(
                    """
                    DELETE FROM semantic_cache_entries
                     WHERE tenant_id = %s
                       AND provider_id = (SELECT id FROM providers WHERE name = %s)
                       AND model = %s
                       AND embedding_model = %s
                       AND embedding_dimensions = %s
                       AND embedding_version = %s
                       AND quality_version = %s
                       AND request_parameters_hash = %s
                       AND canonical_prompt_hash = %s
                    RETURNING tenant_id, response_ref
                    """,
                    (
                        scope.tenant_id,
                        scope.provider,
                        scope.model,
                        scope.embedding_model,
                        scope.embedding_dimensions,
                        scope.embedding_version,
                        scope.quality_version,
                        scope.request_parameters_hash,
                        canonical_prompt_hash,
                    ),
                ).fetchall()
                for tenant_id, old_response_ref in old_pairs:
                    pg.execute(
                        """
                        DELETE FROM semantic_cache_responses
                         WHERE tenant_id = %s AND response_ref = %s
                        """,
                        (tenant_id, old_response_ref),
                    )
                expires_at = pg.execute(
                    "SELECT now() + (%s * interval '1 second')",
                    (ttl_seconds,),
                ).fetchone()[0]
                pg.execute(
                    """
                    INSERT INTO semantic_cache_responses (
                        tenant_id, response_ref, payload, payload_bytes,
                        schema_version, sha256, content_length, expires_at
                    )
                    VALUES (%s, %s, %s::jsonb, %s, 1, %s, %s, %s)
                    """,
                    (
                        scope.tenant_id,
                        response_ref,
                        payload_json,
                        psycopg.Binary(response_body),
                        hashlib.sha256(response_body).hexdigest(),
                        len(response_body),
                        expires_at,
                    ),
                )
                entry = pg.execute(
                    """
                    INSERT INTO semantic_cache_entries (
                        tenant_id, provider_id, model, embedding_model, embedding_dimensions,
                        embedding_version, quality_version, request_parameters_hash,
                        canonical_prompt_hash, embedding, response_ref, expires_at
                    )
                    SELECT %s, id, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s
                      FROM providers
                     WHERE name = %s
                    RETURNING id
                    """,
                    (
                        scope.tenant_id,
                        scope.model,
                        scope.embedding_model,
                        scope.embedding_dimensions,
                        scope.embedding_version,
                        scope.quality_version,
                        scope.request_parameters_hash,
                        canonical_prompt_hash,
                        vector,
                        response_ref,
                        expires_at,
                        scope.provider,
                    ),
                ).fetchone()
                if entry is None:
                    raise RuntimeError("semantic cache provider is unavailable")
    except (psycopg.Error, RuntimeError):
        return None
    return response_ref
