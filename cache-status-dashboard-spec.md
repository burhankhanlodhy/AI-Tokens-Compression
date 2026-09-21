# v1.1 / AC-PC — Semantic-Cache Status Surfacing on the Dashboard (UI/UX spec)

Author: @ui-ux-engineer · For: @application-developer (implementation) + @qa-lead (gate) + @product-manager (AC-PC5 ratification input)
Supersedes: nothing. Extends `dashboard-ac-a8-spec.md` (§6 savings-decomposition contract) and the PA-3 shell.
Standing rules enforced (per PM, v1.1 kickoff): tiles read the **ledger's own per-request fields** — never a benchmark constant, never a config value presented as a measurement — and the decomposition is **never summed** into the aggregate.

---

## 1. Purpose & acceptance

Phase C1 adds a pgvector-backed semantic cache to the request path. The frozen order is exact-prefix detection FIRST, then semantic lookup only on an exact miss (§2.1). The dashboard must make three things visible at a glance, each traceable 1:1 to ledger rows:

1. **Is the semantic cache on, and is it working** (hit / threshold-miss / miss rates)?
2. **What did it save** — as a contained sub-tile of the savings decomposition, sibling to L1, never summed with it (taxonomy §1; a1eae90 contract).
3. **How confident can the operator be in those numbers** — the tie back to AC-PC4/AC-PC5 (embedding/quality version policy, traffic-volume measurement) is surfaced via a version line on the tile, not via a hardcoded "benchmark" number.

Current state: `cache_status` column already exists in both ledger paths (SQLite + Postgres, `stats.py:107-226`); current emitted values are `miss` and `exact_hit` (`main.py:558-572`); `/api/kpis` already counts `exact_hit` (`kpis.py:71,154`). `semantic_cache.py` ships `lookup()` + `store_response()`. Gap: no semantic statuses, no semantic tile, no flag-off state.

**Acceptance:** QA gate is a new AC named **AC-PC-UI** in the test suite (pattern of AC-A8), verifying every tile against seeded ledger rows. Flag flips and row counts are never asserted against a fixture constant.

**Acceptance hardening (PM ruling, after live-DB probe):** AC-PC-UI additionally asserts (a) `token_saver_ledger_write_failures` **stays 0** across the gate run and (b) ledger row count == requests sent. Context: the live constraint was `chk_cache_status IN ('miss','exact_hit','semantic_hit')` — a writer emitting `semantic_threshold_miss` before the constraint widened would 200 the request but silently **drop the entire ledger row** at `main.py:1100` (`except Exception: LEDGER_WRITE_FAILURES += 1`), erasing savings/tokens/cost from everything the dashboard computes. Two related facts: the SQLite fallback `INSERT` in `stats.py` omits `cache_status`, so the unit suite could stay green through this defect class (writer-side fix is Dev's); and the DB-only gate `token-saver/test/test_ac_pcui_ledger_taxonomy_gate.py` (branch `qa/ac-pcui-ledger-taxonomy-gate`, commit `8c9a88e`) pins the taxonomy round-trip against real Postgres.

**Merge order (frozen):** DBA constraint migration `b4baf47` (`20260920_ac_pcui_cache_status.sql`) **first** — now applied live — then the QA gate file, then any writer literal. No dashboard tile may render a taxonomy value whose ledger write the running constraint would reject.

---

## 2. Ledger status taxonomy (frozen; Dev implements, PM ratifies)

`cache_status` takes exactly these values. Every dashboard element is a function of these strings only.

| `cache_status` | Meaning | Emitted when |
|---|---|---|
| `miss` | No lookup path produced a servable row | Cache disabled, or lookup ran and found no compatible row, or request wasn't cache-eligible |
| `exact_hit` | Exact-match PA-4 key hit (existing behavior, unchanged) | Exact lookup served the response |
| `semantic_hit` | **New.** pgvector lookup found a compatible unexpired row within threshold | Semantic lookup served the response |
| `semantic_threshold_miss` | **New.** pgvector lookup found a *compatible* row (same scope/dims/model/embedding-version) but its best cosine **distance** exceeds the calibrated threshold | Lookup ran, `SemanticLookupScope.complete()`, closest candidate's cosine distance > threshold (cosine distance is the `<=>` metric — lower is closer; hit iff distance ≤ threshold) |

Rationale for keeping `semantic_threshold_miss` distinct: it is the cheap, always-on observability signal for the same three-way outcome DBA is measuring manually (hit / threshold-miss / no-compatible-row). If the operating threshold needs tuning later, the operator sees the pressure on the dashboard instead of re-running the AC-PC4 probe. The distinct value is ratified (PM, C1 reconciliation, this commit) — do not fold threshold-misses into `miss`.

`cache_savings` semantics unchanged: populated on `exact_hit` **and** `semantic_hit` only; 0.0 otherwise. A request that is L1-stripped **and** cache-hit reports **only cache savings** — the tile never displays a request in both sub-tiles.

### 2.1 Lookup ordering and single-status precedence (FROZEN — PM C1 reconciliation)

Exactly one `cache_status` is written per request. The order is fixed and must be implemented as written; QA's mutation checks (removing one path) must fail the intended gate:

1. **Exact-prefix detection runs FIRST** — the existing PA-4 block (`main.py:558-572`, after L1 clean, before compression) is unchanged and untouched by the semantic path. It applies to streaming and non-streaming requests alike, exactly as in v1.0.1.
2. **If exact-prefix detection hits → `cache_status = "exact_hit"` is final.** The semantic lookup is **NOT attempted** and no semantic store happens for that request. Rationale: an exact-prefix hit already pinned the request on a deterministic identity; a similarity lookup would add embedding + HNSW cost and could only conflict with the frozen single status. `exact_hit` rows keep their existing ledger shape (no semantic version columns — they are NULL, §3.1).
3. **Only on an exact miss does the semantic path run** (when `SEMANTIC_CACHE_ENABLED` is true and the request is a non-streaming eligible request). The structured semantic lookup result (product-spec AC-PC7) maps to the single status:
   - `HIT` → `semantic_hit` — replay the stored body byte-verbatim, never call upstream.
   - `THRESHOLD_MISS` → `semantic_threshold_miss` — the compatible row exists but its cosine distance exceeds the calibrated threshold (AC-PC4); proceed upstream.
   - `NO_COMPATIBLE_ROW` / `NOT_ATTEMPTED` → `miss` — proceed upstream.
4. **Streaming is a semantic miss by construction** (AC-PC2 hit shape): the semantic lookup is never invoked for `stream: true`; the exact-prefix path still runs and may produce `exact_hit`.
5. **Storage** happens only after a successful upstream response for an eligible non-streaming exact-miss request whose semantic lookup **ran** (outcome `THRESHOLD_MISS` or `NO_COMPATIBLE_ROW`). `NOT_ATTEMPTED` (disabled, absent/invalid threshold, embedding/DB failure, oversize) stores nothing. An `exact_hit` request never stores.

Every row the dashboard reads is exactly one of the four taxonomy strings; no renderer may synthesize a fifth.

---

## 3. API contract — `/api/kpis` additions (FROZEN — PM C1 reconciliation; product-spec AC-PC8)

All new fields are computed **server-side in SQL** over the ledger, per AC-A8's zero-client-aggregation rule. The shape is frozen — this is not a suggestion, there is no "Dev's call" left open. New top-level key `cache`, exactly:

```json
{
  "cache": {
    "enabled": true,
    "total_requests": 6,
    "exact_hit_count": 1,
    "semantic_hit_count": 2,
    "semantic_threshold_miss_count": 1,
    "miss_count": 2,
    "hit_rate": 50.0,
    "semantic_hit_rate": 33.33,
    "semantic_threshold_miss_rate": 16.67,
    "exact_hit_savings": 0.0004,
    "semantic_hit_savings": 0.0009,
    "embedding_versions": [{"version": "openai:text-embedding-3-small@1536", "hit_count": 2}],
    "quality_versions": [{"version": "1.1.0", "hit_count": 2}]
  }
}
```

Field-by-field contract (denominators are explicit; Dev/UI/QA must not read any other reading):

| Field | Type | Definition (all over the selected bucket/window/tenant scope, same as the rest of KPI API) |
|---|---|---|
| `enabled` | bool | Config echo of `SEMANTIC_CACHE_ENABLED` at query time. Allowed only for the flag-off state (§5.1); never used in any arithmetic. |
| `total_requests` | int | `COUNT(*)` of ledger rows in scope. THE denominator for every rate. |
| `exact_hit_count` | int | `COUNT(*) WHERE cache_status = 'exact_hit'`. |
| `semantic_hit_count` | int | `COUNT(*) WHERE cache_status = 'semantic_hit'`. |
| `semantic_threshold_miss_count` | int | `COUNT(*) WHERE cache_status = 'semantic_threshold_miss'`. |
| `miss_count` | int | `COUNT(*) WHERE cache_status = 'miss'`. |
| `hit_rate` | float\|null | Combined cache hit rate: `100 * (exact_hit_count + semantic_hit_count) / total_requests`, rounded to 2 decimals in SQL. `null` when `total_requests = 0`. This is the **combined** rate and is rendered by NO card labeled "semantic". |
| `semantic_hit_rate` | float\|null | Semantic-only hit rate: `100 * semantic_hit_count / total_requests`, 2 decimals. `null` when `total_requests = 0`. **This** is what the "SEMANTIC CACHE HIT RATE" card renders (§4.2). |
| `semantic_threshold_miss_rate` | float\|null | `100 * semantic_threshold_miss_count / total_requests`, 2 decimals. `null` when `total_requests = 0`. |
| `exact_hit_savings` | float | `COALESCE(SUM(cache_savings) WHERE cache_status='exact_hit', 0)` — SQL, USD. |
| `semantic_hit_savings` | float | `COALESCE(SUM(cache_savings) WHERE cache_status='semantic_hit', 0)` — SQL, USD. |
| `embedding_versions` | array | Distinct `embedding_version` values observed on **`semantic_hit`** ledger rows in scope, each `{"version": <text>, "hit_count": int}`. Deterministic order: `hit_count` DESC, then `version` ASC. Empty array `[]` when no semantic hits (never `null`, never a scalar — a scalar cannot represent multiplicity). No aggregation across versions. |
| `quality_versions` | array | Same contract as `embedding_versions` over `quality_version` on `semantic_hit` rows. |

Rules (unchanged from prior draft, made binding):
- **No benchmark constants in the payload.** `embedding_versions`/`quality_versions` come from the ledger rows the writer stamped at request time (see §3.1). The dashboard shows what the cache *actually* served with, never what the spec *intends* (AC-PC5 read-back).
- `enabled` is a mode, not a measurement — used **only** for the flag-off state in §5.1, never to compute any number.
- Name/denominator resolution (the "Semantic cache hit rate" ambiguity): the card labeled **"SEMANTIC CACHE HIT RATE"** displays **`semantic_hit_rate`** (semantic-only). The combined figure lives only in **`hit_rate`** (exact+semantic); no UI element labeled "semantic" may render `hit_rate`, and no UI element may compute `hit_rate` client-side.
- Invariant (QA asserts): `exact_hit_count + semantic_hit_count + semantic_threshold_miss_count + miss_count == total_requests` for every scope.
- Counts and rates are over the selected bucket/window, same as the rest of the KPI API (§6 I-7).

### 3.1 Version surfacing rule (multi-version windows, FROZEN)

`embedding_version`/`quality_version` are per-row cache namespaces (AC-PC5, committed `c0e3622` — ratified, do not re-litigate). A selected window **can** contain hits served under multiple versions (a version bump quarantines *new writes* while previously-written rows stay readable until `expires_at`; a rollback restores old-version rows immediately). Therefore:

1. The request-time writer **stamps** `embedding_version` and `quality_version` onto the ledger `requests` row **for `semantic_hit` rows only** (the lookup scope already carries both — AC-PC5.1/5.2). These columns are NULL on `miss`, `exact_hit`, and `semantic_threshold_miss` rows. This is new schema work in the writer commit (currently the `requests` table has no version columns — verified `postgres-schema-v2.sql`).
2. `/api/kpis` never returns a single "current" version scalar; it returns the distinct observed versions with hit counts (§3 `embedding_versions`/`quality_versions`). If exactly one distinct version exists, the array has one element — the UI still renders it from the array, never from config.
3. The UI version line reads `embedding_versions`/`quality_versions` directly: primary value = first element (`hit_count` DESC). When either array has more than one element, the line appends the disagreeing count: `embeddings: <primary_ev> / <primary_qv> (+<sum of non-primary hit_counts> rows on other versions)`. The parenthetical must match the array math byte-for-byte.

---

## 4. UI — where it lives

### 4.1 Overview tab — "Cache" sub-tile inside the savings decomposition card

The existing decomposition card renders `cost_saved` as the headline with L1 and cache as **contained sub-tiles** (`.subtile` pattern, `dashboard_v2.py:90-105`). Extend it:

```
COST SAVED                                    $X.XX
┌─────────────────────────────────────────────┐
│ EXACT CACHE                                 │
│  n requests served            $X.XX  ─────  │
│ SEMANTIC CACHE   [live|off|warming]         │
│  n requests served            $X.XX  ─────  │
│  hit rate p% · threshold-misses m          │
│  embeddings: <embedding_version> / <quality_version> │
│ L1 STRIP                                    │
│  n tokens stripped            $X.XX         │
└─────────────────────────────────────────────┘
```

- Semantic sub-tile is the third sub-tile, styled identically to the L1 one (`.subtile`). A muted note stays visible in the card: *"Savings decomposition: exact, semantic, and L1 are per-request categories — never summed into the headline."* (This makes the taxonomy §1 rule user-visible, as the B3 note required.)
- **Hit rate line** shows server-computed `semantic_hit_rate`; the parenthetical gives threshold-miss pressure (`semantic_threshold_miss_count`). If `semantic_threshold_miss_count = 0` the line reads simply "hit rate p%".
- **Version line** renders iff `embedding_versions` is non-empty; primary text `embeddings: <embedding_versions[0].version> / <quality_versions[0].version>` with the §3.1 disagreement parenthetical when either array has >1 element. This is the operator-facing tie to AC-PC4's traffic-volume measurement and AC-PC5's ratified namespaces — a glanceable answer to "which embedding/quality regime produced these hits", resolved honestly when a window spans versions.

### 4.2 KPI card row (span 3) — "Semantic cache hit rate"

One new KPI card on Overview, matching the existing card anatomy: label "SEMANTIC CACHE HIT RATE", big numeral **`semantic_hit_rate`** (tabular-nums), delta line comparing current bucket to previous (▲/▼, existing `.kpi-delta` semantics). This card is a verbatim copy of the API field — the percentage math lives in the `/api/kpis` SQL, nowhere else. It never renders `hit_rate` (combined), per §3.

### 4.3 Traffic tab — per-request status column

The requests table gains a "Cache" column rendering `cache_status` as a badge (`.badge`): `exact_hit` → green, `semantic_hit` → green (distinguish by text; optionally a neutral→gold tint for semantic to make the v1.1 contribution scannable), `semantic_threshold_miss` → gold, `miss` → neutral. Badge text is the raw status string — no renaming/translation layer, so QA reconciles badge ↔ ledger cell directly.

---

## 5. States (shared component set; every element implements all four)

### 5.1 Flag-off state — **must not render as zero**

When `cache.enabled = false` (the state we are shipping in until AC-PC4 passes on live traffic):
- Semantic sub-tile header shows a **neutral badge `off`** next to the title; body shows a single muted line: *"Semantic cache is disabled — no lookups are running."*
- The KPI card renders the label with a neutral `off` badge and the numeral slot shows "—" (`zero-dash` style), not `0%`. **Zero is a measurement ("on, found nothing"); "—" is a mode ("not running").** Conflating these is exactly the failure mode of reading a benchmark constant as live data.
- Traffic-tab cache column renders neutral "off" badges for semantic statuses only — `exact_hit` rows still badge normally, since the exact cache is unaffected by the flag.

### 5.2 On, no rows yet ("warming")

`cache.enabled = true` but 0 ledger rows carry `semantic_hit` in the window: sub-tile shows the `live` badge and the body reads *"No semantic hits yet — cache is warming."* Hit-rate card shows `—` (null `semantic_hit_rate`, per §3, not 0). Threshold-miss line still renders if threshold-misses exist (they can precede the first hit — that's normal pgvector behavior and is the number DBA's measurement tracks).

### 5.3 Loading / error

Skeleton rows for the sub-tile and card during fetch; on `/api/kpis` failure the existing `.error-box` + retry pattern applies to the whole Overview render — the cache tile must **not** fall back to cached/previous numbers silently (same rule as AC-A8 §8).

---

## 6. Explicit non-goals / invariants (testable)

1. **I-1:** No displayed number on any cache tile is constant, config-derived arithmetic, or a benchmark figure; every number is a verbatim copy of a `/api/kpis` field computed in SQL from the ledger. *(Pattern: AC-A8 1:1 rule.)*
2. **I-2:** No renderer, anywhere, sums `exact_hit_savings + semantic_hit_savings + l1_savings` to display anything; the headline remains `cost_saved` verbatim. *(Pattern: taxonomy §1; a1eae90.)*
3. **I-3:** A request never contributes to both a cache sub-tile and the L1 sub-tile (per-request attribution, §2).
4. **I-4:** Flag-off renders `off`/"—", never `0`/`0%`.
5. **I-5:** `semantic_threshold_miss` rows appear in Traffic with the gold badge and count in the sub-tile parenthetical.
6. **I-6:** Version line renders iff `embedding_versions` is non-empty; its text equals the ledger/entries array values verbatim — no config echo, no benchmark constant. When either `embedding_versions` or `quality_versions` has >1 element, the §3.1 disagreement parenthetical is present and its count equals the array math exactly.
7. **I-7:** All counts/rates respond to the bucket selector as a re-fetch (control, not client aggregation).
8. **I-8:** Exactly one `cache_status` per request is rendered (frozen precedence §2.1): `exact_hit` beats every semantic outcome (semantic lookup never runs after an exact hit); `semantic_hit`/`semantic_threshold_miss`/`miss` follow only on exact miss. No renderer may display two statuses for one row.

## 7. Test plan (for @qa-lead — AC-PC-UI)

Seed ledgers with one request per status plus mixed buckets, then assert I-1…I-8 per render (Node harness pattern of `test_dashboard_render.py` / AC-A8 gate). Include the two poison cases specifically: (a) `enabled=false` with historical semantic rows present in the window → must still render `off`/"—" (I-4 trumps history); (b) a window containing only `semantic_threshold_miss` → "warming" body + visible threshold-miss count, `—` hit rate. Add the frozen-contract cases: (c) mixed multi-version window (two `embedding_versions` / two `quality_versions` on `semantic_hit` rows) → version line renders primary + disagreement parenthetical whose count matches the array math (I-6); (d) rows carrying `exact_hit` and `semantic_hit` in the same window → KPI card reads `semantic_hit_rate` only, never `hit_rate` (§3); (e) `total_requests = 0` → all rates `null`, card shows "—" not 0; (f) a seeded row that would violate single-status (two statuses for one request id) → renderer shows exactly one (§2.1, I-8).

## 8. Resolved: version policy is ratified (was: Open item)

~~AC-PC5's embedding_version/quality_version policy is **not yet ratified** — §4.1's version line is written so it renders whatever the entries table returns once @product-manager fixes the policy; if the ratified policy makes the two versions per-row-varying, the line becomes "embeddings: vN (n rows disagree)" — but that ruling is PM's, and Dev should not build against a guess.~~

**RESOLVED by PM (C1 reconciliation, this commit):** the AC-PC5 version-namespace policy **is ratified** (product-spec-v2.md AC-PC5, committed `c0e3622`, "do not re-litigate") and IS per-row-varying by design. The version line is frozen in §3.1/§4.1: primary value from `embedding_versions`/`quality_versions[0]`, with the disagreement parenthetical when a window spans versions. Dev builds against §3.1/§4.1 as written — no guess remains.
