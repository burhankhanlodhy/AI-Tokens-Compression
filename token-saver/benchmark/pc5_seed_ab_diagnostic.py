#!/usr/bin/env python3
"""DBA diagnostic (t_5a98b8da): A/B the harness seed under the committed SQL.

Scratch A: committed runner's seed (filler = exact copies of the 48 corpus
stored vectors, ~208 copies each).  Scratch B: traffic-shaped filler of
DISTINCT real prompts (600 authored support/API/dev questions, embedded by the
same pinned model, cycled to 10k rows; no filler vector duplicates a corpus
vector).  Same corpus, same scope structure, same production HOT_SQL.

Measures per scratch: exact ground truth per positive (forced scan), HNSW
hit/miss at ef in {100,300,1000} at threshold 0.18, EXPLAIN plan shape per ef,
pgvector GUC defaults.  Diagnostic only: creates and drops its scratch DBs and
never touches the live application database.
"""
import json
import sys

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R

import psycopg
from psycopg import sql

DIMS = 1536
MODEL = "text-embedding-3-small"
EF_VALUES = [100, 300, 1000]
TH = 0.18

CORPUS = json.load(open("/tmp/pc5/token-saver/benchmark/fixtures/pc5_real_paraphrase_pairs_v2.json"))
CASES = CORPUS["cases"]
POSITIVES = [c for c in CASES if c["class"] == "positive"]
NEGATIVES = [c for c in CASES if c["class"] == "hard_negative"]
POOL = [l.strip() for l in open("/tmp/pc5_filler_pool.txt") if l.strip()]
assert len(POOL) == len(set(POOL))


def vector_literal(v):
    return R.vector_literal(v)


def seed_distinct(dsn, vectors, pool_vectors, target_rows=10000):
    """Same structure as R.seed_database but filler rows draw from pool_vectors."""
    stored_vectors = [vectors[c["stored_prompt"]] for c in CASES]
    with psycopg.connect(dsn, autocommit=True) as pg:
        body = b'{"calibration":true}'
        digest = __import__("hashlib").sha256(body).hexdigest()
        with pg.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO semantic_cache_responses
                  (tenant_id, response_ref, payload, payload_bytes, schema_version, sha256, content_length, expires_at)
                VALUES (%s, %s, %s::jsonb, %s, 1, %s, %s, now() + interval '1 hour')
                """,
                [(t, ref, body.decode(), psycopg.Binary(body), digest, len(body))
                 for t, ref in ((R.TENANT_A, "calibration-response-a"), (R.TENANT_B, "calibration-response-b"),
                                (R.TENANT_C, "calibration-response-c"))],
            )
        pg.execute("CREATE TEMP TABLE calibration_vectors (slot integer PRIMARY KEY, embedding vector(1536) NOT NULL)")
        pg.execute("CREATE TEMP TABLE pool_vectors (slot integer PRIMARY KEY, embedding vector(1536) NOT NULL)")
        with pg.cursor() as cur:
            cur.executemany("INSERT INTO calibration_vectors (slot, embedding) VALUES (%s, %s::vector)",
                            [(i + 1, vector_literal(v)) for i, v in enumerate(stored_vectors)])
            cur.executemany("INSERT INTO pool_vectors (slot, embedding) VALUES (%s, %s::vector)",
                            [(i + 1, vector_literal(v)) for i, v in enumerate(pool_vectors)])
        provider_ids = {row[0]: int(row[1]) for row in pg.execute("SELECT name, id FROM providers").fetchall()}
        scope = R.CASE_SCOPE
        with pg.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO semantic_cache_entries
                  (tenant_id, provider_id, model, embedding_model, embedding_dimensions,
                   embedding_version, quality_version, request_parameters_hash,
                   canonical_prompt_hash, embedding, response_ref, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,now()+interval '1 hour')
                """,
                [(R.TENANT_A, provider_ids["openai"], scope[0], MODEL, DIMS, scope[3], scope[4], scope[5],
                  f"corpus-{c['id']}", vector_literal(vectors[c["stored_prompt"]]), "calibration-response-a")
                 for c in CASES],
            )
        filler_target = target_rows - len(CASES)
        pg.execute(
            """
            INSERT INTO semantic_cache_entries
              (tenant_id, provider_id, model, embedding_model, embedding_dimensions,
               embedding_version, quality_version, request_parameters_hash,
               canonical_prompt_hash, embedding, response_ref, expires_at)
            SELECT %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                   'traffic-pool-' || g, p.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, %s) AS x(g)
              JOIN pool_vectors p ON p.slot = ((x.g - 1) %% %s) + 1
            """,
            (R.TENANT_A, provider_ids["openai"], scope[0], MODEL, DIMS, scope[3], scope[4], scope[5],
             "calibration-response-a", filler_target, len(pool_vectors)),
        )
        pg.execute(
            """
            INSERT INTO semantic_cache_entries
              (tenant_id, provider_id, model, embedding_model, embedding_dimensions,
               embedding_version, quality_version, request_parameters_hash,
               canonical_prompt_hash, embedding, response_ref, expires_at)
            SELECT %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                   'traffic-tenant-b-' || g, p.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, 500) AS x(g)
              JOIN pool_vectors p ON p.slot = ((x.g - 1) %% %s) + 1
            UNION ALL
            SELECT %s::uuid, %s, 'other/model', %s, %s, %s, %s, %s,
                   'traffic-model-' || g, p.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, 500) AS x(g)
              JOIN pool_vectors p ON p.slot = ((x.g - 1) %% %s) + 1
            UNION ALL
            SELECT %s::uuid, %s, %s, %s, %s, 'old:embedding@1536', 'old-quality', 'old-params',
                   'traffic-mismatch-' || g, p.embedding, %s, now()+interval '1 hour'
              FROM generate_series(1, 500) AS x(g)
              JOIN pool_vectors p ON p.slot = ((x.g - 1) %% %s) + 1
            """,
            (R.TENANT_B, provider_ids["openai"], scope[0], MODEL, DIMS, scope[3], scope[4], scope[5],
             "calibration-response-b", len(pool_vectors),
             R.TENANT_A, provider_ids["openrouter"], MODEL, DIMS, scope[3], scope[4], scope[5],
             "calibration-response-a", len(pool_vectors),
             R.TENANT_A, provider_ids["openai"], scope[0], MODEL, DIMS,
             "calibration-response-a", len(pool_vectors)),
        )
        pg.execute("ANALYZE semantic_cache_entries")
        return int(pg.execute("SELECT count(*) FROM semantic_cache_entries").fetchone()[0])


def explain_shape(pg, vector, scope, threshold, ef):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    raw = pg.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()[0]
    plan = raw[0]["Plan"]

    def walk(n, out):
        out.append({"node": n.get("Node Type"), "index": n.get("Index Name"),
                    "rows": n.get("Actual Rows"), "removed": n.get("Rows Removed by Filter"),
                    "time": n.get("Actual Total Time")})
        for c in n.get("Plans", []) or []:
            walk(c, out)
    nodes = []
    walk(plan, nodes)
    return {"exec_ms": raw[0]["Execution Time"], "nodes": nodes}


def measure(scratch_dsn, vectors, label):
    scope = R.Scope()
    out = {"label": label}
    with psycopg.connect(scratch_dsn) as pg:
        gucs = {}
        for name in ("hnsw.ef_search", "hnsw.iterative_scan", "hnsw.scan_pro_mem_factor",
                     "max_parallel_workers_per_gather", "shared_buffers", "work_mem"):
            try:
                gucs[name] = pg.execute(f"SHOW {name}").fetchone()[0]
            except psycopg.errors.UndefinedObject:
                pg.rollback()
                gucs[name] = "<unset until vector loaded>"
        out["gucs"] = gucs
        exact = {}
        for c in POSITIVES:
            with pg.transaction():
                exact[c["id"]] = R.run_exact(pg, vector_literal(vectors[c["query_prompt"]]), scope)
        out["exact_pos_distances"] = {k: (None if v is None else round(v["distance"], 4)) for k, v in exact.items()}
        recall = {}
        for ef in EF_VALUES:
            hits, misses, dets = 0, [], []
            for c in POSITIVES:
                vector = vector_literal(vectors[c["query_prompt"]])
                with pg.transaction():
                    R.run_approx(pg, vector, scope, TH, ef)  # warm
                    row, elapsed = R.run_approx(pg, vector, scope, TH, ef)
                if row is not None:
                    hits += 1
                else:
                    misses.append(c["id"])
                dets.append(round(elapsed, 3))
            recall[str(ef)] = {"hits": hits, "of": len(POSITIVES), "missed": misses,
                               "lat_ms_min": min(dets), "lat_ms_med": sorted(dets)[len(dets)//2], "lat_ms_max": max(dets)}
        out["hnsw_recall_at_" + str(TH)] = recall
        neg_hits = {}
        for ef in EF_VALUES:
            fh = 0
            for c in NEGATIVES:
                with pg.transaction():
                    row, _ = R.run_approx(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef)
                if row is not None:
                    fh += 1
            neg_hits[str(ef)] = fh
        out["hnsw_negative_false_hits_at_" + str(TH)] = neg_hits
        sample_case = next(c for c in POSITIVES if exact[c["id"]] and exact[c["id"]]["distance"] <= TH)
        shapes = {}
        for ef in EF_VALUES:
            with pg.transaction():
                shapes[str(ef)] = explain_shape(pg, vector_literal(vectors[sample_case["query_prompt"]]), scope, TH, ef)
        out["explain_sample_" + sample_case["id"]] = shapes
    return out


def main():
    admin_dsn = sys.argv[1]
    all_prompts = []
    for c in CASES:
        for k in ("stored_prompt", "query_prompt"):
            if c[k] not in all_prompts:
                all_prompts.append(c[k])
    pool_prompts = [p for p in POOL if p not in all_prompts]
    print(f"corpus prompts: {len(all_prompts)}, pool prompts used: {len(pool_prompts)}", flush=True)
    # one real embedding call per batch (OpenAI-compatible endpoint dedupes identical inputs)
    _, corpus_vecs = R.embedding_request("https://openrouter.ai/api/v1", __import__("os").environ["OPENROUTER_API_KEY"], all_prompts)
    vectors = dict(zip(all_prompts, corpus_vecs))
    _, pool_vecs = R.embedding_request("https://openrouter.ai/api/v1", __import__("os").environ["OPENROUTER_API_KEY"], pool_prompts)
    pool_vectors = dict(zip(pool_prompts, pool_vecs))
    assert len(pool_vectors) == len(pool_prompts)
    results = {}
    for label, seeder in (("A_duplicate_seed", "runner"), ("B_distinct_pool_seed", "distinct")):
        db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, __import__("pathlib").Path("/tmp/pc5"))
        try:
            if seeder == "runner":
                seed = R.seed_database(scratch_dsn, vectors, CASES, 10000)
            else:
                rows = seed_distinct(scratch_dsn, vectors, [pool_vectors[p] for p in pool_prompts], 10000)
                seed = {"row_count": rows}
            print(f"{label}: seeded {seed['row_count']} rows", flush=True)
            results[label] = measure(scratch_dsn, vectors, label)
            results[label]["rows"] = seed["row_count"]
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
