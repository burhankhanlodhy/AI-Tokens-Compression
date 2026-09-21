#!/usr/bin/env python3
"""DBA diagnostic 3 (t_5a98b8da): iterative_scan + full plan trees.

On both seeds (A: committed duplicate filler, B: distinct real-prompt pool):
  1. Full EXPLAIN node trees for the forced-HNSW path at ef=100/300/1000.
  2. Forced-HNSW recall at ef=1000 (th=0.18) with per-case latency.
  3. Recall + latency at ef=100/300/1000 with hnsw.iterative_scan=relaxed_order
     (measured as calibration evidence only; not a product change).
"""
import json
import os
import sys
import time

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R
from pc5_seed_ab_diagnostic import CASES, POSITIVES, NEGATIVES, seed_distinct, vector_literal

import psycopg
from pathlib import Path
from psycopg import sql

TH = 0.18


def set_gucs(pg, ef, iterative=None, force_hnsw=True):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    if force_hnsw:
        pg.execute("SET LOCAL enable_bitmapscan TO off")
        pg.execute("SET LOCAL enable_seqscan TO off")
    if iterative:
        pg.execute("SELECT set_config('hnsw.iterative_scan', %s, true)", (iterative,))


def plan_tree(pg, vector, scope, threshold, ef, iterative=None):
    set_gucs(pg, ef, iterative)
    raw = pg.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()[0]

    def walk(n, depth=0):
        idx = n.get("Index Name")
        line = {"d": depth, "node": n.get("Node Type"), "index": idx, "rows": n.get("Actual Rows"),
                "time": n.get("Actual Total Time")}
        out = [line]
        for c in n.get("Plans", []) or []:
            out.extend(walk(c, depth + 1))
        return out
    return {"exec_ms": raw[0]["Execution Time"], "nodes": walk(raw[0]["Plan"])}


def query_once(pg, vector, scope, threshold, ef, iterative=None):
    set_gucs(pg, ef, iterative)
    start = time.perf_counter_ns()
    row = pg.execute(R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()
    return R.row_to_dict(row), (time.perf_counter_ns() - start) / 1_000_000


def measure(scratch_dsn, vectors, label):
    scope = R.Scope()
    out = {"label": label}
    with psycopg.connect(scratch_dsn) as pg:
        # 1. full plan trees, forced HNSW
        trees = {}
        for ef in (100, 300, 1000):
            with pg.transaction():
                trees[str(ef)] = plan_tree(pg, vector_literal(vectors[POSITIVES[0]["query_prompt"]]), scope, TH, ef)
        out["forced_plan_trees"] = trees
        # 2. forced-HNSW recall at ef=1000
        hits, dets, misses = 0, [], []
        for c in POSITIVES:
            with pg.transaction():
                row, el = query_once(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, 1000)
            if row is not None:
                hits += 1
            else:
                misses.append(c["id"])
            dets.append(round(el, 2))
        out["forced_hnsw_ef1000"] = {"hits": hits, "of": len(POSITIVES), "missed": misses,
                                     "lat_med": sorted(dets)[len(dets)//2], "lat_max": max(dets)}
        # 3. iterative_scan=relaxed_order, default path (no forcing)
        it = {}
        for ef in (100, 300, 1000):
            per = {}
            for th in (0.15, 0.18, 0.22):
                hits, dets, misses, fh = 0, [], [], []
                for c in POSITIVES:
                    with pg.transaction():
                        row, el = query_once(pg, vector_literal(vectors[c["query_prompt"]]), scope, th, ef, "relaxed_order")
                    if row is not None:
                        hits += 1
                    else:
                        misses.append(c["id"])
                    dets.append(round(el, 2))
                for c in NEGATIVES:
                    with pg.transaction():
                        row, _ = query_once(pg, vector_literal(vectors[c["query_prompt"]]), scope, th, ef, "relaxed_order")
                    if row is not None:
                        fh.append(c["id"])
                per[str(th)] = {"pos_hits": hits, "of": len(POSITIVES), "missed": misses,
                                "neg_fh": fh, "lat_med": sorted(dets)[len(dets)//2],
                                "lat_p95": sorted(dets)[int(len(dets)*0.95)-1], "lat_max": max(dets)}
            # plan under iterative scan
            with pg.transaction():
                per["plan"] = plan_tree(pg, vector_literal(vectors[POSITIVES[0]["query_prompt"]]), scope, TH, ef, "relaxed_order")
            it[str(ef)] = per
        out["iterative_relaxed_default_path"] = it
    return out


def main():
    admin_dsn = sys.argv[1]
    all_prompts = []
    for c in CASES:
        for k in ("stored_prompt", "query_prompt"):
            if c[k] not in all_prompts:
                all_prompts.append(c[k])
    pool_prompts = [p for p in open("/tmp/pc5_filler_pool.txt").read().split("\n") if p.strip() and p.strip() not in all_prompts]
    _, corpus_vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], all_prompts)
    vectors = dict(zip(all_prompts, corpus_vecs))
    _, pool_vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], pool_prompts)
    pool_vectors = dict(zip(pool_prompts, pool_vecs))
    results = {}
    for label, mode in (("A_duplicate_seed", "runner"), ("B_distinct_pool_seed", "distinct")):
        db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, Path("/tmp/pc5"))
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
