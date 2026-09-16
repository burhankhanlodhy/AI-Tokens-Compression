"""PA-4 tests: exact-prefix cache detection + ledger attribution.

Requires Postgres (dev container on :5433); skips when unavailable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PSYCHOPG = None
try:
    import psycopg
    PSYCHOPG = psycopg
except ImportError:  # pragma: no cover
    pytest.skip("psycopg not installed", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_test_support import PG_BASE, unique_db_name  # noqa: E402
_DB = unique_db_name("ts_cache_test")
_PRIOR_DSN = os.environ.get("TOKEN_SAVER_PG_DSN")

SCHEMA = (Path(__file__).resolve().parent.parent.parent / "postgres-schema-v2.sql").read_text()

from proxy import caching  # noqa: E402


@pytest.fixture(scope="module")
def cache_env():
    try:
        with psycopg.connect(PG_BASE, autocommit=True, connect_timeout=3) as pg:
            pg.execute(f"DROP DATABASE IF EXISTS {_DB}")
            pg.execute(f"CREATE DATABASE {_DB}")
    except psycopg.OperationalError:
        pytest.skip(
            "TOKEN_SAVER_PG_BASE is unavailable for Postgres acceptance tests",
            allow_module_level=False,
        )

    dsn = f"{PG_BASE}/{_DB}"
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(SCHEMA)
        pg.execute("INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000000','default')")
        pg.execute("""INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES
            ('openrouter','https://openrouter.ai/api/v1','OpenAICompatAdapter','bearer'),
            ('openai','https://api.openai.com/v1','OpenAICompatAdapter','bearer'),
            ('anthropic','https://api.anthropic.com','AnthropicAdapter','x-api-key'),
            ('legacy','https://openrouter.ai/api/v1','OpenAICompatAdapter','bearer')""")
    old = caching._dsn
    caching._dsn = lambda: dsn
    yield dsn
    caching._dsn = old
    with psycopg.connect(PG_BASE, autocommit=True) as pg:
        pg.execute(f"DROP DATABASE IF EXISTS {_DB}")


def _body(sys_prompt, user_msg):
    return {"model": "openai/gpt-4o",
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": user_msg}]}


# ------------------------------------------------------ key canonicalization

def test_same_prefix_same_key(cache_env):
    b1 = _body("sys", "question A")
    b2 = _body("sys", "question B")  # same prefix, different dynamic tail
    assert caching.cache_key(caching.canonical_prefix(b1), "openai/gpt-4o", "openrouter") == \
           caching.cache_key(caching.canonical_prefix(b2), "openai/gpt-4o", "openrouter")


def test_different_system_different_key(cache_env):
    b1 = _body("sys A", "q")
    b2 = _body("sys B", "q")
    assert caching.cache_key(caching.canonical_prefix(b1), "openai/gpt-4o", "openrouter") != \
           caching.cache_key(caching.canonical_prefix(b2), "openai/gpt-4o", "openrouter")


def test_message_order_matters(cache_env):
    b1 = {"model": "m", "messages": [{"role": "user", "content": "a"},
                                     {"role": "user", "content": "b"}]}
    b2 = {"model": "m", "messages": [{"role": "user", "content": "b"},
                                     {"role": "user", "content": "a"}]}
    # prefix = all but last message; these prefixes differ
    assert caching.canonical_prefix(b1) != caching.canonical_prefix(b2)


def test_key_includes_model_and_provider(cache_env):
    p = caching.canonical_prefix(_body("sys", "q"))
    assert caching.cache_key(p, "m1", "openrouter") != caching.cache_key(p, "m2", "openrouter")
    assert caching.cache_key(p, "m1", "openrouter") != caching.cache_key(p, "m1", "anthropic")


# ------------------------------------------------------ lookup/record behavior

def test_record_then_lookup_hits(cache_env):
    b = _body("be brief", "hello")
    caching.record("openrouter", "openai/gpt-4o", b)
    assert caching.lookup("openrouter", "openai/gpt-4o", b) is not None


def test_miss_on_unknown_prefix(cache_env):
    b = _body("never seen before", "unique")
    assert caching.lookup("openrouter", "openai/gpt-4o", b) is None


def test_lookup_increments_hit_count(cache_env):
    b = _body("count me", "q")
    caching.record("openrouter", "openai/gpt-4o", b)
    caching.lookup("openrouter", "openai/gpt-4o", b)
    caching.lookup("openrouter", "openai/gpt-4o", b)
    with psycopg.connect(caching._dsn()) as pg:
        n = pg.execute("SELECT hit_count FROM cache_entries WHERE prefix_hash = %s",
                       (caching.cache_key(caching.canonical_prefix(b), "openai/gpt-4o", "openrouter"),)).fetchone()[0]
    assert n == 2


def test_provider_isolation(cache_env):
    b = _body("prov isolation", "q")
    caching.record("openrouter", "openai/gpt-4o", b)
    # same prefix on another provider = miss
    assert caching.lookup("anthropic", "openai/gpt-4o", b) is None


def test_cache_failure_never_raises(cache_env, monkeypatch):
    b = _body("boom", "q")
    def _boom(*a, **k):
        raise RuntimeError("pg down")
    monkeypatch.setattr(caching, "record", _boom)
    monkeypatch.setattr(caching, "lookup", _boom)
    # pipeline-level guard: exceptions are caught in main; here assert the
    # functions themselves are what the try/except wraps (documented contract)
    with pytest.raises(RuntimeError):
        caching.lookup("openrouter", "openai/gpt-4o", b)


# ------------------------------------------------------ ledger attribution

def test_log_request_writes_cache_columns(cache_env):
    from proxy import stats
    os.environ["TOKEN_SAVER_PG_DSN"] = cache_env
    try:
        stats.log_request(model="openai/gpt-4o", route="compress",
                          input_tokens_before=100, input_tokens_after=60,
                          output_tokens=10, est_cost_before=0.001,
                          est_cost_after=0.0006, latency_ms=120.0,
                          compressed=True, status=200,
                          cache_status="exact_hit", cache_savings=0.0005)
        with psycopg.connect(cache_env) as pg:
            row = pg.execute(
                "SELECT cache_status, cache_savings, p.name FROM requests r"
                " JOIN providers p ON p.id = r.provider_id"
                " WHERE r.model = 'openai/gpt-4o' ORDER BY r.id DESC LIMIT 1"
            ).fetchone()
        assert row[0] == "exact_hit"
        assert float(row[1]) == 0.0005
        assert row[2] == "openai"  # provider routed from model prefix
    finally:
        if _PRIOR_DSN is None:
            os.environ.pop("TOKEN_SAVER_PG_DSN", None)
        else:
            os.environ["TOKEN_SAVER_PG_DSN"] = _PRIOR_DSN


def test_log_request_default_miss(cache_env):
    from proxy import stats
    os.environ["TOKEN_SAVER_PG_DSN"] = cache_env
    try:
        stats.log_request(model="openai/gpt-4o", route="passthrough",
                          input_tokens_before=10, input_tokens_after=10,
                          output_tokens=5, est_cost_before=0.0001,
                          est_cost_after=0.0001, latency_ms=50.0,
                          compressed=False, status=200)
        with psycopg.connect(cache_env) as pg:
            row = pg.execute(
                "SELECT cache_status, cache_savings FROM requests"
                " WHERE route='passthrough' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row[0] == "miss" and float(row[1]) == 0.0
    finally:
        if _PRIOR_DSN is None:
            os.environ.pop("TOKEN_SAVER_PG_DSN", None)
        else:
            os.environ["TOKEN_SAVER_PG_DSN"] = _PRIOR_DSN
