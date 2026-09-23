# Performance tuning guide

How to tune the token-saver proxy's savings levers — and which knobs not to
touch. All defaults below are the shipped values in `proxy/config.py`.

## Codebase-context optimization (v1.2.1 candidate)

The codebase optimizer runs before L1 cleanup and only transforms text-bearing
message content. It does not edit roles, tool-call metadata, image parts, or
fenced code during shell filtering. The candidate measured **92.61% as-shipped
codebase-segment input-token reduction** on the general E2E corpus (20 scenarios
/ 5 segments, k=2; SHA-256
`af21ac53e8d1da7f2ab4402a0573ec3709bc5d92513a3ecbc8ce575f6f10a33a`). This
result is population-specific, not a per-request guarantee, and does not imply
that every transformed context is semantically interchangeable. Review any
omission marker and retrieve source context when details matter.

### V1.2.1 measured candidate evidence and status

The v1.2.1 integration recommendation remains **NO-GO**. The following
population-specific measurements explain the candidate behavior; re-baselined
gates remain pending re-measurement on the commissioned fixtures. These are
not per-request guarantees and do not establish release readiness.

- **Tool segment, general E2E corpus** (20 scenarios / 5 segments, k=2;
  SHA-256 `af21ac53e8d1da7f2ab4402a0573ec3709bc5d92513a3ecbc8ce575f6f10a33a`):
  **0.55% as-shipped marginal reduction**, ratio-of-sums over the whole prompt
  (14,682 → 14,601 prompt tokens), alongside **15.51% isolated transformer
  contribution**, measured as 9,722 `tool_compression_saved` tokens / 62,666
  baseline tool+schema prompt tokens. The isolated figure is not a customer-bill
  result. The former 15% general-corpus floor is retired; the new ≥8%
  as-shipped tool-heavy floor awaits the tool-schema-heavy fixture.
- **Schema corpus** (92 tools / 5 scenarios; SHA-256
  `e689f2c7fc8accf6140f2b22dc19bceb0b3e4012d14c0517cb947d40d97d2284`):
  compact-client end-to-end reduction was **1.22%**; pretty-printed-client
  end-to-end reduction was **43.9%**. Compact clients claim “never inflates,
  never breaks validation,” not a percentage floor; the pretty-printed ≥20%
  gate is retained. Conservation had 0 violations across 92 tools, and schema
  cache hit rate was **100% (40/40 eligible turns)**. The uniform general-corpus
  schema scenarios on the general E2E corpus (20 scenarios / 5 segments, k=2;
  SHA-256 `af21ac53e8d1da7f2ab4402a0573ec3709bc5d92513a3ecbc8ce575f6f10a33a`)
  measured 3.58–3.71%; re-baselined gates are pending.
- **Result segment, general E2E corpus** (20 scenarios / 5 segments, k=2;
  SHA-256 `af21ac53e8d1da7f2ab4402a0573ec3709bc5d92513a3ecbc8ce575f6f10a33a`):
  **0.00% as-shipped reduction** (55,145 → 55,145 prompt tokens), because the
  corpus's 4,506-token tool results were below the 5,000-token cap and correctly
  no-oped. The 25% floor is retained and awaits the commissioned over-cap corpus.

The measurements above describe different corpus populations and must not be
combined as if they were one benchmark. See `CHANGELOG.md` for the candidate
summary; no gate is reported as passing based on a retired or re-baselined floor.

- `CODEBASE_OPTIMIZATION_ENABLED` — master switch (default `true`). Set it to
  `false` to disable all three operations below.
- `CODEBASE_MAX_FILE_LINES` — maximum source lines retained from oversized
  fenced file bodies (default `200`, minimum `2`), before the omission marker
  is added. The first and last portions are kept; lower it only when context
  budgets justify more aggressive omission.
- `CODEBASE_DEDUPE_IMPORTS` — remove repeated import lines after their first
  occurrence (default `true`). A line must occur more than three times across
  eligible fenced code before later copies are replaced with a marker.
- `SHELL_OUTPUT_FILTERING` — filter recognizable shell/debug noise from
  unfenced text (default `true`). Errors, warnings, useful output, and Python
  traceback frames are retained; fenced code is not filtered. Set it to
  `false` when exact shell transcripts are important.

The switches are independent: disabling import deduplication does not disable
shell filtering, and vice versa. All settings are available in
`token-saver/.env.example`.

## Semantic cache threshold (`SEMANTIC_CACHE_MAX_COSINE_DISTANCE`)

The threshold is a **cosine distance**: a lookup hits when the nearest
compatible entry is at most this far from the query embedding. Lower = more
conservative (fewer hits, fewer false hits); higher = more hits but more
risk of serving a response to a prompt that meant something else.

- **Default: unset.** An absent threshold **fails closed** — every semantic
  lookup is a clean miss. This is deliberate: the proxy never guesses a
  similarity bar.
- **Ratified production operating point: `0.18`** (C1 ruling, PC5
  recalibration on real traffic-shaped embeddings at 11,500 rows:
  100% coverage of positive pairs, 95.83% recall, zero false hits, p95
  lookup latency 4.74 ms). This value was measured, not picked by eye —
  the separating gap between the closest positive pair (0.1607) and the
  nearest hard negative (0.2530) sits either side of it.
- Do **not** raise the threshold without re-measuring: false-hit onset was
  calibrated at this exact point. Re-run the committed PC5 calibration
  (`benchmark/run_pc4_real_embeddings.py` with the committed corpus) before
  shipping a different number.
- The threshold never changes cache identity: entries written under one
  threshold remain readable when the threshold changes (query-time knobs
  are excluded from the version namespace).

## HNSW `ef_search` (`SEMANTIC_CACHE_HNSW_EF_SEARCH`)

`ef_search` is pgvector's per-query candidate-list size for the HNSW index.
Higher values scan more of the graph (better recall, more latency).

- **Default and ratified value: 100** — applied per-lookup via
  `set_config('hnsw.ef_search', ...)` on the lookup session, never as a
  global server setting.
- Measured at 11,500-row traffic shape: ef=100 passes all gates (p95
  4.74 ms); ef=300 also passes (p95 8.59 ms) with identical recall at the
  ratified threshold; **ef=1000 fails the latency bar** (p95 103.8–112.1 ms
  — the planner flips to a scope-scan at ≥10k rows). Keep it **≤ 300**.
- Index build parameters are a separate, frozen concern: the pc1 migration
  builds the HNSW index with `m=16, ef_construction=200`. The ratified
  operating point is conditional on that build — an index rebuilt with
  pgvector's default `ef_construction=64` does not meet the recall bar
  (87.5% on the healthy-graph control, 58% under duplicate flooding). Do
  not rebuild the index with lower build parameters to "save time".

## Semantic cache TTL (`SEMANTIC_CACHE_TTL_SECONDS`)

- Default **300 s** (range 1–86400). A semantic entry and its verbatim
  response payload **expire as one pair** — there is no path where the index
  entry outlives its response bytes.
- Shorter TTL trades hit rate for freshness of invalidation semantics;
  longer TTL raises the chance a cached response predates an upstream
  behavior change. Deterministic invalidation (expiry + version namespaces)
  is a QA-pinned contract, so tune only this duration, not the mechanism.
- Not request-configurable by design; a client header cannot extend or
  shrink the TTL.

## When to leave the semantic cache off

`SEMANTIC_CACHE_ENABLED=false` (the default) is the right choice when:

- Your traffic is mostly unique prompts — the exact-prefix cache
  (`cache_enabled`, on by default, 24 h TTL) already covers repeated
  identical requests; the semantic cache only pays off on paraphrase-heavy
  traffic.
- Your upstream does not serve `/embeddings` with the client's credential —
  embedding acquisition is best-effort and lookups silently become misses.
- You have not applied the pgvector migrations — the lookup fails closed,
  but there is no cache to hit.

Enablement is a deployment decision: set
`SEMANTIC_CACHE_ENABLED=true` **and** `SEMANTIC_CACHE_MAX_COSINE_DISTANCE=0.18`
together. No request header can enable the cache or alter its scope.

## L1 lossless cleanup opt-out (`L1_ENABLED=false`)

L1 runs **on by default** and is lossless by contract: JSON whitespace
compaction (C1), duplicate/empty system-block removal (C2), dead RAG
metadata drop (C3). Answer content is never touched.

Opt out when:

- You send messages whose **entire content is pretty-printed JSON with no
  surrounding prose** — L1 whitespace-compacts those. If you send
  "reformat this" style bare JSON, set `L1_ENABLED=false`.
- You are benchmarking byte-level behavior and need the raw passthrough
  path.

What opting out costs: L1 is where most of the honest savings come from —
the published production-default arm (C1+C2+C3 on) measures **29.9%–70.4%
input-token reduction depending on payload shape** (~80% on RAG context,
~79% on duplicated system blocks, ~19% on log/trace, ~32% on JSON docs,
0% on prose). With C2/C3 disabled conceptually, C1-only remains the
conservative **29.9%** floor. These are corpus-weighted aggregates over the
checksummed taxonomy corpus — never per-request guarantees; the dashboard
tiles read actual ledger attribution (`l1_tokens_stripped`, `l1_cost_saved`
as a contained portion of `cost_saved`, never added to it).

L1 runs before the cache key (cache key = clean bytes), so it is stable and
deterministic: identical raw input produces identical cache keys.

## Compression tuning (`COMPRESSION_RATE`, `LLMLINGUA_MODEL`)

- `COMPRESSION_RATE` (default **0.6**) is the LLMLingua-2 target compression
  ratio. Lossy compression is intent-gated: code and precise routes pass
  through by policy, so the knob only affects prose-classified traffic.
- Outputs longer than LLMLingua-2's 512-token BERT input window are left
  unchanged (the window guard from v1.0.1), so raising the rate does not
  corrupt long inputs — it simply has no effect on them.
- `OUTPUT_CONCISENESS_ENABLED` stays **off by default** (P1-1 benchmark: no
  reliable savings on the pinned instrument). Per-request enabling is only
  via the `X-Token-Saver-Conciseness` control header, which never forwards
  upstream.
