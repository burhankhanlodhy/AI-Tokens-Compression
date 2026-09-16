"""PA-4: exact-prefix cache orchestration over the Postgres `cache_entries`
table (schema: postgres-schema-v2.sql).

Design (product-spec-v2.md PA-4):
- Cache key = sha256 over the *canonicalized static prefix*: everything up to
  (and excluding) the last user message — system prompt + stable history —
  plus model and provider name. Exact match only; semantic is Phase C.
- Cache STATUS is recorded per request ('miss' | 'exact_hit'); cache-hit
  SAVINGS are reported separately from compression savings (AC-A6) so the
  dashboard stays honest against the provider-native-caching critique.
- Phase A does NOT synthesize responses locally (that requires serving the
  stored completion body, which Phase B adds with response storage). What
  Phase A delivers: detection, flag injection for providers with native
  caching (Anthropic cache_control), hit/miss ledger attribution, and the
  savings accounting path the dashboard reads.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg

from .db import get_pg_dsn

CACHE_TTL_HOURS = 24


def _dsn() -> str:
    return get_pg_dsn()


def canonical_prefix(body: dict[str, Any]) -> str:
    """Serialize the cacheable prefix deterministically.

    Prefix = system prompt + every message except the final user message
    (the dynamic part). Canonicalization: sorted keys, compact separators.
    """
    messages = body.get("messages") or []
    prefix_msgs = messages[:-1] if messages else []
    system = body.get("system") or _extract_system(messages)
    payload = json.dumps(
        {"system": system, "messages": prefix_msgs},
        sort_keys=True, separators=(",", ":"),
    )
    return payload


def _extract_system(messages: list[dict[str, Any]]) -> str | None:
    for m in messages:
        if m.get("role") == "system":
            return m.get("content")
    return None


def cache_key(prefix: str, model: str, provider: str) -> str:
    h = hashlib.sha256()
    h.update(prefix.encode())
    h.update(b"\x00")
    h.update(model.encode())
    h.update(b"\x00")
    h.update(provider.encode())
    return h.hexdigest()


def lookup(provider: str, model: str, body: dict[str, Any]) -> str | None:
    """Return the cache entry id if this exact prefix was seen recently."""
    key = cache_key(canonical_prefix(body), model, provider)
    try:
        with psycopg.connect(_dsn()) as pg:
            row = pg.execute(
                """
                UPDATE cache_entries
                   SET hit_count = hit_count + 1, last_hit_at = now()
                 WHERE tenant_id = '00000000-0000-0000-0000-000000000000'
                   AND provider_id = (SELECT id FROM providers WHERE name = %s)
                   AND model = %s AND prefix_hash = %s AND expires_at > now()
                RETURNING id
                """,
                (provider, model, key),
            ).fetchone()
            return str(row[0]) if row else None
    except psycopg.Error:
        return None  # cache must never break the proxy path


def record(provider: str, model: str, body: dict[str, Any]) -> None:
    """Insert/refresh a cache entry for this prefix (fire-and-forget)."""
    key = cache_key(canonical_prefix(body), model, provider)
    try:
        with psycopg.connect(_dsn()) as pg:
            pg.execute(
                """
                INSERT INTO cache_entries (tenant_id, provider_id, model, prefix_hash,
                                           expires_at)
                VALUES ('00000000-0000-0000-0000-000000000000',
                        (SELECT id FROM providers WHERE name = %s), %s, %s,
                        now() + interval '%s hours')
                ON CONFLICT (tenant_id, provider_id, model, prefix_hash)
                DO UPDATE SET expires_at = EXCLUDED.expires_at
                """,
                (provider, model, key, CACHE_TTL_HOURS),
            )
    except psycopg.Error:
        pass


def cache_flag_headers(provider: str) -> dict[str, str]:
    """Provider-native cache flags we can express via headers (future use)."""
    return {}
