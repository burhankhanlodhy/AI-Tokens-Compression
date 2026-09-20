"""AC-PC1/PC2/PC4 pgvector-lane gates: the only automated tests that execute
the semantic_cache_entries DDL end-to-end.

CI runs this file EXCLUSIVELY on the pinned pgvector/pgvector:0.8.6-pg16 lane
(the stock postgres:16 job passes --ignore for it, and it is likewise excluded
from the reverse-order job): the base schema applies anywhere, but the
semantic-cache migration needs the vector extension, and a skip is a floor
violation in the standard suites.

Gates owned here:
  1. Cross-tenant/isolation probe — a lookup carrying tenant B's scope can
     never return tenant A's row, on the live HNSW query path, and every
     mandatory compatibility filter actually filters (not just parses).
  2. Committed calibration corpus (test/data/pc4_calibration_corpus.json) —
     deterministic seeds with expected hit AND miss classes; pgvector's
     computed <=> distance must match the analytic value and the committed
     threshold must classify every case exactly.  The threshold in the corpus
     gates the filter mechanics only; the production value is AC-PC4 scope.
  3. Deterministic invalidation — expiry is the invalidation primitive and it
     must be exact: future expiry hits, past expiry misses, the boundary is
     strict (> now(), not >=), supersession is blocked by the identity unique
     index, and delete+reinsert deterministically retargets the hit.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import get_settings
from proxy import semantic_cache
from pg_test_support import TENANT_A, TENANT_B, make_pgvector_database, unique_db_name

CORPUS = json.loads(
    (Path(__file__).resolve().parent / "data" / "pc4_calibration_corpus.json").read_text()
)
DIMS = CORPUS["embedding_dimensions"]
PROVISIONAL_THRESHOLD = CORPUS["provisional_max_cosine_distance"]

DB_NAME = unique_db_name("pc_pgvector_gates")


@pytest.fixture(scope="module")
def pg_dsn():
    dsn = make_pgvector_database(DB_NAME)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'tenant-a'), (%s, 'tenant-b') "
            "ON CONFLICT (id) DO NOTHING",
            (TENANT_A, TENANT_B),
        )
    yield dsn
    from pg_test_support import drop_database

    drop_database(DB_NAME)


@pytest.fixture(autouse=True)
def _lookup_env(monkeypatch, pg_dsn):
    """Point the seam at the isolated pgvector database with caching enabled."""
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", pg_dsn)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# deterministic vector construction (mirrors the corpus spec comment)


def _uniform_unit(seed: str) -> list[float]:
    """SHA-256 counter stream -> uniform [-1, 1]^DIMS, normalized to unit length."""
    raw: list[float] = []
    counter = 0
    while len(raw) < DIMS:
        digest = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        for (a, b) in zip(digest[0::2], digest[1::2]):
            raw.append((a / 255.0) * 2.0 - 1.0)
            raw.append((b / 255.0) * 2.0 - 1.0)
        counter += 1
    raw = raw[:DIMS]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


def build_case_vectors(case: dict) -> tuple[list[float], list[float], float]:
    """Return (stored embedding u, queried embedding w, expected cosine distance).

    Distance is computed analytically from the actual vectors (1 - u·w), so the
    gate checks pgvector against ground truth, not against the corpus' target.
    """
    u = _uniform_unit(case["seed"])
    if case["kind"] == "identical":
        return u, list(u), 0.0
    p = _uniform_unit(case["near_seed"])
    if case["kind"] == "near":
        alpha = math.acos(1.0 - case["target_distance"])  # cos distance = 1 - cos_sim
        w = [u[i] * math.cos(alpha) + p[i] * math.sin(alpha) for i in range(DIMS)]
        # renormalize away the tiny u·p cross-term drift so the analytic
        # distance below is measured on exactly the vectors we store
        wnorm = math.sqrt(sum(v * v for v in w))
        w = [v / wnorm for v in w]
    elif case["kind"] == "independent":
        w = p
    else:  # pragma: no cover - corpus kinds are fixed
        raise AssertionError(f"unknown corpus kind {case['kind']!r}")
    expected_distance = 1.0 - sum(u[i] * w[i] for i in range(DIMS))
    return u, w, expected_distance


def _vec(values: list[float]) -> str:
    return "[" + ",".join(repr(v) for v in values) + "]"


def _scope(tenant_id: str = TENANT_A, **overrides) -> semantic_cache.SemanticLookupScope:
    values = {
        "tenant_id": tenant_id,
        "provider": "openai",  # seeded by pg_test_support.make_database
        "model": "google/gemini-3.5-flash-lite",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": DIMS,
        "embedding_version": "2026-09-18",
        "quality_version": "pc4-gate-v1",
        "request_parameters_hash": "pc4gate01",
    }
    values.update(overrides)
    return semantic_cache.SemanticLookupScope(**values)


def insert_entry(
    dsn: str,
    embedding: list[float],
    *,
    scope: semantic_cache.SemanticLookupScope | None = None,
    canonical_prompt_hash: str | None = None,
    response_ref: str = "response-pc4",
    expires_sql: str = "now() + interval '1 hour'",
) -> int:
    scope = scope or _scope()
    if canonical_prompt_hash is None:
        # the identity unique index is real: never let two inserts collide by
        # accident — a deliberate collision is a test's explicit choice
        canonical_prompt_hash = "pc4-" + uuid4().hex
    with psycopg.connect(dsn) as pg:
        row = pg.execute(
            f"""
            INSERT INTO semantic_cache_entries (
                tenant_id, provider_id, model, embedding_model, embedding_dimensions,
                embedding_version, quality_version, request_parameters_hash,
                canonical_prompt_hash, embedding, response_ref, expires_at
            )
            SELECT %s, id, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, {expires_sql}
              FROM providers WHERE name = %s
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
                _vec(embedding),
                response_ref,
                scope.provider,
            ),
        ).fetchone()
    assert row is not None, "entry insert matched no provider row"
    return int(row[0])


def lookup(scope, embedding, threshold=PROVISIONAL_THRESHOLD):
    return semantic_cache.lookup(scope, embedding, max_cosine_distance=threshold)


# --------------------------------------------------------------------------
# gate 1: cross-tenant / isolation probe on the live query path


def test_lookup_hits_on_exact_scope_over_real_hnsw(pg_dsn):
    v = _uniform_unit("pc4-exact-hit")
    entry_id = insert_entry(pg_dsn, v)
    hit = lookup(_scope(), v)
    assert hit is not None
    assert hit.entry_id == entry_id
    assert hit.response_ref == "response-pc4"
    assert hit.cosine_distance == pytest.approx(0.0, abs=1e-9)


def test_lookup_never_returns_another_tenants_row(pg_dsn):
    """The cross-tenant probe: identical vector + identical everything except
    tenant_id must miss, not order across the boundary."""
    v = _uniform_unit("pc4-xtenant")
    insert_entry(pg_dsn, v)
    probe = _scope(tenant_id=TENANT_B)
    assert lookup(probe, v) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "other/model"),
        ("embedding_version", "1999-01-01"),
        ("quality_version", "stale-quality"),
        ("request_parameters_hash", "different-params"),
    ],
)
def test_lookup_rejects_incompatible_scope_dimensions(pg_dsn, field, value):
    """Every mandatory compatibility filter filters on the live path; a
    mismatch in exactly one dimension is a miss, not a near-hit."""
    base = _scope()
    v = _uniform_unit(f"pc4-incompat-{field}")
    entry_id = insert_entry(pg_dsn, v)
    assert lookup(_scope(**{field: value}), v) is None
    # sanity: the unmodified scope still hits the same row
    hit = lookup(base, v)
    assert hit is not None and hit.entry_id == entry_id


def test_lookup_misses_for_unregistered_provider_without_raising(pg_dsn):
    v = _uniform_unit("pc4-unregistered")
    insert_entry(pg_dsn, v)
    probe = _scope(provider="no-such-provider")
    assert lookup(probe, v) is None


def test_lookup_rejects_dimension_mismatched_embedding_without_query_error(pg_dsn):
    insert_entry(pg_dsn, _uniform_unit("pc4-dim-mismatch"))
    assert lookup(_scope(), [0.3, 0.4]) is None


# --------------------------------------------------------------------------
# gate 2: committed calibration corpus — false hits and misses


def test_corpus_threshold_separates_hits_from_misses(pg_dsn):
    """Every corpus case is inserted for tenant A and probed with its partner
    vector; the committed threshold must classify hits and misses EXACTLY as
    the corpus expects — a false hit or a false miss fails the gate."""
    for case in CORPUS["cases"]:
        u, w, expected_distance = build_case_vectors(case)
        insert_entry(
            pg_dsn, u, canonical_prompt_hash=f"pc4-corpus-{case['id']}",
            response_ref=f"response-{case['id']}",
        )
        hit = lookup(_scope(), w)
        if case["expected"] == "hit":
            assert hit is not None, f"corpus case {case['id']} falsely missed"
            assert hit.response_ref == f"response-{case['id']}"
            assert abs(hit.cosine_distance - expected_distance) < 1e-6, (
                f"pgvector <=> distance for {case['id']} diverged from analytic value"
            )
        else:
            assert hit is None, (
                f"corpus case {case['id']} falsely hit at distance "
                f"{expected_distance:.4f} (threshold {PROVISIONAL_THRESHOLD})"
            )


def test_threshold_boundary_is_inclusive_at_exact_distance(pg_dsn):
    """The exact pgvector distance must hit (the seam filters with <=).

    Vectors are stored as float32 by pgvector, whereas ``expected_distance``
    is calculated from Python float64 components.  The analytic value can lie
    on either side of pgvector's real distance across CPU/libm builds, so it
    cannot represent the database boundary under test.
    """
    case = CORPUS["cases"][1]  # near-hit-a
    u, w, _expected_distance = build_case_vectors(case)
    entry_id = insert_entry(pg_dsn, u, canonical_prompt_hash="pc4-boundary-probe")
    vector = "[" + ",".join(str(value) for value in w) + "]"
    with psycopg.connect(pg_dsn) as pg:
        exact_pgvector_distance = float(
            pg.execute(
                "SELECT embedding <=> %s::vector FROM semantic_cache_entries WHERE id = %s",
                (vector, entry_id),
            ).fetchone()[0]
        )
    hit = semantic_cache.lookup(
        _scope(), w, max_cosine_distance=exact_pgvector_distance
    )
    assert hit is not None, "inclusive <= boundary at the exact distance was refused"


def test_corpus_cases_straddle_the_committed_threshold():
    """The corpus must actually straddle the threshold — a corpus that is all
    hits or all misses gates nothing."""
    distances = []
    for case in CORPUS["cases"]:
        _u, _w, expected_distance = build_case_vectors(case)
        distances.append((case["expected"], expected_distance))
    hits = [d for exp, d in distances if exp == "hit"]
    misses = [d for exp, d in distances if exp == "miss"]
    assert hits and max(hits) <= PROVISIONAL_THRESHOLD
    assert misses and min(misses) > PROVISIONAL_THRESHOLD
    assert min(misses) - max(hits) > 0.05, "hit/miss bands must not approach the threshold"


# --------------------------------------------------------------------------
# gate 3: deterministic invalidation


def test_expiry_invalidation_is_exact_and_boundary_strict(pg_dsn):
    embedding = _uniform_unit("pc4-expiry")
    entry = insert_entry(pg_dsn, embedding)
    assert lookup(_scope(), embedding).entry_id == entry

    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute(
            "UPDATE semantic_cache_entries SET expires_at = now() WHERE id = %s",
            (entry,),
        )
    assert lookup(_scope(), embedding) is None, "expires_at = now() must miss (strict >)"

    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute(
            "UPDATE semantic_cache_entries SET expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (entry,),
        )
    assert lookup(_scope(), embedding) is None, "expired entry must never hit"


def test_identity_unique_index_blocks_supersession_in_place(pg_dsn):
    """The mandatory-scope identity unique index must reject a second write for
    the same identity — invalidation happens by delete+reinsert, never by
    silently overwriting a cached response."""
    embedding = _uniform_unit("pc4-supersede")
    insert_entry(pg_dsn, embedding, canonical_prompt_hash="pc4-collision")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_entry(
            pg_dsn,
            embedding,
            canonical_prompt_hash="pc4-collision",
            response_ref="response-overwrite",
        )


def test_delete_then_reinsert_deterministically_retargets_the_hit(pg_dsn):
    embedding = _uniform_unit("pc4-retarget")
    prompt_hash = "pc4-retarget-hash"
    stale = insert_entry(
        pg_dsn, embedding, canonical_prompt_hash=prompt_hash, response_ref="response-stale"
    )
    assert lookup(_scope(), embedding).entry_id == stale

    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute("DELETE FROM semantic_cache_entries WHERE id = %s", (stale,))
    assert lookup(_scope(), embedding) is None, "deleted entry must be invalidated immediately"

    fresh = insert_entry(
        pg_dsn, embedding, canonical_prompt_hash=prompt_hash, response_ref="response-fresh"
    )
    hit = lookup(_scope(), embedding)
    assert hit is not None and hit.entry_id == fresh
    assert hit.response_ref == "response-fresh"
    assert hit.entry_id != stale
