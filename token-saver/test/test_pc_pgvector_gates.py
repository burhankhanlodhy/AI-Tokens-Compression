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
  4. Plan pin (AC-PC4) — at traffic-shaped volume the lookup query must be
     served by idx_semantic_cache_embedding_hnsw, never a Seq Scan.  The
     behavior gates above assert results, not plans: a 216x Seq Scan
     regression would pass them silently.  The gate drives the real lookup()
     path and EXPLAINs the exact statement it ran, in the session state that
     lookup itself pinned (ef_search + force_custom_plan), so removing the
     pin, the index, or the cosine opclass fails here.
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
    """Querying with the analytically exact distance as the threshold must hit
    (the seam filters cosine_distance <= max_cosine_distance)."""
    case = CORPUS["cases"][1]  # near-hit-a
    u, w, expected_distance = build_case_vectors(case)
    insert_entry(pg_dsn, u, canonical_prompt_hash="pc4-boundary-probe")
    hit = semantic_cache.lookup(
        _scope(), w, max_cosine_distance=expected_distance
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


# --------------------------------------------------------------------------
# gate 4: AC-PC4 plan pin — HNSW, never a Seq Scan, at traffic-shaped volume


# Empirically pinned on pgvector/pgvector:0.8.6-pg16 (probe: 1/50/200 rows
# plan a Seq Scan — planner-correct for tiny tables — and >=1000 rows plan
# the HNSW index under the pinned settings).  10000 gives margin without
# turning the lane into a benchmark.
PLAN_GATE_ROWS = 10000


class _RecordingConnection:
    """Wraps a real psycopg connection so the true lookup() path runs while
    every statement is captured.  Mirrors psycopg's connection context
    manager semantics (close on exit)."""

    def __init__(self, pg: psycopg.Connection):
        self._pg = pg
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *_args) -> None:
        self._pg.close()

    def execute(self, sql, params=None):
        self.calls.append((sql, tuple(params or ())))
        return self._pg.execute(sql, params)


def _seed_plan_volume(dsn: str) -> None:
    """Fill semantic_cache_entries with PLAN_GATE_ROWS same-scope rows so the
    planner's cost model sees a traffic-shaped table, then refresh stats."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            f"""
            INSERT INTO semantic_cache_entries (
                tenant_id, provider_id, model, embedding_model, embedding_dimensions,
                embedding_version, quality_version, request_parameters_hash,
                canonical_prompt_hash, embedding, response_ref, expires_at
            )
            SELECT %s, (SELECT id FROM providers WHERE name = %s), %s, %s, %s,
                   %s, %s, %s, 'pc4-plan-vol-' || g,
                   ('[' || array_to_string(
                       array(SELECT round(random()::numeric, 4)::float8
                             FROM generate_series(1, %s)), ',') || ']')::vector,
                   'response-plan-vol', now() + interval '1 hour'
              FROM generate_series(1, %s) g
            """,
            (
                TENANT_A, _scope().provider, _scope().model, _scope().embedding_model,
                DIMS, _scope().embedding_version, _scope().quality_version,
                _scope().request_parameters_hash, DIMS, PLAN_GATE_ROWS,
            ),
        )
        pg.execute("ANALYZE semantic_cache_entries")


def test_lookup_plans_the_hnsw_index_not_a_seq_scan(pg_dsn, monkeypatch):
    """AC-PC4: the statement lookup() actually runs — in the session state
    lookup() itself pinned — must be served by
    idx_semantic_cache_embedding_hnsw.  A Seq Scan here is the latency
    regression the behavior gates cannot see.

    The assertion is about the PLAN only: HNSW is approximate and this gate
    deliberately does not require the seeded exact-match row to be *found*
    (probe result 2026-09-19: at 5001 random 1536-dim vectors the pinned
    lookup misses a distance-0.0 row even at ef_search=300 — a recall
    question for AC-PC4 calibration, tracked separately)."""
    embedding = _uniform_unit("pc4-plan-pin")
    entry_id = insert_entry(pg_dsn, embedding)
    _seed_plan_volume(pg_dsn)

    # Scenario reality check without trusting the HNSW graph: a forced Seq
    # Scan must compute distance ~0.0 for the exact-match row.
    with psycopg.connect(pg_dsn) as pg:
        pg.execute("SET enable_indexscan TO off; SET enable_indexonlyscan TO off")
        row = pg.execute(
            "SELECT embedding <=> %s::vector FROM semantic_cache_entries WHERE id = %s",
            (_vec(embedding), entry_id),
        ).fetchone()
        assert row is not None and abs(row[0]) < 1e-9, (
            "seeded exact-match row does not reproduce distance 0.0 under Seq Scan"
        )

    recorded: _RecordingConnection | None = None

    def _connect():
        nonlocal recorded
        recorded = _RecordingConnection(psycopg.connect(pg_dsn))
        return recorded

    monkeypatch.setattr(semantic_cache, "_connect", _connect)
    semantic_cache.lookup(_scope(), embedding, max_cosine_distance=PROVISIONAL_THRESHOLD)

    assert recorded is not None
    lookup_sql, lookup_params = recorded.calls[-1]
    assert "semantic_cache_entries" in lookup_sql

    # EXPLAIN in a fresh session restored to the exact state lookup() pinned,
    # replayed from the captured statements themselves.
    with psycopg.connect(pg_dsn) as pg:
        for sql, params in recorded.calls[:-1]:
            pg.execute(sql, params)
        assert (
            pg.execute("SHOW hnsw.ef_search").fetchone()[0]
            == str(get_settings().semantic_cache_hnsw_ef_search)
        ), "lookup() must pin hnsw.ef_search on its session for this plan to be meaningful"
        plan_rows = pg.execute("EXPLAIN " + lookup_sql, lookup_params).fetchall()
    plan = "\n".join(row[0] for row in plan_rows)

    assert "idx_semantic_cache_embedding_hnsw" in plan, (
        "lookup() query plan does not use the HNSW index:\n" + plan
    )
    assert "Seq Scan on semantic_cache_entries" not in plan, (
        "lookup() query plan Seq-Scans semantic_cache_entries at "
        f"{PLAN_GATE_ROWS} rows:\n" + plan
    )
