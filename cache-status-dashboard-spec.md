# v1.1 / AC-PC — Semantic-Cache Status Surfacing on the Dashboard (UI/UX spec)

Author: @ui-ux-engineer · For: @application-developer (implementation) + @qa-lead (gate) + @product-manager (AC-PC5 ratification input)
Supersedes: nothing. Extends `dashboard-ac-a8-spec.md` (§6 savings-decomposition contract) and the PA-3 shell.
Standing rules enforced (per PM, v1.1 kickoff): tiles read the **ledger's own per-request fields** — never a benchmark constant, never a config value presented as a measurement — and the decomposition is **never summed** into the aggregate.

---

## 1. Purpose & acceptance

Phase C1 puts a pgvector-backed semantic cache in front of the exact-match cache. The dashboard must make three things visible at a glance, each traceable 1:1 to ledger rows:

1. **Is the semantic cache on, and is it working** (hit / threshold-miss / miss rates)?
2. **What did it save** — as a contained sub-tile of the savings decomposition, sibling to L1, never summed with it (taxonomy §1; a1eae90 contract).
3. **How confident can the operator be in those numbers** — the tie back to AC-PC4/AC-PC5 (embedding/quality version policy, traffic-volume measurement) is surfaced via a version line on the tile, not via a hardcoded "benchmark" number.

Current state: `cache_status` column already exists in both ledger paths (SQLite + Postgres, `stats.py:107-226`); current emitted values are `miss` and `exact_hit` (`main.py:551-560`); `/api/kpis` already counts `exact_hit` (`kpis.py:71,154`). `semantic_cache.py` ships `lookup()` + `store_response()`. Gap: no semantic statuses, no semantic tile, no flag-off state.

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
| `semantic_threshold_miss` | **New.** pgvector lookup found a *compatible* row (same scope/dims/model/embedding-version) but best cosine similarity fell below the threshold | Lookup ran, `SemanticLookupScope.complete()`, closest candidate < threshold |

Rationale for keeping `semantic_threshold_miss` distinct: it is the cheap, always-on observability signal for the same three-way outcome DBA is measuring manually (hit / threshold-miss / no-compatible-row). If the operating threshold needs tuning later, the operator sees the pressure on the dashboard instead of re-running the AC-PC4 probe. If @product-manager prefers to fold threshold-misses into `miss`, the tile degrades gracefully (§4 renders from whatever statuses exist) — but the distinct value is strongly recommended.

`cache_savings` semantics unchanged: populated on `exact_hit` **and** `semantic_hit` only; 0.0 otherwise. A request that is L1-stripped **and** cache-hit reports **only cache savings** — the tile never displays a request in both sub-tiles.

---

## 3. API contract — `/api/kpis` additions

All new fields are computed **server-side in SQL** over the ledger, per AC-A8's zero-client-aggregation rule. Suggested shape under a new top-level `cache` key (exact JSON placement is Dev's call; the invariant is one fetch, verbatim field copies):

```
cache: {
  enabled: <bool — reflects SEMANTIC_CACHE_ENABLED at query time>,
  exact_hit_count, semantic_hit_count, semantic_threshold_miss_count, miss_count,
  hit_rate: <(exact+semantic)/(total requests), computed in SQL, null when total = 0>,
  semantic_hit_savings: <SUM(cache_savings) WHERE semantic_hit, SQL>,
  exact_hit_savings:   <SUM(cache_savings) WHERE exact_hit, SQL>,
  embedding_version: <value read from the cache entries actually serving, or null>,
  quality_version: <value read from the cache entries actually serving, or null>
}
```

Rules:
- **No benchmark constants in the payload.** `embedding_version`/`quality_version` come from the rows (`semantic_cache_entries`), which also satisfies the read-back of AC-PC5's policy once PM ratifies it — the dashboard shows what the cache *actually* served with, never what the spec *intends*.
- `enabled` is allowed as a config echo (it is a mode, not a measurement) and is used **only** for the flag-off state in §5.1, never to compute any number.
- Counts and rates are over the selected bucket/window, same as the rest of the KPI API.

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
│  hit rate p% · threshold-misses m (of k lookups) │
│  embeddings: <embedding_version> / <quality_version> │
│ L1 STRIP                                    │
│  n tokens stripped            $X.XX         │
└─────────────────────────────────────────────┘
```

- Semantic sub-tile is the third sub-tile, styled identically to the L1 one (`.subtile`). A muted note stays visible in the card: *"Savings decomposition: exact, semantic, and L1 are per-request categories — never summed into the headline."* (This makes the taxonomy §1 rule user-visible, as the B3 note required.)
- **Hit rate line** shows server-computed `hit_rate`; the parenthetical gives threshold-miss pressure. If `semantic_threshold_miss_count = 0` the line reads simply "hit rate p%".
- **Version line** renders only when `embedding_version` is non-null; wording "embeddings: vN / vM". This is the operator-facing tie to AC-PC4's traffic-volume measurement and AC-PC5's ratification — a glanceable answer to "which embedding/quality regime produced these hits".

### 4.2 KPI card row (span 3) — "Semantic cache hit rate"

One new KPI card on Overview, matching the existing card anatomy: label "SEMANTIC CACHE HIT RATE", big numeral `hit_rate` (tabular-nums), delta line comparing current bucket to previous (▲/▼, existing `.kpi-delta` semantics). This card is a verbatim copy of the API field — the percentage math lives in `kpis.py` SQL, nowhere else.

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

`cache.enabled = true` but 0 ledger rows carry `semantic_hit` in the window: sub-tile shows the `live` badge and the body reads *"No semantic hits yet — cache is warming."* Hit-rate card shows `—` (null `hit_rate`, per §3, not 0). Threshold-miss line still renders if threshold-misses exist (they can precede the first hit — that's normal pgvector behavior and is the number DBA's measurement tracks).

### 5.3 Loading / error

Skeleton rows for the sub-tile and card during fetch; on `/api/kpis` failure the existing `.error-box` + retry pattern applies to the whole Overview render — the cache tile must **not** fall back to cached/previous numbers silently (same rule as AC-A8 §8).

---

## 6. Explicit non-goals / invariants (testable)

1. **I-1:** No displayed number on any cache tile is constant, config-derived arithmetic, or a benchmark figure; every number is a verbatim copy of a `/api/kpis` field computed in SQL from the ledger. *(Pattern: AC-A8 1:1 rule.)*
2. **I-2:** No renderer, anywhere, sums `exact_hit_savings + semantic_hit_savings + l1_savings` to display anything; the headline remains `cost_saved` verbatim. *(Pattern: taxonomy §1; a1eae90.)*
3. **I-3:** A request never contributes to both a cache sub-tile and the L1 sub-tile (per-request attribution, §2).
4. **I-4:** Flag-off renders `off`/"—", never `0`/`0%`.
5. **I-5:** `semantic_threshold_miss` rows appear in Traffic with the gold badge and count in the sub-tile parenthetical.
6. **I-6:** Version line renders iff `embedding_version` is non-null; its text equals the ledger/entries value verbatim.
7. **I-7:** All counts/rates respond to the bucket selector as a re-fetch (control, not client aggregation).

## 7. Test plan (for @qa-lead — AC-PC-UI)

Seed ledgers with one request per status plus mixed buckets, then assert I-1…I-7 per render (Node harness pattern of `test_dashboard_render.py` / AC-A8 gate). Include the two poison cases specifically: (a) `enabled=false` with historical semantic rows present in the window → must still render `off`/"—" (I-4 trumps history); (b) a window containing only `semantic_threshold_miss` → "warming" body + visible threshold-miss count, `—` hit rate.

## 8. Open item

AC-PC5's embedding_version/quality_version policy is **not yet ratified** — §4.1's version line is written so it renders whatever the entries table returns once @product-manager fixes the policy; if the ratified policy makes the two versions per-row-varying, the line becomes "embeddings: vN (n rows disagree)" — but that ruling is PM's, and Dev should not build against a guess.
