# Token-savings techniques in the market — sourced research

Author: @project-manager · Requested by @user (2026-09-18) · For: @product-manager (folds into `competitive-analysis.md` §2)

> **Provenance note (2026-09-19):** this file is a **reconstruction**. The original
> (10,233 bytes, written 2026-09-18) was untracked and was destroyed by a `git clean`
> of untracked root files during Phase B row B6. It is rebuilt from the sourced
> digest posted to the team room and the original source list, both of which survived.
> Figures and citations below are the same ones the room acted on; the prose is
> re-authored. **This copy is committed** — that is the whole lesson of the incident.
>
> **Reading rule:** every number here is either (V) vendor/self-reported marketing,
> (P) peer-reviewed or preprint measurement, or (E) engagement/anecdote. They are
> **not** interchangeable, and the tag matters more than the number. Ours are the
> only figures in this document reproducible from a committed fixture by a reader.

---

## Headline finding

**The industry's big percentages are not measuring what we measure.** There are five
distinct levers. Every "we cut LLM cost 80%" claim is one of them, or a stack of
them presented as one. Four of the five are **input-side or call-avoidance**; ours is
the only one that attacks **output tokens**, and the only one that is **lossless**.

Ranking by raw headline number: **routing > native caching > semantic cache >
prompt compression > us.** Ranking by *verifiability*: we are alone.

---

## 1. Provider-native caching — the free floor

Reprices tokens you still send. Exact-prefix only, zero token reduction, zero quality
risk, and **on by default** at several providers — which means it is the floor every
other lever competes against, not an advantage anyone owns.

| Provider | Discount on cached input | Notes |
|---|---|---|
| OpenAI | **50% off** (V) | automatic, no API change |
| Anthropic | **90% off** cache reads (V) | writes cost **1.25x** (5-min) / **2.0x** (1-hr); breakeven ≈ **1.4 reads** |
| Gemini 2.5 | **90%** implicit (V) | Gemini 2.0 was 75% |
| DeepSeek | **~90%** (V) | |
| Batch APIs (OpenAI / Vertex) | flat **50%** (V) | latency traded for price |

Two consequences the marketing skips:

- The Anthropic 1-hour cache tier needs roughly a **67% hit rate** to beat the 5-minute
  tier — the longer TTL is not free, and below that threshold it is a loss.
- One documented engagement moved cache hit rate **7% → 84%** purely by relocating
  volatile prompt content to the end of the prompt, cutting total LLM cost **59% over
  9.8B tokens** (E). A large share of the "80% savings" folklore in circulation is
  **correct prefix hygiene**, not a product.

Sources: `platform.openai.com/docs/guides/batch`, `ai.google.dev/gemini-api/docs/caching`,
`docs.cloud.google.com/.../context-cache-overview`,
`prompthub.us/blog/prompt-caching-with-openai-anthropic-and-google-models`,
`technspire.com/en/blog/prompt-caching-2026-real-cost-wins`,
`digitalapplied.com/blog/prompt-caching-2026-cut-llm-costs-engineering-guide`,
`introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025`,
`simonwillison.net/tags/prompt-caching/`.

## 2. Prompt compression — our direct lane (LLMLingua family)

The lane closest to what we do, and the one whose published claims diverge most from
its own literature.

- Microsoft **LLMLingua** headline: *"up to 20x"* (V); its own demo shows **11.2x** on a
  chain-of-thought prompt (V).
- **LLMLingua-2**'s published operating range is **2x–5x** (P), 3–6x faster than prior
  compressors, **1.6–2.9x** end-to-end latency improvement (P).
- The replication literature is blunt: at **2x compression all methods sit within ~4%
  of each other** (P), and differentiation only appears at aggressive ratios — which is
  exactly where accuracy degrades.
- Stanford's "lost in the middle" work shows accuracy drops **15–47% as context grows**
  (P) — compression is a *quality* lever, not only a cost lever. This cuts both ways and
  is the strongest honest argument for the whole category.

Structural costs nobody prices: you run **a compressor model in the request path**, and
the output is **neither human-readable nor round-trippable**.

Commercial players in this lane: **The Token Company** 10–40% at "full accuracy",
$0.05/1M (V); **Headroom** 60–95% (V). Both self-reported, neither reproducible.

Sources: `microsoft.com/en-us/research/project/llmlingua/`, `llmlingua.com/llmlingua2.html`,
`aclanthology.org/2024.findings-acl.57.pdf`, `arxiv.org/abs/2307.03172` (lost in the middle),
`arxiv.org/html/2403.12968v2`, `arxiv.org/html/2410.12388v2`,
`prompthub.us/blog/compressing-prompts-with-llmlingua-reduce-costs-retain-performance`,
`compresr.ai/blog/compression-ratio-vs-accuracy-llm-context-tradeoffs`,
`pointfive.co/guides/top-prompt-compression-solutions-2026`,
`klu.ai/glossary/lossless-context-compression`.

## 3. Semantic caching — the paid moat, and the lever we do not have

- **GPTCache** reports **61.6–68.8% hit rates at 97%+ hit accuracy** (V/E); commercial
  vendors target **30–60%** (V).
- Roughly **31% of real queries are semantically similar to a prior one** (P) — that is
  the ceiling this lever is chasing.
- The *GPT Semantic Cache* work pins **cosine threshold 0.8** as optimal (P): above it
  hit rate collapses, below it you **serve wrong answers**. That single number *is* the
  risk profile of the entire technique, and it is why this is a QA-gated feature for us
  rather than a config flag.

Sources: `github.com/zilliztech/GPTCache`, `arxiv.org/html/2411.05276v2`,
`spheron.network/blog/semantic-cache-llm-inference-gpu-cloud/`.

Direct relevance to us: this is **Phase C** (AC-PC1..PC5), off by default until AC-PC4's
cross-tenant/calibration/invalidation gate is green.

## 4. Routing / cascades — the biggest published number

- **RouteLLM** (Berkeley / LMSYS): **>85% cost reduction on MT-Bench at 95% of GPT-4
  quality**, sending only **14% of queries** to the strong model; **45%** on MMLU, **35%**
  on GSM8K (P).
- **FrugalGPT**: up to **98%** via cascades (P).

The caveat the marketing drops: these are **benchmark-distribution** numbers, and the
quality guarantee is **probabilistic** — a per-request floor does not exist. We do not
do routing at all.

Sources: `lmsys.org/blog/2024-07-01-routellm/`, `github.com/lm-sys/routellm`,
`arxiv.org/abs/2406.18665`, `arxiv.org/abs/2305.05176` (FrugalGPT),
`neuraltrust.ai/blog/llm-model-routing`, `burnwise.io/blog/llm-model-routing-guide`.

## 5. Gateways create no savings

**Portkey, LiteLLM, Helicone, Cloudflare AI Gateway, Kong, Zuplo** package levers 1–4
and resell them. **None reduce output tokens. None offer a lossless, verifiable
transform.** Their real moats are provider breadth (100–250+ providers vs. our handful)
and observability.

Sources: `portkey.ai`, `litellm.ai`, `helicone.ai`, `developers.cloudflare.com/ai-gateway/`,
`blog.cloudflare.com/ai-gateway-is-generally-available/`,
`zuplo.com/learning-center/best-ai-gateway-buyers-guide`,
`guptadeepak.com/tools/top-5-ai-gateways-2026/`.

---

## Us vs. them

Our figures, with their provenance — the point is the provenance, not the size:

- **57.71pp output-token reduction** — non-code non-RAG population, n=10/55,
  quality delta **−0.35pt**, **ratio-of-sums**, empty-box-calibration-gated.
- **L1 lossless structural cleanup** — **29.9% input (C1-only floor) / 70.4%
  corpus-weighted aggregate (production-default C1+C2+C3 arm)** at **100% answer
  retention**, with round-trip equivalence.

Levers 1–4 are **all** input-side or call-avoidance. Our edge is three things:

1. **Lossless, not lossy.** Everyone else drops tokens and needs a rubric to defend it.
   Ours is a deterministic pure function a customer verifies **in one command**.
2. **We attack the output side of the bill.** On thinking/reasoning models output is
   frequently the majority of the invoice, and every tool above pays full freight on it.
3. **Measurement is the product.** Empty-box calibration, the RAG carve, the live
   tripwire. The market has already punished unverified claims — JetBrains independently
   A/B-tested a compression tool against its own marketing.

**Where they beat us, no spin:** semantic caching (30–68% hit) is the largest lever we
lack and it sits in Phase C; routing (35–85%) we do not do at all; **native caching is
free and structurally caps what pure input-side compression can ever be worth**; and
LiteLLM/Portkey carry 100–250+ providers to our handful.

These levers **compose rather than compete**. The pitch is therefore not "we save more
than Portkey" — it is **"we save on the axis your gateway and your provider both ignore,
and we can prove it."**

---

## Decisions this research fed

- **Semantic caching stays Phase C** (@user ruled, 2026-09-18) — recommendation was to
  hold the line rather than pull it into Phase B, because L1's lossless wedge is the
  only item here nobody can copy quickly.
- **57.71% stays pinned** — no re-litigation of the headline.
- @product-manager to fold this into `competitive-analysis.md` §2 as the **sourced**
  version of the feature-gap list.
