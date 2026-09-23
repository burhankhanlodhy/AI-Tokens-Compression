# V1.2.1 Product Decisions — Compression Floors and Corpus Policy

**Card:** t_45779d90 · **Decider:** product-manager · **Date:** 2026-09-23 (CDT)
**Ratifies against:** integration NO-GO report t_6cfedefc (`v1.2.1-integration-go-no-go.md` @ f52fb6f),
Part 2 E2E evidence t_a35375a1, T3 QA report t_9119e541, Part 1 T1 flag t_1ef6a911,
floor definitions in the v1.2.1 compression plan (`v1.2.1-tool-codebase-compression-plan.md`).
**Status:** RATIFIED (PM, this card). These decisions are durable; do not re-litigate in later cards.
**Scope guard:** this note decides product policy and measurement contracts only. No code is
implemented and no release action (tag/push/publish/deploy) is authorized by this card.

---

## Context in one paragraph

The v1.2.1 integration run is NO-GO. Aggregate savings are strong (66.62% vs ≥25% floor), the
regression suite is fully green (824/824, PG lane live), and protocol integrity is intact
(tool-call parity 24/24, ledger 46/46). Three category floors fail: tool-heavy 0.55% (floor 15%),
schema-heavy 3.66% / compact-client 1.22% (floor 20%), result-heavy 0.00% (floor 25%) — with the
result floor additionally shown to be *unsatisfiable as written* because the pinned corpus's tool
results (4,506 tokens) sit under the 5,000-token optimization cap, so the optimizer correctly
no-ops. Root causes are measurement-contract problems and corpus-representativeness gaps, not
correctness defects: conservation is proven clean (0 violations across 92 tools), and over-cap
truncation separately verified working. The three gates below are re-decided here.

---

## Decision 1 — T1 tool-heavy savings interpretation: dual-number contract; re-baseline the floor

**The numbers.** As-shipped marginal on the E2E corpus: **0.55%** (14,682 → 14,601 prompt tokens).
Isolated T1-contribution reading of the same run's ledger: **15.51%** (`tool_compression_saved`
9,722 tokens over the 62,666 baseline tool+schema prompt tokens). T1's own QA measured
**24.04%** under the isolated interpretation on its fixture set. T3 QA measured the seam
directly: `minify_tool_schema` on the corpus's tool array yields 3.55% — the corpus's 7 tools
simply do not contain the content T1/T3 compress, because the minifier's allow-list
(`_REDUNDANT_PARAMETER_DESCRIPTIONS` = exactly `email`) matches none of them.

**PM ruling.**

1. **The as-shipped number is the only release-gate number.** The customer's bill sees the whole
   prompt; a floor defined against a counter-reading that no customer experiences is marketing,
   not measurement. The 15.51%/24.04% "isolated contribution" figures are real but describe the
   transformer in isolation, not the product. This is the same principle already ratified for the
   P1 headline (AC-P1: ratio-of-sums — "what the customer's bill actually sees").
2. **But a category floor set on a corpus that doesn't contain the category's compressible content
   is a mis-specified instrument, not a product failure.** The tool-heavy floor (plan AC-T5 /
   Release Gate 1: "≥15% additional input token reduction vs. v1.2.0") was implicitly calibrated
   on tool payloads rich in redundant parameter descriptions and pretty-printed JSON. The pinned
   E2E corpus has 3.55% of such content at the seam. Failing 0.55% vs 15% here measures the
   corpus, not the feature.
3. **Therefore: dual-number publication, floor re-baselined on a corpus that can exercise the
   feature.**
   - **Publication contract (mandatory, every release doc and CHANGELOG entry):** both numbers are
     reported, labelled exactly as follows: `as-shipped marginal (ratio-of-sums, whole-prompt,
     vs v1.2.0 on corpus <checksum>)` and `isolated transformer contribution (ledger
     tool_compression_saved / baseline category prompt tokens)`. Never present the isolated
     number alone; never present the as-shipped number as the feature's capability. This mirrors
     the AC-P1g publication pattern: the blended figure exists only beside the eligible-subset
     figure, both named.
   - **The 15% tool-heavy floor is RETIRED as a gate on the current pinned E2E corpus.** It is
     re-baselined: the tool-heavy floor applies on a **tool-schema-heavy corpus fixture** whose
     tool sets contain content the minifier actually targets (verbose descriptions, pretty-printed
     parameter blocks — the realistic MCP/CRM shape), measured **as-shipped** (whole-prompt,
     ratio-of-sums). The new floor is **≥8% as-shipped on that corpus**. Rationale: T3 QA showed
     pretty-printed-client content yields 43.9% e2e and the T1 isolated reading is 15.5–24%; an
     as-shipped floor of 15% would require ~3× the seam content the realistic compact-client
     shape carries. 8% as-shipped is aggressive but reachable on a corpus built for the feature,
     and honest on compact clients where the honest seam number is ~1.2%.
   - **The minifier allow-list is NOT widened by this decision.** Stripping all parameter
     descriptions or adding name-heuristics removes tool-selection signal and would require
     re-running full provider function-calling QA (T3 AC1) for marginal savings on the dominant
     compact-client shape. Accepted as-is for v1.2.1: the conservative allow-list is a
     zero-semantic-risk trade and the schema-cache wins (100% hit rate on repeated tool sets)
     are where repeated-schema traffic actually saves. Widening semantics is deferred to a
     post-v1.2.1 evaluation card with its own QA contract.

**Consequence for the release gate:** on the re-baselined contract, the tool gate becomes
`tool-heavy ≥8% as-shipped on the new tool-schema-heavy corpus fixture` plus
`isolated contribution published beside it`. The current 0.55% on the general E2E corpus is
no longer a gate input; it remains a published number.

## Decision 2 — T3 schema floor: floor re-based to "no overhead + 20% where pretty-printing exists"; semantics not widened

**The numbers.** Compact-serialized clients (httpx/openai-python shape — the dominant real-world
shape): **1.22% e2e / 1.23% bytes / 0.98% tokens**. Pretty-printed (indent=2, MCP/log style):
**43.9% e2e** (mostly envelope whitespace from compact serialization). Uniform 3.58–3.71% across
all 4 schema scenarios on the general corpus. Conservation: 0 violations / 92 tools, proven by an
independent recursive checker. Reviewer precedent (t_129f50c6) measured ~12–13% on a 40-tool
corpus.

**PM ruling.**

1. **Semantics are NOT widened for v1.2.1.** Same reasoning as Decision 1 item 3: the 20% floor
   was set against expected "15–30% on verbose tool definitions," which assumed pretty-printed
   input. Compact clients — the dominant shape — simply don't carry 20% of removable overhead
   under a conservative allow-list, and widening to strip all descriptions trades tool-selection
   signal and a full AC1 QA re-run for savings that a schema cache (AC2, 100% hit rate) already
   delivers on the repeated-schema traffic where schema size matters most. Accepted: ~1.2% on
   compact clients is the honest cost of the conservative allow-list, and it is the right trade
   for a product whose differentiator is preserving function-calling quality.
2. **The 20% floor is RE-BASED to a conditional contract:**
   - **Compact-client gate (new):** `schema-heavy: >0% savings AND 0 conservation violations`.
     The product claim for compact clients is "never inflates, never breaks validation" — not a
     percentage. The 1.22–3.66% measured is published as evidence of exactly that, labelled
     `compact-client (dominant shape)`.
   - **Pretty-printed gate:** `≥20% schema overhead reduction on pretty-printed (indent≥2)
     client fixtures` — retained as a real gate, because on that shape the measured 43.9% shows
     the feature delivers what the floor promised. This keeps the original floor's ambition
     where the original floor's assumptions hold.
   - Both numbers published side by side with client-shape labels, per the same dual-number
     publication contract as Decision 1. Per-scenario uniformity (3.58–3.71%) should be reported
     to preempt any cherry-picking concern.
3. **Schema-cache savings are promoted into the schema-heavy claim.** The measured 100% cache hit
   rate on repeated tool sets (40/40 eligible turns, fingerprint invalidation verified) is the
   bigger customer win for schema-heavy agent traffic and belongs in the headline of the schema
   story, not a footnote. The gate's percentage floor is about one-shot minification; the
   cache is where repeated traffic saves. CHANGELOG must carry both.

## Decision 3 — Result-heavy gate: corpus extension with over-cap results; floor retained at 25%

**The numbers.** Result segment: 0.00% (55,145 → 55,145). Root cause (Part 2 §3.3, verified at
the seam): corpus tool results are 4,506 tokens — under `TOOL_RESULT_MAX_TOKENS=5000` — so
`optimize_tool_result` correctly no-ops. Over-cap truncation separately verified (80k chars →
12.5k chars). The plan's AC-R4 / Release Gate 3 floor (≥25% "on agent loop sessions" / "result
overhead reduction") is **unsatisfiable on any corpus whose results fit under the cap**: the gate
as written can only be exercised by a corpus that does not exist in the fixture set.

**PM ruling.**

1. **The 25% floor is CORRECT and RETAINED.** T4's designed behavior (40–60% on oversized
   outputs, plus filtering) makes 25% achievable and meaningful — *if* the corpus contains what
   the feature targets. The failure here is corpus-representativeness, exactly the class of
   mis-instrumentation ruled on in Decision 1 item 2. Lowering the floor to "pass" on a corpus
   that can't exercise the feature would be softening a gate to hide an untested capability —
   the opposite of what a release gate is for.
2. **The corpus is EXTENDED.** Authoritative spec for the extension (owner: application-developer;
   QA re-verifies):
   - Add a `result` segment of scenarios whose tool results are **over-cap** (>= 5,000 tokens,
     spanning 5k–80k chars, realistic shapes: `ls -la`-style file listings with noise lines,
     verbose JSON API responses, log dumps with repeated frames).
   - Include at least one scenario exercising **file-listing filtering** (AC-R3 class) and one
     exercising **repeated identical results** (cache path), so both mechanisms contribute
     measured savings rather than truncation alone.
   - Include at least one **at-cap boundary scenario** (4,900–5,100 tokens) to pin the cap
     boundary behavior in the benchmark, not just unit tests.
   - Corpus is checksum-pinned and committed alongside the existing fixture conventions
     (`e2e_corpus.json` + `.sha256`); k-sampling and ratio-of-sums measurement identical to the
     existing harness. **Do not** tune corpus content to a target percentage; scenarios are
     fixed from realistic shapes first, numbers measured after.
3. **Measurement contract unchanged:** ratio-of-sums on upstream-reported prompt tokens, both
   arms, same as the rest of the E2E A/B. The 25% floor is evaluated on the extended corpus only.
   The current corpus's 0.00% remains published as a control datum with the no-op explanation.
4. **Explicit note on the alternative rejected:** revising the floor down (e.g. 10%) was rejected
   because under-cap results have a designed, correct behavior of passing through untouched —
   any floor above 0% is unsatisfiable there, and 0% is not a floor. Re-baselining the floor
   while keeping a corpus that can't exercise the feature would institutionalize an unmeasurable
   gate.

---

## Consolidated acceptance-criteria updates (for the integration/release cards)

These replace the corresponding lines in the v1.2.1 release gate definitions
(`v1.2.1-tool-codebase-compression-plan.md` "Success Metrics" items 1 and 3, plan AC-T5, AC-S4,
AC-R4). Owners for the resulting implementation work are noted; the work itself is NOT this card.

| # | New/updated gate | Replaces | Owner of resulting work |
|---|---|---|---|
| G1 | Tool-heavy: ≥8% as-shipped (whole-prompt, ratio-of-sums vs v1.2.0) on a new tool-schema-heavy corpus fixture containing minifier-targetable content; isolated transformer contribution (ledger `tool_compression_saved` reading) published beside it, never alone. Minifier allow-list unchanged. | Plan AC-T5 / Release Gate 1 "≥15%" on the general corpus | application-developer (fixture corpus); qa-lead (re-measure) |
| G2 | Schema-heavy: (a) compact-client gate: savings >0% AND 0 conservation violations, percentage published but not gated; (b) pretty-printed gate: ≥20% on indent≥2 fixtures; (c) schema-cache hit-rate ≥80% on repeated tool sets (already passing at 100%) reported in the same claim. | Plan AC-S4 "≥20%" single-number floor | qa-lead (re-measure; semantics already shipped) |
| G3 | Result-heavy: ≥25% on the EXTENDED corpus (over-cap results 5k–80k chars, file-listing filter + repeated-result cache scenarios, one 4,900–5,100-token boundary scenario; checksum-pinned, realistic shapes fixed before measurement). Floor value unchanged. | Plan AC-R4 / Release Gate 3 "≥25%" on the current corpus | application-developer (corpus extension); qa-lead (re-run) |
| G4 | Publication contract (applies to all three): every published per-category figure carries both the as-shipped whole-prompt number and any isolated-contribution number, each labelled with population and corpus checksum; no figure may be published alone. Mirrors AC-P1g precedent. | (new, cross-cutting) | documentation owner, enforced by qa-lead |

**What does NOT change:** quality delta <2%, protocol integrity, tool-call parity, ledger
reconciliation, codebase ≥35%, aggregate ≥25%, all CI/testing/docs gates — none are touched by
these decisions, and the two P0s and P1s from the NO-GO remain release-blocking regardless of
these re-baselines.

## Sequencing note (for the orchestrator)

The NO-GO review card's recovery sequence step 4 ("docs alignment — after product decisions") can
now proceed against this note's publication contract; steps 1–2 (P0/P1 fixes) and the T2 dedup
rework are independent of these decisions and unblocked as they were. Re-benchmarking (G1/G3
measurement) requires the new fixture corpora; re-running the full release gate remains gated on
all recovery steps completing.

## Rejected alternatives (recorded so they are not re-litigated)

1. *Widen the minifier (strip all parameter descriptions / name-heuristics) to hit 20% on
   compact clients* — rejected: removes tool-selection signal, requires full AC1 provider
   function-calling QA re-run, buys savings only on a minority shape while the schema cache
   already covers the repeated-schema win.
2. *Adopt the isolated-contribution reading as the tool-heavy gate number* — rejected: a gate
   must measure what the customer's bill sees (ratio-of-sums precedent, AC-P1); isolated
   contribution is published context, never a gate.
3. *Lower the result-heavy floor to fit the current corpus* — rejected: the corpus cannot
   exercise the feature at any floor above 0%; the floor is meaningful only on an
   over-cap corpus, which this note commissions.
4. *Treat 0.55%/3.66% as proof T1/T3 are broken* — rejected by evidence: seam measurements show
   the transforms do what they claim on content that contains their target; the general corpus
   simply doesn't contain enough of that content. Conservation and parity are clean.
