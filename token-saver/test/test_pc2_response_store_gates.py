"""AC-PC2/PC3 response-store gates: the only automated tests that execute the
20260919_pc2_semantic_responses.sql DDL end-to-end and bind the entry+response
writer contract.

CI runs this file EXCLUSIVELY on the pinned pgvector/pgvector:0.8.6-pg16 lane
(the stock postgres:16 job passes --ignore for it, and it is likewise excluded
from the reverse-order job), exactly like test_pc_pgvector_gates.py: the
response store's FK targets semantic_cache_entries, which only exists after
the pgvector migration.  A skip is a floor violation; an unapplied or broken
migration surfaces here as a collection/fixture ERROR, never a silent pass.

Gates owned here:
  1. Schema — the committed migration applies, pins the tenant-scoped unique
     ref, and its CHECKs actually bind integrity to the stored BYTES
     (sha256 == digest(payload_bytes), content_length == octet_length,
     payload == payload_bytes::jsonb, schema_version = 1), with the entry FK
     blocking orphans and cascading deliberate purges.
  2. Writer pairing — the writer inserts the entry + response pair in ONE
     transaction with identical tenant_id/response_ref/expires_at, stores the
     exact relayed bytes in payload_bytes, rolls back BOTH rows when either
     insert fails, and supersedes by delete-then-reinsert across both tables.
  3. Replay — a hit resolves to byte-identical payload_bytes with matching
     sha256/content_length; oversized bodies are refused as a miss; the one
     TTL knob governs both rows together.

Writer seam (proposed against the ratified Response store contract,
product-spec-v2.md commit 30db1d7): proxy.semantic_cache.store_response(
scope, canonical_prompt_hash, embedding, response_body: bytes,
*, ttl_seconds: int | None = None) -> str | None.  Returns the response_ref
on success, None on a clean refusal/failure (never raises).
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
from pg_test_support import (
    TENANT_A,
    TENANT_B,
    make_response_store_database,
    unique_db_name,
)

DIMS = 1536  # pinned by AC-PC2: HNSW vector(1536) + CHECK (embedding_dimensions = 1536)

DB_NAME = unique_db_name("pc2_response_store_gates")


@pytest.fixture(scope="module")
def pg_dsn():
    dsn = make_response_store_database(DB_NAME)
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
def _writer_env(monkeypatch, pg_dsn):
    """Point the seam at the isolated response-store database, caching enabled."""
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", pg_dsn)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_pair_tables(pg_dsn):
    """Per-test isolation: the DB is module-scoped for speed, but every gate
    gets an empty pair of tables.  Several writer gates assert GLOBAL emptiness
    (a committed row anywhere is a rollback failure), so they must never see a
    prior test's unexpired rows."""
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute("TRUNCATE semantic_cache_responses, semantic_cache_entries CASCADE")
    yield


# --------------------------------------------------------------------------
# shared helpers


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
        "quality_version": "pc2-gate-v1",
        "request_parameters_hash": "pc2gate01",
    }
    values.update(overrides)
    return semantic_cache.SemanticLookupScope(**values)


def _body(tag: str = "pc2") -> bytes:
    """A realistic relayed body whose exact byte layout matters (spaces, unicode)."""
    return json.dumps(
        {
            "id": f"chatcmpl-{tag}-{uuid4().hex[:8]}",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"héllo  {tag} — verbatim réplay"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        },
        ensure_ascii=False,
        separators=(", ", ": "),
    ).encode("utf-8")


def _insert_response(
    pg_dsn: str,
    response_ref: str,
    *,
    tenant_id: str = TENANT_A,
    payload_bytes: bytes | None = None,
    sha256: str | None = None,
    content_length: int | None = None,
    payload: str | None = None,
    schema_version: int = 1,
) -> None:
    """Insert one response row, defaulting every derived field to a VALID value
    so each test perturbs exactly the dimension under test."""
    if payload_bytes is None:
        payload_bytes = _body(response_ref)
    if sha256 is None:
        sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if content_length is None:
        content_length = len(payload_bytes)
    if payload is None:
        payload = payload_bytes.decode("utf-8")
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute(
            """
            INSERT INTO semantic_cache_responses (
                tenant_id, response_ref, payload, payload_bytes,
                schema_version, sha256, content_length, expires_at
            )
            SELECT %s, %s, %s::jsonb, %s, %s, %s, %s, now() + interval '1 hour'
            """,
            (tenant_id, response_ref, payload, psycopg.Binary(payload_bytes), schema_version, sha256, content_length),
        )


def _insert_entry(pg_dsn: str, response_ref: str, *, tenant_id: str = TENANT_A) -> int:
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        row = pg.execute(
            """
            INSERT INTO semantic_cache_entries (
                tenant_id, provider_id, model, embedding_model, embedding_dimensions,
                embedding_version, quality_version, request_parameters_hash,
                canonical_prompt_hash, embedding, response_ref, expires_at
            )
            SELECT %s, id, 'google/gemini-3.5-flash-lite', 'text-embedding-3-small',
                   %s, '2026-09-18', 'pc2-gate-v1', 'pc2gate01', %s, %s::vector, %s,
                   now() + interval '1 hour'
              FROM providers WHERE name = 'openai'
            RETURNING id
            """,
            (tenant_id, DIMS, "pc2-" + uuid4().hex, _vec(_uniform_unit("pc2-" + response_ref)), response_ref),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _writer_unavailable() -> str:
    return (
        "AC-PC2/PC3 writer not implemented: expected "
        "proxy.semantic_cache.store_response(scope, canonical_prompt_hash, "
        "embedding, response_body: bytes, *, ttl_seconds: int | None = None) "
        "-> str | None per the ratified Response store contract "
        "(product-spec-v2.md, commit 30db1d7)"
    )


def _store(scope, body: bytes, canonical_prompt_hash: str | None = None, **kwargs) -> str | None:
    if not hasattr(semantic_cache, "store_response"):
        pytest.fail(_writer_unavailable())
    if canonical_prompt_hash is None:
        canonical_prompt_hash = "pc2-" + uuid4().hex
    return semantic_cache.store_response(
        scope, canonical_prompt_hash, _uniform_unit("pc2-store"), body, **kwargs
    )


def _response_rows(pg_dsn: str, tenant_id: str = TENANT_A, ref: str | None = None) -> list[dict]:
    sql = "SELECT response_ref, payload_bytes, payload, sha256, content_length, expires_at, schema_version FROM semantic_cache_responses WHERE tenant_id = %s"
    args: tuple = (tenant_id,)
    if ref is not None:
        sql += " AND response_ref = %s"
        args += (ref,)
    with psycopg.connect(pg_dsn) as pg:
        cols = ["response_ref", "payload_bytes", "payload", "sha256", "content_length", "expires_at", "schema_version"]
        return [dict(zip(cols, r)) for r in pg.execute(sql, args).fetchall()]


# --------------------------------------------------------------------------
# gate 1: schema — the committed migration and its enforced integrity


def test_pc2_migration_creates_pinned_response_store(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        columns = {
            r[0]
            for r in pg.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'semantic_cache_responses'"
            ).fetchall()
        }
        assert columns == {
            "id", "tenant_id", "response_ref", "payload", "payload_bytes",
            "schema_version", "sha256", "content_length", "created_at", "expires_at",
        }
        # tenant-scoped unique ref, same shape as the entries table
        unique_refs = pg.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'semantic_cache_responses'::regclass "
            "AND contype = 'u' AND conname = 'uq_semantic_cache_responses_tenant_ref'"
        ).fetchone()[0]
        assert unique_refs == 1
        # entries reference the pair and purge with it
        fk = pg.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_semantic_cache_entry_response'"
        ).fetchone()
        assert fk is not None and fk[0] == "c"  # ON DELETE CASCADE
        # the sha256 CHECK depends on digest(): pgcrypto must be loadable
        assert pg.execute("SELECT count(*) FROM pg_extension WHERE extname = 'pgcrypto'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "case",
    [
        "wrong-sha256",       # valid hex, wrong digest
        "uppercase-sha256",   # fails the ^[0-9a-f]{64}$ pin
        "short-sha256",       # fails the length pin
        "content-length-high",
        "content-length-negative",
        "payload-projection",
        "schema-version-2",
        "empty-response-ref",
    ],
    ids=str,
)
def test_response_store_check_rejects_integrity_violations(pg_dsn, case):
    body = _body(case)
    with pytest.raises(psycopg.errors.CheckViolation):
        if case == "wrong-sha256":
            _insert_response(pg_dsn, case, payload_bytes=body, sha256="0" * 64)
        elif case == "uppercase-sha256":
            _insert_response(pg_dsn, case, payload_bytes=body, sha256=hashlib.sha256(body).hexdigest().upper())
        elif case == "short-sha256":
            _insert_response(pg_dsn, case, payload_bytes=body, sha256="abcd")
        elif case == "content-length-high":
            _insert_response(pg_dsn, case, payload_bytes=body, content_length=len(body) + 1)
        elif case == "content-length-negative":
            _insert_response(pg_dsn, case, payload_bytes=body, content_length=-1)
        elif case == "payload-projection":
            _insert_response(pg_dsn, case, payload_bytes=body, payload=json.dumps({"tampered": True}))
        elif case == "schema-version-2":
            _insert_response(pg_dsn, case, payload_bytes=body, schema_version=2)
        elif case == "empty-response-ref":
            _insert_response(pg_dsn, "", payload_bytes=body)


def test_response_store_checks_rebind_on_update(pg_dsn):
    """The CHECKs guard the stored bytes, not just the insert statement: a
    later byte tamper or length drift must be rejected too."""
    ref = "pc2-update-rebind"
    _insert_response(pg_dsn, ref)
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                "UPDATE semantic_cache_responses SET payload_bytes = payload_bytes || 'x' WHERE response_ref = %s",
                (ref,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                "UPDATE semantic_cache_responses SET content_length = content_length + 1 WHERE response_ref = %s",
                (ref,),
            )


def test_entry_fk_blocks_orphan_response_ref(pg_dsn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_entry(pg_dsn, "pc2-no-such-response")


def test_response_delete_cascades_to_entry(pg_dsn):
    ref = "pc2-cascade"
    _insert_response(pg_dsn, ref)
    entry_id = _insert_entry(pg_dsn, ref)
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute("DELETE FROM semantic_cache_responses WHERE response_ref = %s", (ref,))
        assert pg.execute("SELECT count(*) FROM semantic_cache_entries WHERE id = %s", (entry_id,)).fetchone()[0] == 0


# --------------------------------------------------------------------------
# gate 2: writer pairing — one transaction, identical keys, exact bytes


def test_writer_stores_exact_relayed_bytes_with_pair_metadata(pg_dsn):
    body = _body("pairing")
    ref = _store(_scope(), body)
    assert ref, "writer refused a valid cacheable body"

    rows = _response_rows(pg_dsn, ref=ref)
    assert len(rows) == 1, "response row missing for the returned response_ref"
    row = rows[0]
    assert row["payload_bytes"] == body, "payload_bytes is not the exact relayed body"
    assert row["payload"] == json.loads(body), "payload is not the JSONB projection of the body"
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert row["content_length"] == len(body)
    assert row["schema_version"] == 1

    with psycopg.connect(pg_dsn) as pg:
        entry = pg.execute(
            "SELECT tenant_id, response_ref, expires_at FROM semantic_cache_entries WHERE response_ref = %s",
            (ref,),
        ).fetchone()
    assert entry is not None, "no semantic_cache_entries row paired with the response row"
    assert str(entry[0]) == TENANT_A and entry[1] == ref
    assert entry[2] == row["expires_at"], "entry/response expires_at must be identical (one transaction)"


@pytest.mark.parametrize("table", ["semantic_cache_responses", "semantic_cache_entries"])
def test_writer_rolls_back_both_rows_when_either_insert_fails(pg_dsn, table):
    """Atomicity probe: abort the SECOND half of the pair via a QA trigger and
    require that NEITHER row commits — and that the failure never breaks the
    proxy path (clean miss, no raise out of the writer)."""
    scope = _scope()
    body = _body(f"atomic-{table}")
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        pg.execute(
            f"""
            CREATE OR REPLACE FUNCTION qa_abort_{table.replace('_', '_')}_fn() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'qa-injected abort on {table}'; END;
            $$ LANGUAGE plpgsql
            """
        )
        pg.execute(
            f"DROP TRIGGER IF EXISTS qa_abort_{table} ON {table}"
        )
        pg.execute(
            f"CREATE TRIGGER qa_abort_{table} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION qa_abort_{table}_fn()"
        )
    try:
        try:
            _store(scope, body)
        except Exception as exc:  # noqa: BLE001 - the seam must swallow this
            pytest.fail(f"writer raised on injected failure: {exc!r}")
        assert _response_rows(pg_dsn, ref=None) == [], "response row committed despite the failed pair"
        with psycopg.connect(pg_dsn) as pg:
            assert pg.execute(
                "SELECT count(*) FROM semantic_cache_entries WHERE tenant_id = %s AND model = %s",
                (scope.tenant_id, scope.model),
            ).fetchone()[0] == 0, "entry row committed despite the failed pair"
    finally:
        with psycopg.connect(pg_dsn, autocommit=True) as pg:
            pg.execute(f"DROP TRIGGER IF EXISTS qa_abort_{table} ON {table}")
            pg.execute(f"DROP FUNCTION IF EXISTS qa_abort_{table}_fn()")


def test_writer_replays_body_byte_identically(pg_dsn):
    body = _body("replay")
    ref = _store(_scope(), body)
    assert ref
    hit = semantic_cache.lookup(_scope(), _uniform_unit("pc2-store"), max_cosine_distance=0.12)
    assert hit is not None, "the stored pair must be findable on the live lookup path"
    assert hit.response_ref == ref
    rows = _response_rows(pg_dsn, ref=ref)
    assert rows and rows[0]["payload_bytes"] == body, "replay would not be byte-identical"
    assert rows[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert rows[0]["content_length"] == len(body)
    assert json.loads(bytes(rows[0]["payload_bytes"])) == json.loads(body)


def test_writer_supersession_replaces_both_tables(pg_dsn):
    scope = _scope()
    body1, body2 = _body("supersede-1"), _body("supersede-2")
    # ONE canonical hash across both writes: the hash is part of the committed
    # unique identity, so a fixed value is what makes the second write a
    # supersession of the first (random hashes would be two distinct identities).
    identity_hash = "pc2-supersede-fixed-identity"
    ref1 = _store(scope, body1, canonical_prompt_hash=identity_hash)
    assert ref1
    ref2 = _store(scope, body2, canonical_prompt_hash=identity_hash)
    assert ref2 and ref2 != ref1, "supersession must mint a new response_ref"

    assert _response_rows(pg_dsn, ref=ref1) == [], "old response row survived supersession"
    rows = _response_rows(pg_dsn, ref=ref2)
    assert len(rows) == 1 and rows[0]["payload_bytes"] == body2
    with psycopg.connect(pg_dsn) as pg:
        entries = pg.execute(
            "SELECT response_ref FROM semantic_cache_entries WHERE tenant_id = %s AND model = %s",
            (scope.tenant_id, scope.model),
        ).fetchall()
    assert [e[0] for e in entries] == [ref2], "entry must be retargeted by delete-then-reinsert, not duplicated"


def test_writer_refuses_oversized_response_without_caching(monkeypatch, pg_dsn):
    monkeypatch.setenv("SEMANTIC_CACHE_MAX_RESPONSE_BYTES", "64")
    get_settings.cache_clear()
    body = b"x" * 65
    ref = _store(_scope(), body)
    assert ref is None, "oversized body must be refused as a miss, never truncated"
    assert _response_rows(pg_dsn) == [], "refused body must not be cached"
    with psycopg.connect(pg_dsn) as pg:
        assert pg.execute("SELECT count(*) FROM semantic_cache_entries WHERE tenant_id = %s", (TENANT_A,)).fetchone()[0] == 0


def test_writer_ttl_governs_both_rows_together(pg_dsn):
    body = _body("ttl")
    ref = _store(_scope(), body, ttl_seconds=120)
    assert ref
    with psycopg.connect(pg_dsn) as pg:
        entry_exp = pg.execute(
            "SELECT expires_at FROM semantic_cache_entries WHERE response_ref = %s", (ref,)
        ).fetchone()
        resp_exp = pg.execute(
            "SELECT expires_at FROM semantic_cache_responses WHERE response_ref = %s", (ref,)
        ).fetchone()
    assert entry_exp is not None and resp_exp is not None
    assert entry_exp[0] == resp_exp[0], "the one TTL knob must set both rows to the same instant"
    with psycopg.connect(pg_dsn, autocommit=True) as pg:
        seconds = pg.execute(
            "SELECT extract(epoch FROM (expires_at - now())) FROM semantic_cache_responses WHERE response_ref = %s",
            (ref,),
        ).fetchone()[0]
    assert 60 <= float(seconds) <= 180, f"TTL not honored: expires in {seconds}s, requested 120s"
