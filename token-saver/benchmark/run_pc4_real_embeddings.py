#!/usr/bin/env python3
"""Reproducible PC4 traffic-shaped pgvector calibration.

The runner creates and drops an isolated database, obtains embeddings for the
committed prompt corpus from the configured OpenAI-compatible endpoint, seeds
real vectors plus traffic-shaped scope volume, compares exact nearest-neighbor
results with the production filtered HNSW query, and writes JSON evidence.
It never writes the application database; --live-dsn is read-only evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

DIMS = 1536
MODEL = "text-embedding-3-small"
EMBEDDING_VERSION = "openai:text-embedding-3-small@1536"
QUALITY_VERSION = "pc4-real-corpus-v1"
PARAMETERS_HASH = "pc4-traffic-v1"
TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
TENANT_C = "33333333-3333-3333-3333-333333333333"
CASE_SCOPE = ("target/model", MODEL, DIMS, EMBEDDING_VERSION, QUALITY_VERSION, PARAMETERS_HASH)
THRESHOLDS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.22, 0.28, 0.35]
EF_SEARCH_VALUES = [100, 300, 1000]
HOT_SQL = """
WITH nearest AS (
    SELECT id, tenant_id, canonical_prompt_hash,
           embedding <=> %s::vector AS cosine_distance
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
SELECT id, tenant_id, canonical_prompt_hash, cosine_distance
  FROM nearest
 WHERE cosine_distance <= %s
"""
# Filler rows intentionally repeat the real corpus vectors, so restricting
# exact ground truth to the 48 canonical rows preserves the exact nearest
# distance while avoiding 48 redundant 10k-row full scans on the Pi host.
EXACT_SQL = HOT_SQL.replace(
    "AND expires_at > now()",
    "AND expires_at > now()\n       AND canonical_prompt_hash LIKE 'corpus-%%'",
)


@dataclass(frozen=True)
class Scope:
    tenant_id: str = TENANT_A
    provider: str = "openai"
    model: str = CASE_SCOPE[0]
    embedding_model: str = MODEL
    embedding_dimensions: int = DIMS
    embedding_version: str = EMBEDDING_VERSION
    quality_version: str = QUALITY_VERSION
    request_parameters_hash: str = PARAMETERS_HASH


class RunnerError(RuntimeError):
    pass


def vector_literal(values: list[float]) -> str:
    if len(values) != DIMS or not all(math.isfinite(v) for v in values):
        raise RunnerError("embedding dimensions or finiteness check failed")
    return "[" + ",".join(format(v, ".9g") for v in values) + "]"


def corpus_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def embedding_request(base_url: str, api_key: str, prompts: list[str], batch_size: int = 994) -> tuple[str, list[list[float]]]:
    endpoint = base_url.rstrip("/") + "/embeddings"
    vectors: list[list[float]] = []
    model_returned = ""
    for start in range(0, len(prompts), batch_size):
        chunk = prompts[start:start + batch_size]
        payload = json.dumps({"model": MODEL, "input": chunk}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = json.loads(response.read())
        except Exception as exc:  # pragma: no cover - network-specific
            raise RunnerError(f"embedding request failed: {type(exc).__name__}: {exc}") from exc
        data = raw.get("data")
        if not isinstance(data, list) or len(data) != len(chunk):
            raise RunnerError(f"embedding response count mismatch: expected {len(chunk)}")
        ordered = sorted(data, key=lambda item: int(item["index"]))
        vectors.extend(list(map(float, item["embedding"])) for item in ordered)
        model_returned = str(raw.get("model", MODEL))
    if any(len(v) != DIMS for v in vectors):
        raise RunnerError("embedding response did not use 1536 dimensions")
    return model_returned, vectors


def db_snapshot(dsn: str) -> dict[str, Any]:
    with psycopg.connect(dsn) as pg:
        row = pg.execute(
            """
            SELECT count(*)::bigint,
                   coalesce(sum(input_tokens_before), 0)::bigint,
                   coalesce(sum(input_tokens_after), 0)::bigint,
                   coalesce(sum(output_tokens), 0)::bigint,
                   coalesce(sum(est_cost_before), 0)::text,
                   coalesce(sum(est_cost_after), 0)::text,
                   coalesce(sum(cache_savings), 0)::text
              FROM requests
            """
        ).fetchone()
        entries = int(pg.execute("SELECT count(*) FROM semantic_cache_entries").fetchone()[0])
        responses = int(pg.execute("SELECT count(*) FROM semantic_cache_responses").fetchone()[0])
        return {
            "requests": int(row[0]),
            "input_tokens_before": int(row[1]),
            "input_tokens_after": int(row[2]),
            "output_tokens": int(row[3]),
            "est_cost_before": str(row[4]),
            "est_cost_after": str(row[5]),
            "cache_savings": str(row[6]),
            "semantic_cache_entries": entries,
            "semantic_cache_responses": responses,
        }


def apply_schemas(pg: psycopg.Connection, repo_root: Path) -> dict[str, Any]:
    schema = (repo_root / "postgres-schema-v2.sql").read_text(encoding="utf-8")
    pc1 = (repo_root / "token-saver/migrations/20260918_pc1_pgvector.sql").read_text(encoding="utf-8")
    pc2 = (repo_root / "token-saver/migrations/20260919_pc2_semantic_responses.sql").read_text(encoding="utf-8")
    pg.execute(schema)
    pg.execute(pc1)
    pg.execute(pc2)
    version = pg.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
    indexes = pg.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'semantic_cache_entries' ORDER BY indexname"
    ).fetchall()
    return {
        "vector_extension_version": str(version),
        "semantic_cache_entry_indexes": [r[0] for r in indexes],
    }


def rebuild_hnsw_index(dsn: str, ef_construction: int) -> dict[str, Any]:
    """Calibration-only control, run AFTER seeding (bulk build, like the
    measured index-quality control). 0 (default) keeps the migration's index
    untouched."""
    if not 1 <= ef_construction <= 1000:
        raise RunnerError("hnsw_ef_construction must be within 1..1000")
    started = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("DROP INDEX IF EXISTS idx_semantic_cache_embedding_hnsw")
        pg.execute(
            f"CREATE INDEX idx_semantic_cache_embedding_hnsw ON semantic_cache_entries "
            f"USING hnsw (embedding vector_cosine_ops) WITH (ef_construction={int(ef_construction)})"
        )
    return {"ef_construction": int(ef_construction), "m": 16, "build_seconds": round(time.perf_counter() - started, 1)}


def create_scratch(admin_dsn: str, repo_root: Path) -> tuple[str, str, dict[str, Any]]:
    suffix = str(os.getpid())
    db_name = f"dba_verify_pc4_real_{suffix}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} ").format(sql.Identifier(db_name)))
        admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(db_name)))
    scratch_dsn = admin_dsn.rstrip("/") + "/" + db_name
    try:
        with psycopg.connect(scratch_dsn, autocommit=True) as pg:
            schema_evidence = apply_schemas(pg, repo_root)
            pg.execute(
                """
                INSERT INTO tenants (id, name) VALUES
                  (%s, 'calibration-tenant-a'), (%s, 'calibration-tenant-b'), (%s, 'calibration-tenant-c')
                """,
                (TENANT_A, TENANT_B, TENANT_C),
            )
            pg.execute(
                """
                INSERT INTO providers (name, base_url, adapter_class, auth_style) VALUES
                  ('openai', 'https://api.openai.com/v1', 'OpenAICompatAdapter', 'bearer'),
                  ('openrouter', 'https://openrouter.ai/api/v1', 'OpenAICompatAdapter', 'bearer'),
                  ('anthropic', 'https://api.anthropic.com', 'AnthropicAdapter', 'x-api-key')
                """
            )
        return db_name, scratch_dsn, schema_evidence
    except Exception:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} ").format(sql.Identifier(db_name)))
        raise


def seed_database(
    dsn: str,
    vectors: dict[str, list[float]],
    cases: list[dict[str, Any]],
    target_rows: int,
    pool_vectors: list[list[float]] | None = None,
) -> dict[str, Any]:
    if target_rows < 1000:
        raise RunnerError("traffic-shaped target volume must be at least 1000 rows")
    stored_vectors = [vectors[c["stored_prompt"]] for c in cases]
    with psycopg.connect(dsn, autocommit=True) as pg:
        body = b'{"calibration":true}'
        digest = hashlib.sha256(body).hexdigest()
        with pg.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO semantic_cache_responses
                  (tenant_id, response_ref, payload, payload_bytes, schema_version, sha256, content_length, expires_at)
                VALUES (%s, %s, %s::jsonb, %s, 1, %s, %s, now() + interval '1 hour')
                """,
                [
                    (tenant, ref, body.decode(), psycopg.Binary(body), digest, len(body))
                    for tenant, ref in (
                        (TENANT_A, "calibration-response-a"),
                        (TENANT_B, "calibration-response-b"),
                        (TENANT_C, "calibration-response-c"),
                    )
                ],
            )
        pg.execute("CREATE TEMP TABLE calibration_vectors (slot integer PRIMARY KEY, embedding vector(1536) NOT NULL)")
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO calibration_vectors (slot, embedding) VALUES (%s, %s::vector)",
                [(i + 1, vector_literal(v)) for i, v in enumerate(stored_vectors)],
            )
        # Distinct-filler mode: volume rows draw from a pool of distinct real
        # prompts instead of repeating the corpus vectors.  Same scope
        # structure, same SQL shape; avoids degenerating the HNSW graph with
        # thousands of exact copies of 48 vectors.
        if pool_vectors:
            pg.execute("CREATE TEMP TABLE pool_vectors_table (slot integer PRIMARY KEY, embedding vector(1536) NOT NULL)")
            with pg.cursor() as cur:
                cur.executemany(
                    "INSERT INTO pool_vectors_table (slot, embedding) VALUES (%s, %s::vector)",
                    [(i + 1, vector_literal(v)) for i, v in enumerate(pool_vectors)],
                )
        filler_table = "pool_vectors_table" if pool_vectors else "calibration_vectors"
        filler_slots = len(pool_vectors) if pool_vectors else len(stored_vectors)
        provider_ids = {
            row[0]: int(row[1])
            for row in pg.execute("SELECT name, id FROM providers").fetchall()
        }
        base_args = [
            (
                TENANT_A, provider_ids["openai"], CASE_SCOPE[0], MODEL, DIMS,
                EMBEDDING_VERSION, QUALITY_VERSION, PARAMETERS_HASH,
                f"corpus-{case['id']}", vector_literal(vectors[case["stored_prompt"]]),
                "calibration-response-a",
            )
            for case in cases
        ]
        with pg.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO semantic_cache_entries
                  (tenant_id, provider_id, model, embedding_model, embedding_dimensions,
                   embedding_version, quality_version, request_parameters_hash,
                   canonical_prompt_hash, embedding, response_ref, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,now()+interval '1 hour')
                """,
                base_args,
            )
        filler_target = target_rows - len(cases)
        # Target scope is the dominant population. Vectors are all obtained from
        # real prompts; repeating them gives the planner the intended volume
        # without manufacturing a synthetic embedding distribution.
        pg.execute(
            f"""
            INSERT INTO semantic_cache_entries
              (tenant_id, provider_id, model, embedding_model, embedding_dimensions,
               embedding_version, quality_version, request_parameters_hash,
               canonical_prompt_hash, embedding, response_ref, expires_at)
            SELECT %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                   'traffic-target-' || g, s.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, %s) AS x(g)
              JOIN {filler_table} s ON s.slot = ((x.g - 1) %% %s) + 1
            """,
            (
                TENANT_A, provider_ids["openai"], CASE_SCOPE[0], MODEL, DIMS,
                EMBEDDING_VERSION, QUALITY_VERSION, PARAMETERS_HASH,
                "calibration-response-a", filler_target, filler_slots,
            ),
        )
        # Selectivity populations: a different tenant, provider/model, and
        # version/parameter scope all carry real vectors but cannot satisfy the
        # target lookup's mandatory filters.
        pg.execute(
            f"""
            INSERT INTO semantic_cache_entries
              (tenant_id, provider_id, model, embedding_model, embedding_dimensions,
               embedding_version, quality_version, request_parameters_hash,
               canonical_prompt_hash, embedding, response_ref, expires_at)
            SELECT %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                   'traffic-tenant-b-' || g, s.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, 500) AS x(g)
              JOIN {filler_table} s ON s.slot = ((x.g - 1) %% %s) + 1
            UNION ALL
            SELECT %s::uuid, %s, 'other/model', %s, %s, %s, %s, %s,
                   'traffic-model-' || g, s.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, 500) AS x(g)
              JOIN {filler_table} s ON s.slot = ((x.g - 1) %% %s) + 1
            UNION ALL
            SELECT %s::uuid, %s, %s, %s, %s, 'old:embedding@1536', 'old-quality', 'old-params',
                   'traffic-mismatch-' || g, s.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, 500) AS x(g)
              JOIN {filler_table} s ON s.slot = ((x.g - 1) %% %s) + 1
            """,
            (
                TENANT_B, provider_ids["openai"], CASE_SCOPE[0], MODEL, DIMS,
                EMBEDDING_VERSION, QUALITY_VERSION, PARAMETERS_HASH, "calibration-response-b", filler_slots,
                TENANT_A, provider_ids["openrouter"], MODEL, DIMS,
                EMBEDDING_VERSION, QUALITY_VERSION, PARAMETERS_HASH, "calibration-response-a", filler_slots,
                TENANT_A, provider_ids["openai"], CASE_SCOPE[0], MODEL, DIMS,
                "calibration-response-a", filler_slots,
            ),
        )
        pg.execute("ANALYZE semantic_cache_entries")
        row_count = int(pg.execute("SELECT count(*) FROM semantic_cache_entries").fetchone()[0])
        scope_counts = {
            "target_scope": int(pg.execute(
                """SELECT count(*) FROM semantic_cache_entries WHERE tenant_id=%s AND provider_id=%s
                   AND model=%s AND embedding_version=%s AND quality_version=%s AND request_parameters_hash=%s""",
                (TENANT_A, provider_ids["openai"], CASE_SCOPE[0], EMBEDDING_VERSION, QUALITY_VERSION, PARAMETERS_HASH),
            ).fetchone()[0]),
            "tenant_b_same_compatibility": 500,
            "other_provider": 500,
            "version_parameter_mismatch": 500,
        }
        sizes = pg.execute(
            """SELECT pg_relation_size('semantic_cache_entries')::bigint,
                      pg_indexes_size('semantic_cache_entries')::bigint,
                      pg_total_relation_size('semantic_cache_entries')::bigint"""
        ).fetchone()
    return {"row_count": row_count, "scope_counts": scope_counts, "table_bytes": int(sizes[0]), "index_bytes": int(sizes[1]), "total_bytes": int(sizes[2])}


def run_exact(pg: psycopg.Connection, vector: str, scope: Scope) -> dict[str, Any] | None:
    pg.execute("SET LOCAL enable_indexscan TO off")
    pg.execute("SET LOCAL enable_indexonlyscan TO off")
    params = hot_params(vector, scope, 2.0)
    row = pg.execute(EXACT_SQL, params).fetchone()
    return row_to_dict(row)


def hot_params(vector: str, scope: Scope, threshold: float) -> tuple[Any, ...]:
    return (
        vector, scope.tenant_id, scope.provider, scope.model, scope.embedding_model,
        scope.embedding_dimensions, scope.embedding_version, scope.quality_version,
        scope.request_parameters_hash, vector, threshold,
    )


def row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": int(row[0]), "tenant_id": str(row[1]), "canonical_prompt_hash": str(row[2]), "distance": float(row[3])}


def run_approx(pg: psycopg.Connection, vector: str, scope: Scope, threshold: float, ef: int) -> tuple[dict[str, Any] | None, float]:
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    start = time.perf_counter_ns()
    row = pg.execute(HOT_SQL, hot_params(vector, scope, threshold)).fetchone()
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    return row_to_dict(row), elapsed


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return round(ordered[low], 4)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 4)


def plan_summary(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
    out.append({
        "node_type": node.get("Node Type"),
        "index_name": node.get("Index Name"),
        "index_cond": node.get("Index Cond"),
        "actual_rows": node.get("Actual Rows"),
        "actual_total_time_ms": node.get("Actual Total Time"),
        "shared_hit_blocks": node.get("Shared Hit Blocks"),
        "shared_read_blocks": node.get("Shared Read Blocks"),
    })
    for child in node.get("Plans", []) or []:
        plan_summary(child, out)


def explain(pg: psycopg.Connection, vector: str, scope: Scope, threshold: float, ef: int) -> dict[str, Any]:
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    raw = pg.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + HOT_SQL,
        hot_params(vector, scope, threshold),
    ).fetchone()[0]
    if isinstance(raw, str):
        raw = json.loads(raw)
    root = raw[0]["Plan"]
    nodes: list[dict[str, Any]] = []
    plan_summary(root, nodes)
    return {"nodes": nodes, "planning_time_ms": raw[0].get("Planning Time"), "execution_time_ms": raw[0].get("Execution Time"), "raw": raw}


def application_guard_probe() -> dict[str, Any]:
    """Exercise the production seam's pre-query mandatory-filter refusal."""
    prior_enabled = os.environ.get("SEMANTIC_CACHE_ENABLED")
    try:
        sys.path.insert(0, "/app")
        from proxy import semantic_cache  # type: ignore
        from proxy.config import get_settings  # type: ignore
        os.environ["SEMANTIC_CACHE_ENABLED"] = "true"
        os.environ["TOKEN_SAVER_PG_DSN"] = os.environ.get("TOKEN_SAVER_PG_DSN", "")
        get_settings.cache_clear()
        bad = semantic_cache.SemanticLookupScope(
            tenant_id=TENANT_A, provider="", model="target/model", embedding_model=MODEL,
            embedding_dimensions=DIMS, embedding_version=EMBEDDING_VERSION,
            quality_version=QUALITY_VERSION, request_parameters_hash=PARAMETERS_HASH,
        )
        try:
            semantic_cache.lookup(bad, [0.0] * DIMS, max_cosine_distance=0.12)
        except ValueError as exc:
            return {"refused": True, "error": str(exc)}
        return {"refused": False, "error": "lookup unexpectedly reached query path"}
    except Exception as exc:  # pragma: no cover - environment-specific
        return {"refused": False, "error": f"probe unavailable: {type(exc).__name__}: {exc}"}
    finally:
        if prior_enabled is None:
            os.environ.pop("SEMANTIC_CACHE_ENABLED", None)
        else:
            os.environ["SEMANTIC_CACHE_ENABLED"] = prior_enabled
        try:
            get_settings.cache_clear()  # type: ignore[name-defined]
        except Exception:
            pass


def calibrate(
    scratch_dsn: str,
    cases: list[dict[str, Any]],
    vectors: dict[str, list[float]],
    repetitions: int,
    warmups: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    exact_by_case: dict[str, dict[str, Any] | None] = {}
    case_vectors: dict[str, str] = {}
    with psycopg.connect(scratch_dsn) as pg:
        for case in cases:
            vector = vector_literal(vectors[case["query_prompt"]])
            case_vectors[case["id"]] = vector
            with pg.transaction():
                exact_by_case[case["id"]] = run_exact(pg, vector, Scope())
        results: list[dict[str, Any]] = []
        for ef in EF_SEARCH_VALUES:
            for threshold in THRESHOLDS:
                latencies: list[float] = []
                observations: list[dict[str, Any]] = []
                for case in cases:
                    vector = case_vectors[case["id"]]
                    for _ in range(warmups):
                        with pg.transaction():
                            run_approx(pg, vector, Scope(), threshold, ef)
                    case_hits: list[bool] = []
                    for _ in range(repetitions):
                        with pg.transaction():
                            row, elapsed = run_approx(pg, vector, Scope(), threshold, ef)
                        latencies.append(elapsed)
                        case_hits.append(row is not None)
                        observations.append({
                            "case_id": case["id"],
                            "class": case["class"],
                            "returned": row is not None,
                            "returned_distance": None if row is None else row["distance"],
                            "returned_scope_tenant": None if row is None else row["tenant_id"],
                            "returned_prompt_hash": None if row is None else row["canonical_prompt_hash"],
                        })
                positives = [c for c in cases if c["class"] == "positive"]
                negatives = [c for c in cases if c["class"] == "hard_negative"]
                eligible = [
                    c for c in positives
                    if exact_by_case[c["id"]] is not None
                    and exact_by_case[c["id"]]["distance"] <= threshold
                ]
                exact_negative_hits = [
                    c for c in negatives
                    if exact_by_case[c["id"]] is not None
                    and exact_by_case[c["id"]]["distance"] <= threshold
                ]
                by_case = {c["id"]: [o for o in observations if o["case_id"] == c["id"]] for c in cases}
                positive_hits = sum(any(o["returned"] for o in by_case[c["id"]]) for c in eligible)
                positive_misses = len(eligible) - positive_hits
                negative_false_hits = sum(any(o["returned"] for o in by_case[c["id"]]) for c in negatives)
                results.append({
                    "ef_search": ef,
                    "threshold": threshold,
                    "positive_cases": len(positives),
                    "positive_exactly_eligible": len(eligible),
                    "positive_hits": positive_hits,
                    "positive_misses": positive_misses,
                    "positive_recall_pct": round(100 * positive_hits / len(eligible), 2) if eligible else 0.0,
                    "positive_false_miss_rate_pct": round(100 * positive_misses / len(eligible), 2) if eligible else 0.0,
                    "positive_exact_coverage_pct": round(100 * len(eligible) / len(positives), 2),
                    "negative_cases": len(negatives),
                    "negative_false_hits": negative_false_hits,
                    "negative_true_misses": len(negatives) - negative_false_hits,
                    "false_hit_rate_pct": round(100 * negative_false_hits / len(negatives), 2),
                    "exact_ground_truth_negative_hits": len(exact_negative_hits),
                    "exact_negative_hit_case_ids": [c["id"] for c in exact_negative_hits],
                    "latency_ms": {
                        "samples": len(latencies), "p50": percentile(latencies, 50),
                        "p95": percentile(latencies, 95), "p99": percentile(latencies, 99),
                        "max": round(max(latencies), 4),
                    },
                    "observed_cases": observations,
                })
        # Cross-tenant and compatibility probes use exact same vectors and the
        # same HNSW SQL, but their scope differs by exactly one boundary.
        cross_tenant: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        for case in cases:
            vector = case_vectors[case["id"]]
            with pg.transaction():
                pg.execute("SELECT set_config('hnsw.ef_search', '300', true)")
                row = pg.execute(HOT_SQL, hot_params(vector, Scope(tenant_id=TENANT_B), 0.35)).fetchone()
            cross_tenant.append({"case_id": case["id"], "returned_tenant": None if row is None else str(row[1]), "leak": row is not None and str(row[1]) != TENANT_B})
            for label, replacement in (
                ("provider", {"provider": "anthropic"}),
                ("model", {"model": "other/model"}),
                ("embedding_version", {"embedding_version": "old:embedding@1536"}),
                ("quality_version", {"quality_version": "old-quality"}),
                ("request_parameters_hash", {"request_parameters_hash": "old-params"}),
            ):
                scope_data = Scope().__dict__.copy()
                scope_data.update(replacement)
                bad_scope = Scope(**scope_data)
                with pg.transaction():
                    row = pg.execute(HOT_SQL, hot_params(vector, bad_scope, 0.35)).fetchone()
                mismatches.append({"case_id": case["id"], "mismatch": label, "returned": row is not None})
        return results, {"exact_by_case": exact_by_case, "cross_tenant": cross_tenant, "mismatches": mismatches}


def choose_point(results: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    safe = [
        r for r in results
        if r["positive_exact_coverage_pct"] >= 90.0
        and r["positive_recall_pct"] >= 95.0
        and r["false_hit_rate_pct"] == 0.0
        and r["exact_ground_truth_negative_hits"] == 0
        and r["latency_ms"]["p95"] <= 100.0
    ]
    if not safe:
        return None, "NO-GO: no grid point met exact-negative zero, HNSW false-hit zero, >=90% positive coverage, >=95% positive recall, and p95 <=100ms."
    chosen = sorted(safe, key=lambda r: (-r["positive_exact_coverage_pct"], -r["positive_recall_pct"], r["latency_ms"]["p95"], r["threshold"], r["ef_search"]))[0]
    return chosen, "GO candidate: conservative zero-false-hit point with measured positive coverage/recall and p95 under 100ms. Threshold remains provisional pending product-manager ratification."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admin-dsn", default=os.environ.get("TOKEN_SAVER_PG_BASE", ""))
    parser.add_argument("--live-dsn", default=os.environ.get("TOKEN_SAVER_PG_DSN", ""))
    parser.add_argument("--embedding-base-url", default=os.environ.get("UPSTREAM_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--embedding-api-key", default=os.environ.get("OPENROUTER_API_KEY", ""))
    parser.add_argument("--target-rows", type=int, default=10000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument(
        "--filler-pool", type=Path, default=None,
        help="Optional distinct-prompt filler pool (one prompt per line). When set, "
             "volume rows draw from pool prompts embedded by the same pinned model "
             "instead of repeating corpus vectors.")
    parser.add_argument(
        "--hnsw-ef-construction", type=int, default=0,
        help="Calibration-only control: rebuild the HNSW index with this ef_construction "
             "after seeding (bulk build). 0 (default) keeps the migration's index.")
    args = parser.parse_args()
    repo_root = (args.repo_root or Path(__file__).resolve().parents[2]).resolve()
    corpus_path = (args.corpus or repo_root / "token-saver/benchmark/fixtures/pc4_real_prompt_pairs.json").resolve()
    if not args.admin_dsn or not args.embedding_api_key:
        raise SystemExit("--admin-dsn and --embedding-api-key (or their environment variables) are required")
    if not args.live_dsn:
        raise SystemExit("--live-dsn (or TOKEN_SAVER_PG_DSN) is required for before/after evidence")
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    cases = corpus["cases"]
    prompts: list[str] = []
    for case in cases:
        for key in ("stored_prompt", "query_prompt"):
            if case[key] not in prompts:
                prompts.append(case[key])
    pool_prompts: list[str] = []
    if args.filler_pool:
        pool_prompts = [
            line.strip() for line in args.filler_pool.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if len(pool_prompts) != len(set(pool_prompts)):
            raise RunnerError("filler pool contains duplicate prompts")
        overlap = set(pool_prompts) & set(prompts)
        if overlap:
            raise RunnerError(f"filler pool overlaps corpus prompts: {sorted(overlap)[:3]}")
    live_before = db_snapshot(args.live_dsn)
    started = datetime.now(timezone.utc).isoformat()
    model_returned, embedded = embedding_request(args.embedding_base_url, args.embedding_api_key, prompts)
    vectors = dict(zip(prompts, embedded))
    pool_vectors: list[list[float]] = []
    if pool_prompts:
        _, pool_embedded = embedding_request(args.embedding_base_url, args.embedding_api_key, pool_prompts)
        pool_vectors = pool_embedded
    db_name = ""
    scratch_dsn = ""
    schema_evidence: dict[str, Any] = {}
    try:
        db_name, scratch_dsn, schema_evidence = create_scratch(args.admin_dsn, repo_root)
        seed_evidence = seed_database(scratch_dsn, vectors, cases, args.target_rows, pool_vectors or None)
        hnsw_rebuild_evidence = (
            rebuild_hnsw_index(scratch_dsn, args.hnsw_ef_construction) if args.hnsw_ef_construction else None
        )
        results, probes = calibrate(scratch_dsn, cases, vectors, args.repetitions, args.warmups)
        selected, decision = choose_point(results)
        plan_point = selected or min(results, key=lambda r: (r["false_hit_rate_pct"], r["positive_false_miss_rate_pct"], r["latency_ms"]["p95"]))
        plan_case = cases[0]
        with psycopg.connect(scratch_dsn) as pg:
            plan = explain(pg, vector_literal(vectors[plan_case["query_prompt"]]), Scope(), plan_point["threshold"], plan_point["ef_search"])
            indexes = [r[0] for r in pg.execute("SELECT indexname FROM pg_indexes WHERE tablename='semantic_cache_entries' ORDER BY indexname").fetchall()]
            table_count = int(pg.execute("SELECT count(*) FROM semantic_cache_entries").fetchone()[0])
            response_count = int(pg.execute("SELECT count(*) FROM semantic_cache_responses").fetchone()[0])
            server_version = str(pg.execute("SELECT version()").fetchone()[0])
        live_after = db_snapshot(args.live_dsn)
        output = {
            "status": "authoritative_provisional_calibration",
            "status_explanation": "Authoritative for this committed real-embedding corpus, pinned model/dimensions, isolated pgvector run, and measured grid; provisional for production because semantic caching remains disabled and product-manager threshold ratification is downstream.",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "decision": decision,
            "recommended_operating_point": selected,
            "plan_evidence_point": {"ef_search": plan_point["ef_search"], "threshold": plan_point["threshold"], "case_id": plan_case["id"]},
            "correctness_latency_bar": {"false_hit_rate_pct": 0.0, "exact_ground_truth_negative_hits": 0, "positive_exact_coverage_pct_min": 90.0, "positive_recall_pct_min": 95.0, "p95_latency_ms_max": 100.0},
            "embedding": {"requested_model": MODEL, "response_model": model_returned, "dimensions": DIMS, "embedding_version": EMBEDDING_VERSION, "provider_endpoint": args.embedding_base_url, "prompt_count": len(prompts)},
            "corpus": {"path": str(corpus_path.relative_to(repo_root)), "sha256": corpus_checksum(corpus_path), "case_count": len(cases), "positive_cases": sum(c["class"] == "positive" for c in cases), "hard_negative_cases": sum(c["class"] == "hard_negative" for c in cases), "filler_mode": "distinct_pool" if pool_vectors else "duplicate_corpus", "filler_pool": ({"path": str(args.filler_pool.relative_to(repo_root)), "sha256": corpus_checksum(args.filler_pool), "prompt_count": len(pool_prompts)} if pool_prompts else None)},
            "runtime": {"python": sys.version.split()[0], "hostname": os.uname().nodename, "kernel": os.uname().release, "machine": os.uname().machine, "target_rows": args.target_rows, "repetitions": args.repetitions, "warmups_per_case": args.warmups, "thresholds": THRESHOLDS, "ef_search_values": EF_SEARCH_VALUES, "hnsw_ef_construction": args.hnsw_ef_construction},
            "database": {"scratch_database": db_name, "server_version": server_version, "extension_and_schema": schema_evidence, "hnsw_index_rebuilt": hnsw_rebuild_evidence, "seed": seed_evidence, "post_seed_entry_count": table_count, "post_seed_response_count": response_count, "indexes": indexes, "plan": plan},
            "grid": results,
            "probes": {"cross_tenant_hit_count": sum(1 for p in probes["cross_tenant"] if p["leak"]), "cross_tenant_probe_count": len(probes["cross_tenant"]), "cross_tenant": probes["cross_tenant"], "mismatch_return_count": sum(1 for p in probes["mismatches"] if p["returned"]), "mismatch_probe_count": len(probes["mismatches"]), "mismatches": probes["mismatches"], "mandatory_filter_omission_refused_pre_query": application_guard_probe()},
            "live_safety_audit": {"before": live_before, "after": live_after, "unchanged": live_before == live_after, "semantic_cache_enabled": os.environ.get("SEMANTIC_CACHE_ENABLED", "false").lower() == "true"},
        }
        if output["database"]["extension_and_schema"].get("vector_extension_version") != "0.8.6":
            raise RunnerError("scratch extension version was not 0.8.6")
        if output["probes"]["cross_tenant_hit_count"] != 0 or output["probes"]["mismatch_return_count"] != 0:
            raise RunnerError("isolation or compatibility probe returned a row")
        if not output["live_safety_audit"]["unchanged"]:
            raise RunnerError("live before/after audit changed")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), "decision": decision, "recommended": selected, "rows": seed_evidence["row_count"]}, indent=2))
        return 0
    finally:
        if db_name:
            try:
                with psycopg.connect(args.admin_dsn, autocommit=True) as admin:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} ").format(sql.Identifier(db_name)))
            except Exception as exc:
                print(f"WARNING: failed to drop scratch database {db_name}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
