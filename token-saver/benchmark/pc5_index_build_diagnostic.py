#!/usr/bin/env python3
"""DBA diagnostic 7 (t_5a98b8da): HNSW index-build quality control.

Same healthy distinct seed; the HNSW index is rebuilt in scratch with higher
build parameters (no production change):
  B1: m=16, ef_construction=200
  B2: m=32, ef_construction=200
Sweeps forced-HNSW (only-index) recall/latency at ef=100..500, th=0.18.
Decides whether the recall deficit at ef<=700 is an index-build artifact
(fixable by DDL ratification) or intrinsic.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R
from pc5_seed_ab_diagnostic import POSITIVES, NEGATIVES, vector_literal
from pc5_seed_ab_diagnostic import seed_distinct as seed_b
from pc5_ef_sweep_diagnostic import get_vectors
from pc5_true_hnsw_diagnostic import q

import psycopg
from pathlib import Path
from psycopg import sql

TH = 0.18


def rebuild_hnsw(scratch_dsn, m, ef_c):
    with psycopg.connect(scratch_dsn, autocommit=True) as pg:
        pg.execute("DROP INDEX IF EXISTS idx_semantic_cache_embedding_hnsw")
        assert m in (16, 32) and ef_c == 200
        pg.execute(
            f"CREATE INDEX idx_semantic_cache_embedding_hnsw ON semantic_cache_entries "
            f"USING hnsw (embedding vector_cosine_ops) WITH (m={m}, ef_construction={ef_c})")
        pg.execute("DROP INDEX IF EXISTS idx_semantic_cache_expiry")
        pg.execute("DROP INDEX IF EXISTS idx_semantic_cache_scope")


def sweep(pg, scope, vectors, efs):
    out = {}
    for ef in efs:
        hits, dets, misses = 0, [], []
        for c in POSITIVES:
            with pg.transaction():
                row, el = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, True)
            hits += row is not None
            if row is None:
                misses.append(c["id"])
            dets.append(round(el, 2))
        neg_fh = 0
        for c in NEGATIVES:
            with pg.transaction():
                row, _ = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, True)
            neg_fh += row is not None
        dets.sort()
        out[str(ef)] = {"pos_hits": hits, "missed": misses, "neg_fh": neg_fh,
                        "lat_med": dets[len(dets)//2], "lat_p95": dets[int(len(dets)*0.95)-1],
                        "lat_max": dets[-1]}
        print(f"  ef={ef}: {hits}/24 negFH={neg_fh} med={dets[len(dets)//2]} p95={dets[int(len(dets)*0.95)-1]}", flush=True)
    return out


def main():
    admin_dsn = sys.argv[1]
    all_prompts, filler_prompts, corpus_vecs, filler_vecs = get_vectors(admin_dsn)
    vectors = dict(zip(all_prompts, corpus_vecs))
    scope = R.Scope()
    results = {}
    for label, m, efc in (("B1_m16_efc200", 16, 200), ("B2_m32_efc200", 32, 200)):
        db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, Path("/tmp/pc5"))
        try:
            seed_b(scratch_dsn, vectors, filler_vecs, 10000)
            t0 = time.time()
            rebuild_hnsw(scratch_dsn, m, efc)
            build_s = round(time.time() - t0, 1)
            print(f"{label}: index rebuilt in {build_s}s", flush=True)
            with psycopg.connect(scratch_dsn) as pg:
                pg.execute("SET LOCAL enable_seqscan TO off")  # no-op for txn scope; kept explicit per txn in q()
                res = sweep(pg, scope, vectors, [100, 200, 300, 400, 500])
                res["index_build_seconds"] = build_s
            results[label] = res
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
