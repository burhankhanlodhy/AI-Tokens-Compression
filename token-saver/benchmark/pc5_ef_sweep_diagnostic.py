#!/usr/bin/env python3
"""DBA diagnostic 5 (t_5a98b8da): ef sweep 400-900 on the healthy-graph table.

Reuses/creates /tmp/pc5_healthy_vectors.json (real embeddings of the pc5 corpus
+ 9,952 distinct filler prompts).  Rebuilds the healthy scratch, then measures
forced-HNSW recall and latency at ef in {400,500,600,700,800,900} (th=0.18) and
negative false hits. Decides whether any ef in (300,1000) meets the full bar
(recall >=95%, p95 <=100ms) on this stack.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R
from pc5_seed_ab_diagnostic import CASES, POSITIVES, NEGATIVES, vector_literal
from pc5_healthy_graph_diagnostic import build_prompts, seed_distinct_full, q

import psycopg
from pathlib import Path
from psycopg import sql

TH = 0.18
VEC_CACHE = "/tmp/pc5_healthy_vectors.json"


def get_vectors(admin_dsn):
    all_prompts = []
    for c in CASES:
        for k in ("stored_prompt", "query_prompt"):
            if c[k] not in all_prompts:
                all_prompts.append(c[k])
    gen = build_prompts()
    filler_prompts = [p for p in gen if p not in all_prompts][:9952]
    if os.path.exists(VEC_CACHE):
        cached = json.load(open(VEC_CACHE))
        if cached["corpus_prompts"] == all_prompts and cached["filler_prompts"] == filler_prompts:
            return all_prompts, filler_prompts, cached["corpus_vecs"], cached["filler_vecs"]
    vectors = {}
    batch = 994
    corpus_vecs = []
    for i in range(0, len(all_prompts), batch):
        chunk = all_prompts[i:i + batch]
        _, vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], chunk)
        corpus_vecs.extend(vecs)
    filler_vecs = []
    for i in range(0, len(filler_prompts), batch):
        chunk = filler_prompts[i:i + batch]
        _, vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], chunk)
        filler_vecs.extend(vecs)
        print(f"embedded filler {i + len(chunk)}/{len(filler_prompts)}", flush=True)
    json.dump({"corpus_prompts": all_prompts, "filler_prompts": filler_prompts,
               "corpus_vecs": corpus_vecs, "filler_vecs": filler_vecs}, open(VEC_CACHE, "w"))
    return all_prompts, filler_prompts, corpus_vecs, filler_vecs


def main():
    admin_dsn = sys.argv[1]
    all_prompts, filler_prompts, corpus_vecs, filler_vecs = get_vectors(admin_dsn)
    vectors = dict(zip(all_prompts, corpus_vecs))
    db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, Path("/tmp/pc5"))
    out = {}
    try:
        rows = seed_distinct_full(scratch_dsn, vectors, filler_vecs, 10000)
        out["rows"] = rows
        print("seeded", rows, flush=True)
        scope = R.Scope()
        with psycopg.connect(scratch_dsn) as pg:
            sweep = {}
            for ef in (400, 500, 600, 700, 800, 900):
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
                sweep[str(ef)] = {"pos_hits": hits, "missed": misses, "neg_fh": neg_fh,
                                  "lat_med": dets[len(dets)//2], "lat_p95": dets[int(len(dets)*0.95)-1],
                                  "lat_max": dets[-1]}
                print(f"ef={ef}: {hits}/24 negFH={neg_fh} med={dets[len(dets)//2]} p95={dets[int(len(dets)*0.95)-1]} max={dets[-1]}", flush=True)
            out["ef_sweep"] = sweep
            # repeat the winner 3x for stability at th=0.18 and 0.22
            for ef in (500, 600, 700, 800):
                runs = []
                for rep in range(3):
                    hits, dets = 0, []
                    for c in POSITIVES:
                        with pg.transaction():
                            row, el = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, True)
                        hits += row is not None
                        dets.append(round(el, 2))
                    runs.append({"hits": hits, "p95": sorted(dets)[int(len(dets)*0.95)-1]})
                out[f"stability_ef{ef}"] = runs
                print(f"stability ef={ef}: {runs}", flush=True)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
