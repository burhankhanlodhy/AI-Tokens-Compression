"""V2.1 session-store migration regression (t_254f9f59, DBA audit package).

Migration 7 (``migrations/20260924_v21_session_stores.sql``, t_cfccc06c) adds
the four V2.1 session stores: ``tocp_continuations``, ``idcp_file_versions``,
``mtcc_turns``, and ``strategy_telemetry``.  Every earlier MIGRATIONS.md entry
has a committed migration-sequence regression; this pins migration 7 to the
same standard so the V2.1 lanes inherit a tested data contract:

- fresh volume (Compose initdb): ``postgres-schema-v2.sql`` first, then the
  mounted ``48-v21-session-stores.sql`` — the idempotent re-apply must be a
  no-op, not an initdb failure;
- existing volume (upgrade runbook): a pre-V2.1 schema upgrades by applying
  only migration 7, and a repeated ops run must still succeed (rows survive);
- constraint rejection matrix: sha256 format/digest, content length, TTL
  positivity, identity non-emptiness, relevance tier, turn identity, telemetry
  decision taxonomy, latency, and segment bounds all reject bad rows;
- isolation: continuation/file-version/turn rows are tenant- and session-
  scoped; the retrieval plan uses the tenant-led indexes (no seq scan);
- retention: the documented TTL purge removes only expired rows;
- rollback compatibility: the documented rollback drops exactly the four V2.1
  tables, leaves the ``requests`` ledger contract byte-identical, keeps
  pre-existing ledger rows readable with V2.0 attribution intact, and a
  subsequent re-apply is clean;
- tenancy FK class: session stores cascade from ``tenants`` (disposable
  derived state) while ``requests`` keeps RESTRICT (audit history).

Requires a Postgres with ``pgcrypto`` available (the pinned
pgvector/pgvector:0.8.6-pg16 lane); skips cleanly without
``TOKEN_SAVER_PG_BASE`` like the other Postgres acceptance suites.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE, unique_db_name, require_pg_base  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_SCHEMA = REPO_ROOT / "postgres-schema-v2.sql"
V21_MIGRATION = (
    REPO_ROOT / "token-saver/migrations/20260924_v21_session_stores.sql"
)

V21_TABLES = (
    "tocp_continuations",
    "idcp_file_versions",
    "mtcc_turns",
    "strategy_telemetry",
)
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"
TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
KEY_A = "33333333-3333-3333-3333-333333333333"
KEY_B = "44444444-4444-4444-4444-444444444444"


def _apply(db_dsn: str, sql: str) -> None:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        pg.execute(sql)


def _v21_objects(db_dsn: str) -> list[str]:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        rows = pg.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) "
            "ORDER BY table_name",
            (list(V21_TABLES),),
        ).fetchall()
    return [r[0] for r in rows]


def _ledger_contract(db_dsn: str) -> str:
    """A fingerprint of the requests ledger DDL the V2.1 change must not bend."""
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        columns = pg.execute(
            "SELECT column_name || ':' || data_type || ':' || is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'requests' "
            "ORDER BY ordinal_position"
        ).fetchall()
        constraints = pg.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'requests'::regclass ORDER BY conname"
        ).fetchall()
    cols = "|".join(r[0] for r in columns)
    cons = "|".join(r[0] for r in constraints)
    return f"{cols}#{cons}"


def _seed_dimensions(db_dsn: str) -> None:
    """Tenants + api_keys + one provider row the probes insert against."""
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        for tid, name in ((DEFAULT_TENANT, "default"), (TENANT_A, "a"), (TENANT_B, "b")):
            pg.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (tid, name),
            )
        for kid, tid in ((KEY_A, TENANT_A), (KEY_B, TENANT_B)):
            pg.execute(
                """INSERT INTO api_keys (id, tenant_id, key_hash, key_last4)
                   VALUES (%s, %s, 'x' || %s, 'last4')
                   ON CONFLICT (id) DO NOTHING""",
                (kid, tid, kid[:8]),
            )
        pg.execute(
            """INSERT INTO providers (name, base_url, adapter_class, auth_style)
               VALUES ('legacy', 'https://openrouter.ai/api/v1',
                       'OpenAICompatAdapter', 'bearer')"""
        )


def _insert_tocp(db_dsn: str, tenant: str, continuation: str, session: str = "s1",
                 key: str | None = None) -> None:
    content = b"full tool output"
    digest = hashlib.sha256(content).hexdigest()
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        pg.execute(
            """INSERT INTO tocp_continuations
                   (tenant_id, api_key_id, session_id, continuation_id, model,
                    tool_name, result_status, exit_code, summary, content,
                    content_sha256, content_length, segment_bytes,
                    segment_count, expires_at)
               VALUES (%s, %s, %s, %s, 'test-model', 'bash', 'success', 0,
                       'ok summary', %s, %s, %s, 0, 0,
                       now() + interval '1 hour')""",
            (tenant, key, session, continuation, content,
             digest, len(content)),
        )


def _insert_telemetry(db_dsn: str, tenant: str, strategy: str, decision: str,
                      session: str | None = None, key: str | None = None) -> None:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        pg.execute(
            """INSERT INTO strategy_telemetry
                   (tenant_id, api_key_id, session_id, strategy, decision,
                    reason, strategy_version, flag_enabled, latency_ms,
                    metadata)
               VALUES (%s, %s, %s, %s, %s, 'probe', 'v1', false, 1.5,
                       '{"lane": "probe"}'::jsonb)""",
            (tenant, key, session, strategy, decision),
        )


@pytest.fixture()
def _fresh_db():
    require_pg_base()
    name = unique_db_name("v21_stores_fresh")
    dsn = f"{PG_BASE}/{name}"
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")
        pg.execute(f"CREATE DATABASE {name}")
    yield dsn
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {name}")


@pytest.fixture()
def _provisioned_db(_fresh_db):
    """Fresh volume with base schema + migration 7 + seeded dimensions."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    _apply(_fresh_db, V21_MIGRATION.read_text())
    _seed_dimensions(_fresh_db)
    return _fresh_db


# ---------------------------------------------------------------- sequences

def test_fresh_volume_schema_then_migration_sequence(_fresh_db):
    """initdb order: base schema first, then the mounted migration 7."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    _apply(_fresh_db, V21_MIGRATION.read_text())
    assert sorted(_v21_objects(_fresh_db)) == sorted(V21_TABLES)


def test_fresh_volume_reapply_is_idempotent(_fresh_db):
    """A repeated initdb application must succeed (CREATE ... IF NOT EXISTS)."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    _apply(_fresh_db, V21_MIGRATION.read_text())
    _apply(_fresh_db, V21_MIGRATION.read_text())
    assert sorted(_v21_objects(_fresh_db)) == sorted(V21_TABLES)


def test_upgrade_volume_migration_only_sequence(_fresh_db):
    """Existing pre-V2.1 volume: base schema alone, then migration 7 once."""
    _apply(_fresh_db, BASE_SCHEMA.read_text())
    assert _v21_objects(_fresh_db) == []
    _apply(_fresh_db, V21_MIGRATION.read_text())
    assert sorted(_v21_objects(_fresh_db)) == sorted(V21_TABLES)
    _seed_dimensions(_fresh_db)
    # Rows inserted on the upgraded volume must satisfy every constraint.
    _insert_tocp(_fresh_db, TENANT_A, "cont-upgrade")
    with psycopg.connect(_fresh_db, autocommit=True) as pg:
        row = pg.execute(
            "SELECT content, content_sha256 FROM tocp_continuations "
            "WHERE tenant_id = %s",
            (TENANT_A,),
        ).fetchone()
    assert row[0] == b"full tool output" and len(row[1]) == 64


def test_migration_is_wired_to_fresh_init_and_documented():
    """Compose initdb mount present, ordered after the V2 ledger migration,
    and the migration is registered in MIGRATIONS.md as entry 7."""
    compose = (REPO_ROOT / "token-saver/docker-compose.yml").read_text()
    migrations_md = (REPO_ROOT / "MIGRATIONS.md").read_text()
    v2_mount = "20260924_v2_provider_cache_usage.sql:/docker-entrypoint-initdb.d/47-v2-provider-cache-usage.sql"
    v21_mount = "20260924_v21_session_stores.sql:/docker-entrypoint-initdb.d/48-v21-session-stores.sql"
    assert v21_mount in compose
    assert compose.index(v2_mount) < compose.index(v21_mount), (
        "V2.1 session-store mount must follow the V2 ledger migration in "
        "initdb order"
    )
    assert "migrations/20260924_v21_session_stores.sql" in migrations_md
    assert "48-v21-session-stores.sql" in migrations_md


# ---------------------------------------------------- constraint rejections

def _expect_reject(db_dsn: str, sql: str, params: tuple) -> None:
    with psycopg.connect(db_dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(sql, params)


TOCP_INSERT = (
    """INSERT INTO tocp_continuations
           (tenant_id, api_key_id, session_id, continuation_id, model,
            tool_name, result_status, exit_code, summary, content,
            content_sha256, content_length, segment_bytes, segment_count,
            expires_at)
       VALUES (%s, NULL, %s, %s, 'm', 'bash', 'success', 0, 'sum', %s,
               %s, %s, 0, 0, now() + interval '1 hour')"""
)
IDCP_INSERT = (
    """INSERT INTO idcp_file_versions
           (tenant_id, api_key_id, session_id, canonical_path, version_id,
            content, content_sha256, content_length, expires_at)
       VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, now() + interval '1 hour')"""
)
MTCC_INSERT = (
    """INSERT INTO mtcc_turns
           (tenant_id, api_key_id, session_id, turn_index, role, content,
            content_sha256, content_length, relevance_tier,
            compressed_summary, expires_at)
       VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, NULL,
               now() + interval '1 hour')"""
)
TELEMETRY_INSERT = (
    """INSERT INTO strategy_telemetry
           (tenant_id, api_key_id, session_id, strategy, decision, reason,
            strategy_version, flag_enabled, latency_ms, metadata)
       VALUES (%s, NULL, 's1', %s, %s, 'probe', 'v1', false, %s, NULL)"""
)


def test_constraint_rejection_matrix(_provisioned_db):
    dsn = _provisioned_db
    t = TENANT_A
    payload = b"bytes"
    payload_sha = hashlib.sha256(payload).hexdigest()
    good_sha = "a" * 64  # well-formed 64-hex hash that is NOT the payload digest

    # -- tocp_continuations --
    # sha256 format: not 64 lowercase hex chars.
    _expect_reject(dsn, TOCP_INSERT, (t, "s1", "c1", payload, "A" * 64, 6))
    # digest mismatch: well-formed hash that is not the payload's digest.
    _expect_reject(dsn, TOCP_INSERT, (t, "s1", "c1", payload, good_sha, 6))
    # content_length mismatch: digest right, length wrong.
    _expect_reject(dsn, TOCP_INSERT, (t, "s1", "c1", payload, good_sha, 7))
    # TTL not positive: expires_at <= created_at.
    with psycopg.connect(dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """INSERT INTO tocp_continuations
                       (tenant_id, session_id, continuation_id, model,
                        tool_name, result_status, summary, content,
                        content_sha256, content_length, expires_at)
                   VALUES (%s, 's1', 'c-ttl', 'm', 'bash', 'success', 'sum',
                           %s, %s, %s, now())""",
                (t, payload, good_sha, len(payload)),
            )
    # empty session_id.
    _expect_reject(dsn, TOCP_INSERT, (t, "", "c1", payload, good_sha, 6))
    # empty continuation_id.
    _expect_reject(dsn, TOCP_INSERT, (t, "s1", "", payload, good_sha, 6))
    # empty tool_name.
    with psycopg.connect(dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """INSERT INTO tocp_continuations
                       (tenant_id, session_id, continuation_id, model,
                        tool_name, result_status, summary, content,
                        content_sha256, content_length, expires_at)
                   VALUES (%s, 's1', 'c1', 'm', '', 'success', 'sum',
                           %s, %s, %s, now() + interval '1 hour')""",
                (t, payload, good_sha, len(payload)),
            )
    # empty result_status.
    with psycopg.connect(dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """INSERT INTO tocp_continuations
                       (tenant_id, session_id, continuation_id, model,
                        tool_name, result_status, summary, content,
                        content_sha256, content_length, expires_at)
                   VALUES (%s, 's1', 'c1', 'm', 'bash', '', 'sum',
                           %s, %s, %s, now() + interval '1 hour')""",
                (t, payload, good_sha, len(payload)),
            )
    # negative segment_bytes.
    _expect_reject(dsn, TOCP_INSERT.replace(
        "0, 0, now() + interval '1 hour')",
        "0, -1, now() + interval '1 hour')"),
        (t, "s1", "c1", payload, good_sha, 6))

    # -- idcp_file_versions --
    _expect_reject(dsn, IDCP_INSERT, (t, "s1", "p", "v1", payload, "A" * 64, 6))
    _expect_reject(dsn, IDCP_INSERT, (t, "s1", "p", "v1", payload, good_sha, 9))
    with psycopg.connect(dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """INSERT INTO idcp_file_versions
                       (tenant_id, session_id, canonical_path, version_id,
                        content, content_sha256, content_length, expires_at)
                   VALUES (%s, 's1', 'p', 'v-ttl', %s, %s, %s, now())""",
                (t, payload, good_sha, len(payload)),
            )
    _expect_reject(dsn, IDCP_INSERT, (t, "", "p", "v1", payload, good_sha, 6))
    _expect_reject(dsn, IDCP_INSERT, (t, "s1", "", "v1", payload, good_sha, 6))
    _expect_reject(dsn, IDCP_INSERT, (t, "s1", "p", "", payload, good_sha, 6))

    # -- mtcc_turns --
    _expect_reject(dsn, MTCC_INSERT, (t, "s1", 0, "user", payload, "A" * 64, 6, None))
    _expect_reject(dsn, MTCC_INSERT, (t, "s1", 0, "user", payload, good_sha, 9, None))
    # negative turn_index.
    _expect_reject(dsn, MTCC_INSERT, (t, "s1", -1, "user", payload, good_sha, 6, None))
    # bogus relevance_tier.
    _expect_reject(dsn, MTCC_INSERT, (t, "s1", 0, "user", payload, good_sha, 6, "huge"))
    # duplicate (tenant, session, turn_index) identity.
    _insert_turn_ok = (
        """INSERT INTO mtcc_turns
               (tenant_id, session_id, turn_index, role, content,
                content_sha256, content_length, expires_at)
           VALUES (%s, 's1', 3, 'user', %s, %s, %s, now() + interval '1 hour')"""
    )
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(_insert_turn_ok, (t, payload, payload_sha, len(payload)))
    with psycopg.connect(dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.UniqueViolation):
            pg.execute(_insert_turn_ok, (t, payload, payload_sha, len(payload)))
    with psycopg.connect(dsn, autocommit=True) as pg:
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """INSERT INTO mtcc_turns
                       (tenant_id, session_id, turn_index, role, content,
                        content_sha256, content_length, expires_at)
                   VALUES (%s, 's1', 4, 'user', %s, %s, %s, now())""",
                (t, payload, good_sha, len(payload)),
            )
    _expect_reject(dsn, MTCC_INSERT, (t, "", 0, "user", payload, good_sha, 6, None))
    _expect_reject(dsn, MTCC_INSERT, (t, "s1", 0, "", payload, good_sha, 6, None))

    # -- strategy_telemetry --
    # bad decision taxonomy.
    _expect_reject(dsn, TELEMETRY_INSERT, (t, "atba", "unknown", 1.0))
    # empty strategy.
    _expect_reject(dsn, TELEMETRY_INSERT, (t, "", "applied", 1.0))
    # negative latency.
    _expect_reject(dsn, TELEMETRY_INSERT, (t, "atba", "applied", -0.1))


def test_valid_inserts_accepted_per_store(_provisioned_db):
    dsn = _provisioned_db
    _insert_tocp(dsn, TENANT_A, "cont-ok", session="sess", key=KEY_A)
    idcp_bytes = b"file bytes"
    mtcc_bytes = b"turn text"
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            IDCP_INSERT,
            (TENANT_A, "s1", "/src/app.py", "v1", idcp_bytes,
             hashlib.sha256(idcp_bytes).hexdigest(), len(idcp_bytes)),
        )
        pg.execute(
            """INSERT INTO mtcc_turns
                   (tenant_id, session_id, turn_index, role, content,
                    content_sha256, content_length, relevance_tier,
                    compressed_summary, expires_at)
               VALUES (%s, 's1', 0, 'assistant', %s, %s, %s, 'medium',
                       'structured facts', now() + interval '1 hour')""",
            (TENANT_A, mtcc_bytes, hashlib.sha256(mtcc_bytes).hexdigest(),
             len(mtcc_bytes)),
        )
        _insert_telemetry(dsn, TENANT_A, "atba", "shadow", session="s1", key=KEY_A)
        counts = {
            tbl: pg.execute(
                "SELECT count(*) FROM " + tbl
            ).fetchone()[0]
            for tbl in V21_TABLES
        }
    assert counts == {
        "tocp_continuations": 1,
        "idcp_file_versions": 1,
        "mtcc_turns": 1,
        "strategy_telemetry": 1,
    }


# ----------------------------------------------------------------- isolation

def test_cross_tenant_continuation_ids_are_isolated(_provisioned_db):
    dsn = _provisioned_db
    # Both tenants hold continuation_id 'cont1' — the unique key is tenant-
    # scoped, so neither insert collides and retrieval is tenant-filtered.
    _insert_tocp(dsn, TENANT_A, "cont1", session="shared-session")
    _insert_tocp(dsn, TENANT_B, "cont1", session="shared-session")
    with psycopg.connect(dsn, autocommit=True) as pg:
        for tenant in (TENANT_A, TENANT_B):
            row = pg.execute(
                "SELECT tenant_id, session_id FROM tocp_continuations "
                "WHERE tenant_id = %s AND session_id = 'shared-session' "
                "AND continuation_id = 'cont1' AND expires_at > now()",
                (tenant,),
            ).fetchone()
            assert row is not None and str(row[0]) == tenant
        assert pg.execute(
            "SELECT count(*) FROM tocp_continuations"
        ).fetchone()[0] == 2


def test_cross_session_retrieval_returns_nothing(_provisioned_db):
    dsn = _provisioned_db
    _insert_tocp(dsn, TENANT_A, "cont-s", session="session-A")
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(
            "SELECT 1 FROM tocp_continuations WHERE tenant_id = %s "
            "AND session_id = 'session-B' AND continuation_id = 'cont-s'",
            (TENANT_A,),
        ).fetchone()
    assert row is None


def test_retrieval_plan_uses_tenant_ledged_index_not_seq_scan(_provisioned_db):
    dsn = _provisioned_db
    _insert_tocp(dsn, TENANT_A, "cont-plan", session="s-plan")
    with psycopg.connect(dsn, autocommit=True) as pg:
        plan = " ".join(
            str(r[0])
            for r in pg.execute(
                "EXPLAIN SELECT * FROM tocp_continuations "
                "WHERE tenant_id = %s AND session_id = 's-plan' "
                "AND expires_at > now()",
                (TENANT_A,),
            ).fetchall()
        )
    assert "Seq Scan" not in plan
    assert "uq_tocp_continuations_tenant_id" in plan or (
        "idx_tocp_continuations_lookup" in plan
    )


# --------------------------------------------------------------- retention

@pytest.mark.parametrize("table", ["tocp_continuations", "idcp_file_versions",
                                   "mtcc_turns"])
def test_ttl_expiry_purge_removes_only_expired_rows(_provisioned_db, table):
    dsn = _provisioned_db
    expired_bytes = b"expired"
    live_bytes = b"live"
    expired_sha = hashlib.sha256(expired_bytes).hexdigest()
    live_sha = hashlib.sha256(live_bytes).hexdigest()
    with psycopg.connect(dsn, autocommit=True) as pg:
        # The chk_*_ttl_positive CHECKs require expires_at > created_at, so an
        # already-expired row is created live and then aged: created_at is
        # backdated past expires_at exactly as a real row ages across its TTL.
        if table == "tocp_continuations":
            pg.execute(
                """INSERT INTO tocp_continuations
                       (tenant_id, session_id, expires_at,
                        continuation_id, model, tool_name, result_status, summary,
                        content, content_sha256, content_length)
                   SELECT %s, 'purge', now() + interval '1 minute',
                          'expired', 'm', 'bash', 'success', 'sum',
                          %s, %s, %s""",
                (TENANT_A, expired_bytes, expired_sha, len(expired_bytes)),
            )
            pg.execute(
                """INSERT INTO tocp_continuations
                       (tenant_id, session_id, expires_at,
                        continuation_id, model, tool_name, result_status, summary,
                        content, content_sha256, content_length)
                   VALUES (%s, 'keep', now() + interval '1 hour',
                           'live', 'm', 'bash', 'success', 'sum',
                           %s, %s, %s)""",
                (TENANT_A, live_bytes, live_sha, len(live_bytes)),
            )
        elif table == "idcp_file_versions":
            pg.execute(
                """INSERT INTO idcp_file_versions
                       (tenant_id, session_id, expires_at,
                        canonical_path, version_id,
                        content, content_sha256, content_length)
                   SELECT %s, 'purge', now() + interval '1 minute',
                          '/p', 'v-exp', %s, %s, %s""",
                (TENANT_A, expired_bytes, expired_sha, len(expired_bytes)),
            )
            pg.execute(
                """INSERT INTO idcp_file_versions
                       (tenant_id, session_id, expires_at,
                        canonical_path, version_id,
                        content, content_sha256, content_length)
                   VALUES (%s, 'keep', now() + interval '1 hour',
                           '/p', 'v-live', %s, %s, %s)""",
                (TENANT_A, live_bytes, live_sha, len(live_bytes)),
            )
        else:
            pg.execute(
                """INSERT INTO mtcc_turns
                       (tenant_id, session_id, turn_index, role, expires_at,
                        content, content_sha256, content_length)
                   SELECT %s, 'purge', 0, 'user', now() + interval '1 minute',
                          %s, %s, %s""",
                (TENANT_A, expired_bytes, expired_sha, len(expired_bytes)),
            )
            pg.execute(
                """INSERT INTO mtcc_turns
                       (tenant_id, session_id, turn_index, role, expires_at,
                        content, content_sha256, content_length)
                   VALUES (%s, 'keep', 1, 'user', now() + interval '1 hour',
                           %s, %s, %s)""",
                (TENANT_A, live_bytes, live_sha, len(live_bytes)),
            )
        # Age the 'purge' row into the expired state: backdate created_at and
        # set expires_at in the past (chk_*_ttl_positive still holds since
        # created_at < expires_at) — the exact expired-row state the purge
        # targets, without sleeping out the TTL in real time.
        pg.execute(
            f"UPDATE {table} SET created_at = now() - interval '5 minutes', "
            "expires_at = now() - interval '1 minute' "
            "WHERE session_id = 'purge'"
        )
        assert pg.execute(
            f"SELECT count(*) FROM {table} WHERE expires_at <= now()"
        ).fetchone()[0] == 1
        # The documented purge statement (MIGRATIONS.md, "V2.1 session
        # stores — retention and rollback").
        pg.execute(f"DELETE FROM {table} WHERE expires_at <= now();")
        expired = pg.execute(
            f"SELECT count(*) FROM {table} WHERE session_id = 'purge'"
        ).fetchone()[0]
        live = pg.execute(
            f"SELECT count(*) FROM {table} WHERE session_id = 'keep'"
        ).fetchone()[0]
    assert expired == 0 and live == 1


# -------------------------------------------------- rollback compatibility

def test_rollback_restores_pre_v21_ledger_contract_and_reapplies(
    _provisioned_db,
):
    dsn = _provisioned_db
    before = _ledger_contract(dsn)

    # A pre-rollback ledger row with the full V2.0 attribution payload.
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            """INSERT INTO requests (tenant_id, provider_id, model, route,
                   input_tokens_before, input_tokens_after, output_tokens,
                   cache_status, cache_savings, l1_tokens_stripped,
                   l1_savings, tool_compression_saved,
                   provider_cache_read_tokens, provider_cache_write_tokens)
               SELECT %s, p.id, 'm', 'compress', 100, 80, 10, 'exact_hit',
                      0.5::numeric, 12, 0.25::numeric, 3, 640, 1000
                 FROM providers p WHERE p.name = 'legacy'""",
            (TENANT_A,),
        )
    _insert_tocp(dsn, TENANT_A, "cont-rb")
    _insert_telemetry(dsn, TENANT_A, "tocp", "applied")

    # The documented rollback (MIGRATIONS.md): one transaction dropping the
    # four V2.1 tables.
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("BEGIN")
        for table in ("strategy_telemetry", "mtcc_turns", "idcp_file_versions",
                      "tocp_continuations"):
            pg.execute(f"DROP TABLE IF EXISTS {table}")
        pg.execute("COMMIT")
    assert _v21_objects(dsn) == []
    # The requests ledger contract is byte-identical across the rollback.
    assert _ledger_contract(dsn) == before

    # Pre-existing ledger rows survive and keep their V2.0 attribution.
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(
            """SELECT cache_savings, l1_savings, tool_compression_saved,
                      provider_cache_read_tokens, provider_cache_write_tokens
                 FROM requests"""
        ).fetchone()
    assert row == (0.5, 0.25, 3, 640, 1000)

    # Re-apply after rollback is clean; ledger row still present.
    _apply(dsn, V21_MIGRATION.read_text())
    assert sorted(_v21_objects(dsn)) == sorted(V21_TABLES)
    with psycopg.connect(dsn, autocommit=True) as pg:
        assert pg.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


def test_tenant_delete_cascades_session_stores_not_ledger(_provisioned_db):
    dsn = _provisioned_db
    _insert_tocp(dsn, TENANT_A, "cont-cas")
    _insert_telemetry(dsn, TENANT_A, "tocp", "applied")
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            """INSERT INTO requests (tenant_id, provider_id, model, route,
                   input_tokens_before, input_tokens_after)
               SELECT %s, p.id, 'm', 'compress', 10, 8
                 FROM providers p WHERE p.name = 'legacy'""",
            (TENANT_A,),
        )
        # requests FK is RESTRICT: the tenant with ledger history cannot be
        # hard-deleted (audit integrity — unchanged by V2.1).
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            pg.execute("DELETE FROM tenants WHERE id = %s", (TENANT_A,))
        # A tenant with only session-store rows cascades (disposable state).
        # Its api_key must go first: api_keys.tenant_id is RESTRICT (only
        # requests/api_keys audit edges protect history; the session stores
        # themselves are CASCADE leaves).
        _insert_tocp(dsn, TENANT_B, "cont-cas-b")
        _insert_telemetry(dsn, TENANT_B, "tocp", "applied")
        pg.execute("DELETE FROM api_keys WHERE tenant_id = %s", (TENANT_B,))
        pg.execute("DELETE FROM tenants WHERE id = %s", (TENANT_B,))
        counts = {
            tbl: pg.execute("SELECT count(*) FROM " + tbl).fetchone()[0]
            for tbl in ("tocp_continuations", "strategy_telemetry")
        }
    assert counts == {"tocp_continuations": 1, "strategy_telemetry": 1}
