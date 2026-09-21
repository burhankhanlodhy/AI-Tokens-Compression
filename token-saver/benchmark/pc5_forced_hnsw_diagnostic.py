#!/usr/bin/env python3
"""DBA diagnostic 2 (t_5a98b8da): TRUE HNSW-path recall, A/B seed.

Forces the HNSW index path (enable_bitmapscan=off, enable_seqscan=off) and
measures per-positive hit/miss and latency at ef in {100,300,1000} on the pc5
corpus, for both the committed duplicate seed and the distinct real-prompt
pool seed. Also records negative false hits under the forced path.
"""
import json
import sys

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R
from pc5_seed_ab_diagnostic import CASES, POSITIVES, NEGATIVES, seed_distinct, vector_literal

import psycopg
from psycopg import sql

EF_VALUES = [100, 300, 1000]
THS = [0.15, 0.18, 0.22, 0.25, 0.30]


def forced_hnsw(pg, vector, scope, threshold, ef):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    pg.execute("SET LOCAL enable_bitmapscan TO off")
    pg.execute("SET LOCAL enable_seqscan TO off")
    start = __import__("time").perf_counter_ns()
    row = pg.execute(R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()
    elapsed = (__import__("time").perf_counter_ns() - start) / 1_000_000
    return R.row_to_dict(row), elapsed


def forced_plan(pg, vector, scope, threshold, ef):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    pg.execute("SET LOCAL enable_bitmapscan TO off")
    pg.execute("SET LOCAL enable_seqscan TO off")
    raw = pg.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()[0]
    return {"exec_ms": raw[0]["Execution Time"], "top": raw[0]["Plan"].get("Node Type"),
            "index": raw[0]["Plan"].get("Index Name") or
                     (raw[0]["Plan"].get("Plans") and [p.get("Index Name") for p in raw[0]["Plan"]["Plans"] if p.get("Index Name")])}


def measure(scratch_dsn, vectors, label):
    scope = R.Scope()
    out = {"label": label}
    with psycopg.connect(scratch_dsn) as pg:
        res = {}
        for ef in EF_VALUES:
            per_th = {}
            for th in THS:
                hits, misses, dets = 0, [], []
                for c in POSITIVES:
                    vector = vector_literal(vectors[c["query_prompt"]])
                    with pg.transaction():
                        row, elapsed = forced_hnsw(pg, vector, scope, th, ef)
                    if row is not None:
                        hits += 1
                    else:
                        misses.append(c["id"])
                    dets.append(round(elapsed, 3))
                fh = 0
                fh_ids = []
                for c in NEGATIVES:
                    with pg.transaction():
                        row, _ = forced_hnsw(pg, vector_literal(vectors[c["query_prompt"]]), scope, th, ef)
                    if row is not None:
                        fh += 1
                        fh_ids.append(c["id"])
                per_th[str(th)] = {"pos_hits": hits, "of": len(POSITIVES), "missed": misses,
                                   "neg_false_hits": fh, "neg_fh_ids": fh_ids,
                                   "lat_med": sorted(dets)[len(dets)//2], "lat_p95": sorted(dets)[int(len(dets)*0.95)-1],
                                   "lat_max": max(dets)}
            res[str(ef)] = per_th
            plan = None
            with pg.transaction():
                plan = forced_plan(pg, vector_literal(vectors[POSITIVES[0]["query_prompt"]]), scope, THS[1], ef)
            res[str(ef)]["plan_check"] = plan
        out["forced_hnsw"] = res
    return out


def main():
    admin_dsn = sys.argv[1]
    all_prompts = []
    for c in CASES:
        for k in ("stored_prompt", "query_prompt"):
            if c[k] not in all_prompts:
                all_prompts.append(c[k])
    pool_prompts = [p for p in open("/tmp/pc5_filler_pool.txt").read().split("\n") if p.strip() and p.strip() not in all_prompts]
    import os
    _, corpus_vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], all_prompts)
    vectors = dict(zip(all_prompts, corpus_vecs))
    _, pool_vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], pool_prompts)
    pool_vectors = dict(zip(pool_prompts, pool_vecs))
    results = {}
    for label, mode in (("A_duplicate_seed", "runner"), ("B_distinct_pool_seed", "distinct")):
        db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, __import__("pathlib").Path("/tmp/pc5"))
        try:
            if mode == "runner":
                R.seed_database(scratch_dsn, vectors, CASES, 10000)
            else:
                seed_distinct(scratch_dsn, vectors, [pool_vectors[p] for p in pool_prompts], 10000)
            print(f"{label}: seeded", flush=True)
            results[label] = measure(scratch_dsn, vectors, label)
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
