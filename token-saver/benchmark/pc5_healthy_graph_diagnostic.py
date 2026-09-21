#!/usr/bin/env python3
"""DBA diagnostic 4 (t_5a98b8da): healthy-graph control.

Scratch C: 48 corpus vectors + 9,952 DISTINCT real prompts (combinatorial but
genuine question texts, embedded by the pinned model) + selectivity groups.
Measures forced-HNSW recall and default-path latency/recall at ef 100/300/1000,
plus plan trees. Decides whether HNSW recall collapse at 10k rows is a filler
artifact or intrinsic to pgvector 0.8.6 on this stack.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/tmp/pc5/token-saver/benchmark")
import run_pc4_real_embeddings as R
from pc5_seed_ab_diagnostic import CASES, POSITIVES, NEGATIVES, vector_literal

import psycopg
from pathlib import Path
from psycopg import sql

TH = 0.18

VERBS = ["reset", "change", "update", "delete", "remove", "create", "add", "enable", "disable", "view",
         "download", "upload", "share", "export", "import", "configure", "set up", "install", "renew", "rotate",
         "cancel", "pause", "restore", "recover", "archive", "duplicate", "transfer", "merge", "invite", "block",
         "unblock", "rename", "move", "copy", "sync", "verify", "validate", "test", "monitor", "limit"]
OBJECTS = ["my password", "my profile photo", "my billing address", "my payment card", "my email address",
           "my API key", "my webhook endpoint", "my SSH key", "my domain", "my DNS records",
           "my SSL certificate", "my firewall rules", "my database backup", "my container image",
           "my Kubernetes cluster", "my CI pipeline", "my build artifacts", "my test coverage",
           "my documentation", "my team members", "my project board", "my task list", "my notifications",
           "my email filters", "my calendar integration", "my storage quota", "my usage reports",
           "my audit logs", "my access tokens", "my service account", "my load balancer", "my CDN settings",
           "my rate limits", "my error alerts", "my status page", "my monitoring dashboards",
           "my data retention policy", "my encryption keys", "my two-factor settings", "my recovery codes",
           "my subscription plan", "my invoice details", "my refund request", "my order history",
           "my shipping preferences", "my saved addresses", "my wishlist", "my reviews",
           "my support tickets", "my feedback submissions"]
FORMS = ["How do I {v} {o}?",
         "What is the best way to {v} {o}?",
         "Can you walk me through how to {v} {o}?",
         "I need to {v} {o}, where do I start?",
         "Where do I find the option to {v} {o}?"]


def build_prompts():
    seen, out = set(), []
    for v in VERBS:
        for o in OBJECTS:
            for f in FORMS:
                s = f.format(v=v, o=o)
                if s not in seen:
                    seen.add(s)
                    out.append(s)
    return out


def set_gucs(pg, ef, force_hnsw):
    pg.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(ef),))
    pg.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
    if force_hnsw:
        pg.execute("SET LOCAL enable_bitmapscan TO off")
        pg.execute("SET LOCAL enable_seqscan TO off")


def q(pg, vector, scope, threshold, ef, force_hnsw):
    set_gucs(pg, ef, force_hnsw)
    start = time.perf_counter_ns()
    row = pg.execute(R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()
    return R.row_to_dict(row), (time.perf_counter_ns() - start) / 1_000_000


def plan_tree(pg, vector, scope, threshold, ef, force_hnsw):
    set_gucs(pg, ef, force_hnsw)
    raw = pg.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + R.HOT_SQL, R.hot_params(vector, scope, threshold)).fetchone()[0]

    def walk(n, d=0):
        out = [{"d": d, "node": n.get("Node Type"), "index": n.get("Index Name"),
                "rows": n.get("Actual Rows"), "time": n.get("Actual Total Time")}]
        for c in n.get("Plans", []) or []:
            out.extend(walk(c, d + 1))
        return out
    return {"exec_ms": raw[0]["Execution Time"], "nodes": walk(raw[0]["Plan"])}


def seed_distinct_full(scratch_dsn, vectors, filler_vectors, target_rows=10000):
    stored = [vectors[c["stored_prompt"]] for c in CASES]
    import hashlib
    with psycopg.connect(scratch_dsn, autocommit=True) as pg:
        body = b'{"calibration":true}'
        digest = hashlib.sha256(body).hexdigest()
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO semantic_cache_responses (tenant_id, response_ref, payload, payload_bytes, schema_version, sha256, content_length, expires_at) VALUES (%s,%s,%s::jsonb,%s,1,%s,%s, now()+interval '1 hour')",
                [(t, ref, body.decode(), psycopg.Binary(body), digest, len(body))
                 for t, ref in ((R.TENANT_A, "calibration-response-a"), (R.TENANT_B, "calibration-response-b"), (R.TENANT_C, "calibration-response-c"))])
        pg.execute("CREATE TEMP TABLE cal_vec (slot int PRIMARY KEY, embedding vector(1536) NOT NULL)")
        pg.execute("CREATE TEMP TABLE fill_vec (slot int PRIMARY KEY, embedding vector(1536) NOT NULL)")
        with pg.cursor() as cur:
            cur.executemany("INSERT INTO cal_vec (slot, embedding) VALUES (%s,%s::vector)",
                            [(i + 1, vector_literal(v)) for i, v in enumerate(stored)])
            cur.executemany("INSERT INTO fill_vec (slot, embedding) VALUES (%s,%s::vector)",
                            [(i + 1, vector_literal(v)) for i, v in enumerate(filler_vectors)])
        pids = {r[0]: int(r[1]) for r in pg.execute("SELECT name, id FROM providers").fetchall()}
        sc = R.CASE_SCOPE
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO semantic_cache_entries (tenant_id, provider_id, model, embedding_model, embedding_dimensions, embedding_version, quality_version, request_parameters_hash, canonical_prompt_hash, embedding, response_ref, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,now()+interval '1 hour')",
                [(R.TENANT_A, pids["openai"], sc[0], R.MODEL, 1536, sc[3], sc[4], sc[5], f"corpus-{c['id']}",
                  vector_literal(vectors[c["stored_prompt"]]), "calibration-response-a") for c in CASES])
        filler_target = target_rows - len(CASES)
        nfill = len(filler_vectors)
        pg.execute(
            """INSERT INTO semantic_cache_entries
               (tenant_id, provider_id, model, embedding_model, embedding_dimensions, embedding_version,
                quality_version, request_parameters_hash, canonical_prompt_hash, embedding, response_ref, expires_at)
               SELECT %s::uuid,%s,%s,%s,%s,%s,%s,%s,'traffic-distinct-'||g, f.embedding,%s,now()+interval '1 hour'
               FROM generate_series(1,%s) x(g) JOIN fill_vec f ON f.slot = x.g""",
            (R.TENANT_A, pids["openai"], sc[0], R.MODEL, 1536, sc[3], sc[4], sc[5], "calibration-response-a",
             filler_target))
        assert filler_target <= nfill, (filler_target, nfill)
        pg.execute(
            """INSERT INTO semantic_cache_entries
               (tenant_id, provider_id, model, embedding_model, embedding_dimensions, embedding_version,
                quality_version, request_parameters_hash, canonical_prompt_hash, embedding, response_ref, expires_at)
               SELECT %s::uuid,%s,%s,%s,%s,%s,%s,%s,'traffic-b-'||g, f.embedding,%s,now()+interval '1 hour'
               FROM generate_series(1,500) x(g) JOIN fill_vec f ON f.slot = x.g
               UNION ALL
               SELECT %s::uuid,%s,'other/model',%s,%s,%s,%s,%s,'traffic-m-'||g, f.embedding,%s,now()+interval '1 hour'
               FROM generate_series(501,1000) x(g) JOIN fill_vec f ON f.slot = x.g
               UNION ALL
               SELECT %s::uuid,%s,%s,%s,%s,'old:embedding@1536','old-quality','old-params','traffic-v-'||g, f.embedding,%s,now()+interval '1 hour'
               FROM generate_series(1001,1500) x(g) JOIN fill_vec f ON f.slot = x.g""",
            (R.TENANT_B, pids["openai"], sc[0], R.MODEL, 1536, sc[3], sc[4], sc[5], "calibration-response-b",
             R.TENANT_A, pids["openrouter"], R.MODEL, 1536, sc[3], sc[4], sc[5], "calibration-response-a",
             R.TENANT_A, pids["openai"], sc[0], R.MODEL, 1536, "calibration-response-a"))
        pg.execute("ANALYZE semantic_cache_entries")
        return int(pg.execute("SELECT count(*) FROM semantic_cache_entries").fetchone()[0])


def main():
    admin_dsn = sys.argv[1]
    all_prompts = []
    for c in CASES:
        for k in ("stored_prompt", "query_prompt"):
            if c[k] not in all_prompts:
                all_prompts.append(c[k])
    gen = build_prompts()
    filler_prompts = [p for p in gen if p not in all_prompts][:9952]
    assert len(filler_prompts) == 9952, len(filler_prompts)
    print(f"corpus prompts {len(all_prompts)}, distinct filler {len(filler_prompts)}", flush=True)
    batch = 994
    vectors = {}
    for i in range(0, len(all_prompts), batch):
        chunk = all_prompts[i:i + batch]
        _, vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], chunk)
        vectors.update(dict(zip(chunk, vecs)))
        print(f"embedded corpus batch {i}//{len(all_prompts)}", flush=True)
    filler_vectors = []
    for i in range(0, len(filler_prompts), batch):
        chunk = filler_prompts[i:i + batch]
        _, vecs = R.embedding_request("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"], chunk)
        filler_vectors.extend(vecs)
        print(f"embedded filler {i + len(chunk)}/{len(filler_prompts)}", flush=True)
    db_name, scratch_dsn, _ = R.create_scratch(admin_dsn, Path("/tmp/pc5"))
    out = {}
    try:
        rows = seed_distinct_full(scratch_dsn, vectors, filler_vectors, 10000)
        out["rows"] = rows
        print("seeded", rows, flush=True)
        scope = R.Scope()
        with psycopg.connect(scratch_dsn) as pg:
            # exact ground truth
            exact = {}
            for c in POSITIVES:
                with pg.transaction():
                    exact[c["id"]] = R.run_exact(pg, vector_literal(vectors[c["query_prompt"]]), scope)
            out["exact_pos"] = {k: (None if v is None else round(v["distance"], 4)) for k, v in exact.items()}
            # forced-HNSW recall per ef
            fh = {}
            for ef in (100, 300, 1000):
                hits, dets, misses = 0, [], []
                for c in POSITIVES:
                    with pg.transaction():
                        row, el = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, True)
                    hits += row is not None
                    if row is None:
                        misses.append(c["id"])
                    dets.append(round(el, 2))
                fh[str(ef)] = {"hits": hits, "missed": misses, "lat_med": sorted(dets)[len(dets)//2], "lat_max": max(dets)}
            out["forced_hnsw"] = fh
            # default-path (planner choice) recall + latency per ef
            dp = {}
            for ef in (100, 300, 1000):
                hits, dets, misses = 0, [], []
                for c in POSITIVES:
                    with pg.transaction():
                        row, el = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, False)
                    hits += row is not None
                    if row is None:
                        misses.append(c["id"])
                    dets.append(round(el, 2))
                neg_fh = 0
                for c in NEGATIVES:
                    with pg.transaction():
                        row, _ = q(pg, vector_literal(vectors[c["query_prompt"]]), scope, TH, ef, False)
                    neg_fh += row is not None
                with pg.transaction():
                    plan = plan_tree(pg, vector_literal(vectors[POSITIVES[0]["query_prompt"]]), scope, TH, ef, False)
                dp[str(ef)] = {"pos_hits": hits, "missed": misses, "neg_fh": neg_fh,
                               "lat_med": sorted(dets)[len(dets)//2], "lat_p95": sorted(dets)[int(len(dets)*0.95)-1],
                               "lat_max": max(dets),
                               "plan": {"exec": plan["exec_ms"],
                                        "hnsw": any(n["index"] == "idx_semantic_cache_embedding_hnsw" for n in plan["nodes"]),
                                        "expiry_scan": any(n["index"] == "idx_semantic_cache_expiry" for n in plan["nodes"]),
                                        "scope_bitmap": any(n["index"] == "idx_semantic_cache_scope" for n in plan["nodes"])}}
            out["default_path"] = dp
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
