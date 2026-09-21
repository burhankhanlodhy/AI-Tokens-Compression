#!/usr/bin/env python3
"""DBA diagnostic 6 (t_5a98b8da): true HNSW curve vs planner curve.

Scratch C1: healthy distinct seed, idx_semantic_cache_expiry and
idx_semantic_cache_scope DROPPED, enable_seqscan off -> the ONLY possible plan
is the HNSW index.  Sweep ef=100..1000: per-positive hit/miss + latency, negFH.
Scratch C2: same seed with all indexes: default-path sweep (planner choice) at
the same ef values, with the plan's index recorded per ef.
Uses cached real embeddings (pc5 corpus + 9,952 distinct filler prompts).
"""
import json
import os
import sys
import time

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R
from pc5_seed_ab_diagnostic import POSITIVES, NEGATIVES, vector_literal
from pc5_seed_ab_diagnostic import seed_distinct as seed_b
from pc5_healthy_graph_diagnostic import build_prompts
from pc5_ef_sweep_diagnostic import get_vectors

import psycopg
from pathlib import Path
from psycopg import sql

TH = 0.18


def q(pg, vector, scope, threshold, ef, force_hnsw):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    if force_hnsw:
        pg.execute("SET LOCAL enable_bitmapscan TO off")
        pg.execute("SET LOCAL enable_seqscan TO off")
    start = time.perf_counter_ns()
    row = pg.execute(R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()
    return R.row_to_dict(row), (time.perf_counter_ns() - start) / 1_000_000


def plan_indexes(pg, vector, scope, threshold, ef, force_hnsw):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    if force_hnsw:
        pg.execute("SET LOCAL enable_bitmapscan TO off")
        pg.execute("SET LOCAL enable_seqscan TO off")
    raw = pg.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()[0]
    found = {"exec_ms": raw[0]["Execution Time"], "hnsw": False, "expiry": False, "scope": False, "sort": False}

    def walk(n):
        i = n.get("Index Name") or ""
        if i == "idx_semantic_cache_embedding_hnsw": found["hnsw"] = True
        if i == "idx_semantic_cache_expiry": found["expiry"] = True
        if i == "idx_semantic_cache_scope": found["scope"] = True
        if n.get("Node Type") == "Sort": found["sort"] = True
        for c in n.get("Plans", []) or []:
            walk(c)
    walk(raw[0]["Plan"])
    return found


def sweep(pg, scope, vectors, ef_values, force_hnsw):
    out = {}
    for ef in ef_values:
        hits, dets, misses = 0, [], []
        for c in POSITIVES:
            with pg.transaction():
                row, el = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, force_hnsw)
            hits += row is not None
            if row is None:
                misses.append(c["id"])
            dets.append(round(el, 2))
        neg_fh = 0
        for c in NEGATIVES:
            with pg.transaction():
                row, _ = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, force_hnsw)
            neg_fh += row is not None
        dets.sort()
        out[str(ef)] = {"pos_hits": hits, "missed": misses, "neg_fh": neg_fh,
                        "lat_med": dets[len(dets)//2], "lat_p95": dets[int(len(dets)*0.95)-1],
                        "lat_max": dets[-1]}
        print(f"ef={ef} forced={force_hnsw}: {hits}/24 negFH={neg_fh} med={dets[len(dets)//2]} p95={dets[int(len(dets)*0.95)-1]}", flush=True)
    return out


def main():
    admin_dsn = sys.argv[1]
    all_prompts, filler_prompts, corpus_vecs, filler_vecs = get_vectors(admin_dsn)
    vectors = dict(zip(all_prompts, corpus_vecs))
    scope = R.Scope()
    results = {}
    # C1: HNSW-only table
    db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, Path("/tmp/pc5"))
    try:
        seed_b(scratch_dsn, vectors, filler_vecs, 10000)
        with psycopg.connect(scratch_dsn, autocommit=True) as pg:
            pg.execute("DROP INDEX idx_semantic_cache_expiry")
            pg.execute("DROP INDEX idx_semantic_cache_scope")
        with psycopg.connect(scratch_dsn) as pg:
            results["C1_hnsw_only"] = sweep(pg, scope, vectors, [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], True)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    # C2: full indexes, planner choice
    db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, Path("/tmp/pc5"))
    try:
        seed_b(scratch_dsn, vectors, filler_vecs, 10000)
        with psycopg.connect(scratch_dsn) as pg:
            res = sweep(pg, scope, vectors, [100, 300, 500, 700, 800, 900, 1000], False)
            for ef, m in res.items():
                with pg.transaction():
                    m["plan"] = plan_indexes(pg, vector_literal(vectors[POSITIVES[0]["query_prompt"]]), scope, TH, int(ef), False)
            results["C2_planner_choice"] = res
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
