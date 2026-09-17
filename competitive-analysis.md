# Competitive Analysis — AI Token Compression & LLM Cost Optimization (researched Sep 2026)

Research compiled by @product-manager, sourcing public docs/pricing (June–Sep 2026). Treat vendor benchmarks as self-reported unless noted.

---

## 1. The market landscape (four lanes)

| Lane | Players | What they optimize | Our relation |
|---|---|---|---|
| **A. AI Gateways** | Portkey, Helicone, LiteLLM, Cloudflare AI GW, Kong AI GW | Proxy + routing + caching + governance across many providers | **closest competitor** (we're a gateway-ish proxy too) |
| **B. Intelligent routers** | OpenRouter, Not Diamond | Pick cheapest acceptable model per prompt | complementary |
| **C. Compression APIs / OSS** | The Token Company, LLMLingua-family, Headroom | Shrink prompt tokens before the model | **direct competitor** on compression |
| **D. Endpoint/agent optimizers** | TokenShift (PointFive), rtk, lean-ctx | Compress traffic *on the dev machine* (coding-agent output) | adjacent, different deployment |
| **Provider-native** | Anthropic prompt caching (~90%), OpenAI (~50%), DeepSeek ~98% | Cheaper re-use of cached input | **the existential pressure** on pure compression |

**Market size signal:** worldwide AI spend ~ $2.59T in 2026 (+47% YoY); enterprise gen-AI spend tripled 2024→2025 ($11.5B→$37B); coding/dev tools = largest bucket ($7.3B). Win rate: ~40–70% of RAG/agent token budget is waste (re-sent history, formatting).

---

## 2. What competitors have that we don't (feature gap list)

**Gateways (Portkey / LiteLLM / Helicone / Kong):**
1. **Semantic caching** (embedding-similarity dedupe) — the single biggest lever; Portkey gates it behind paid tier, LiteLLM needs Redis/Qdrant, Kong needs Redis. Exact-prefix caching is the *baseline* everyone has; semantic is the differentiator.
2. **Semantic / cost-aware model routing** — pick cheapest acceptable model per prompt.
3. **Multi-tenant virtual keys** with per-key/team budgets, spend caps, rate limits, key rotation (LiteLLM's standout).
4. **Guardrails / policy / governance** — allow-lists, PII redaction, content filters.
5. **Fallbacks + load balancing + retries** across providers, provider-error normalization.
6. **FinOps dashboard** (Portkey) — per-team cost attribution, alerts, budgets.
7. **100+ provider breadth** (LiteLLM/Portkey) vs our 1 upstream today.
8. **Auto-synced pricing JSON** from providers (cost accuracy without manual tables).

**Dedicated compression:**
9. **Aggressive, high-quality compression** — The Token Company: 10–40% token cut at *full* (even improved) accuracy, cheap ($0.05/1M); LLMLingua up to 20× at mild loss; Headroom claims 60–95%. Our LLMLingua skeleton is competitive on mechanism but we claim nothing benchmark-verified.
10. **End-to-end (input + output) cost model** — TokenShift/lean-ctx measure output + context overhead, not just input. **This is our plan's stated differentiator (output conciseness) — competitors largely still ignore output.**
11. **Side-by-side / quality verification harness** — some products publish accuracy-preservation benchmarks (The Token Company, JetBrains independently A/B-tested rtk vs its claims).

**Provider-native (the threat):**
12. **Prompt caching orchestration** — we zero-in on this. Native caches are exact-match; a tool that *also* manages cache flags + reports cache-hit savings separately stays credible.

---

## 3. How they price (current, as researched)

| Product | Model | Anchor figures |
|---|---|---|
| **The Token Company** | usage-based ⨯ compression | **$0.05 / 1M tokens** compressed |
| **Portkey** | freemium → monthly | Free dev; **Production ~$49/mo**; Enterprise quote |
| **Helicone** | freemium (observability) | Free tier no token limit; paid for scale |
| **LiteLLM** | OSS + enterprise | OSS free; **Enterprise quote** |
| **Kong AI GW** | OSS + enterprise | OSS free; Enterprise quote |
| **TokenShift** | enterprise | **Custom enterprise pricing** |
| **LLMLingua family** | OSS | **Free (MIT)** |
| **Provider-native caching** | baked into provider price | 50–90% cached-input discount |
| Bitcoin "swap your provider's URL" | **our mooted model** | self-host free / future hosted tier |

**Pattern:** the paid-moat feature is **semantic caching + governance + multi-tenant** (behind Pro/enterprise), while **compression + exact-match-caching + observability** are increasingly commoditized-free or open-source. Nobody is extracting much for *output*-side conciseness yet.

---

## 4. Our positioning wedge (validated by research)

Course holds. The PointFive guide independently lands on the same framing:
- Gateways cover "application-side LLM traffic"; a **compression-focused optimization layer** is a distinct, defensible lane.
- **"Cheaper than Portkey, simpler than LiteLLM, and the only tool attacking input ⨯ output cost as one problem."**
- Anti-compression competitive reality: provider-native caching eats pure *input-compression* value → we must be **caching-orchestration-first, compression-second**, and report cache savings separately (honesty = trust = conversion).

---

## 5. Recommendation — what we MUST build vs. NICE-to-have

### MUST-HAVE (P0 — needed to be credible & sellable; all already in spec v2 Phase A)
- **Multi-provider with real adapters** (Anthropic + OpenAI family + OpenRouter/xAI/Google + local). Non-negotiable for the "drop-in across your stack" claim.
- **Provider-native prompt-caching orchestration** + cache-savings reported separately from compression. This is how we stay honest & credible *against* native caching.
- **KPI API + charts dashboard** (money-visibility) — every competitor has some cost dashboard; we ship ours with honest cache-vs-compression attribution.
- **Distributed, single-command install** (`pip install` / `docker run`) — partners' biggest adoption gate.
- **Exact-prefix caching** (everyone has it; cost = baseline).

### STRONG SHOULD-HAVE (P1 — the real differentiators that win vs. LiteLLM/Portkey)
- **Lossless L1 structural cleanup — the measured wedge (post-audit endorsement).** The proxy sits on the request path and can strip applied structural waste — pretty-printed JSON scaffolding, duplicate/empty system blocks, dead RAG metadata — with **zero quality risk and no judge required**: no rubric to defend. **Measured on the committed corpus: ~29.9% input-token reduction from JSON whitespace compaction alone (C1-only, the conservative floor), 70.4% with C1+C2+C3** — at **100% answer retention** (`run_l1_benchmark.py --production-path`, divergences gated, results committed). This is honest savings a customer can verify in one command, checksum-pinned — which is exactly the trust claim the market punishes generic "X% compression" marketing for lacking (the JetBrains/rtk lesson: claims get independently tested).
- **Honest, calibrated measurement as the brand** (post-audit, supersedes the output-conciseness push). The 2026-09 audit found the draft output-conciseness benchmark **statistically uninterpretable** (mean-of-ratios with one sample per arm collapses toward 0 regardless of true effect). The corrected method is preregistered in spec v2 (AC-P1/P1a/P1b/P1c) and is **gated by an empty-box calibration** (`benchmark/empty_box.py`): the estimator must read ~0 on a synthetic null effect and resolve a synthetic 15% effect before it may spend provider money. The publishable claim is "we can prove what we measured, and what we couldn't measure we didn't claim" — the wedge competitors are structurally unable to copy quickly.
- **Output-side conciseness** — **not** the headline anymore (GLM-class always-on thinking makes it null-by-construction; the audit re-opened the enable-by-default decision, feature is currently off). Full in+out cost model remains on the roadmap but must re-earn enablement through the calibrated harness first.
- **Semantic caching (Phase C, deferred)** — the top paid-moat feature; brings us into "even more than competitors" territory on the roadmap.
- **Focused multi-tenant virtual keys** (Phase C) — needed to sell to teams.

### NICE-TO-HAVE (P2 — later / skip)
- Semantic/cost-aware model routing (OpenRouter/Not Diamond lane) — complementary, not core.
- Guardrails/governance suite, PII redaction — enterprise feature; defer.
- 100+ provider breadth — start with 6 high-value, not 100.
- Huge OSS provider breadth + auto-synced pricing — LiteLLM's lane; we don't win that.

---

## 6. Priority summary for @user

**Order of build (ties to spec v2 phases):**
1. **Phase A (must):** multi-provider + adapters, KPI API, dashboard, exact-prefix cache, Postgres-native. *(already spec'd, gated on your v2 review)*
2. **Phase B (should):** **lossless L1 structural cleanup is the measured wedge — ship it, publish the one-command reproducible savings numbers** (~29.9% C1-only floor / 70.4% full of input tokens on RAG/JSON-heavy traffic at 100% retention), and make **honest calibrated measurement** the brand (empty-box-gated benchmark per spec AC-P1a-gate); dashboard polish.
3. **Phase C (should/later):** semantic caching on pgvector, multi-tenant keys/quotas.

**Biggest strategic call:** invest in **lossless L1 structural savings + honest, calibrated verification** — the wedge the field treats as commodity marketing, made provable; that's where a small, fast team can credibly lead rather than out-gatewaying Portkey/LiteLLM.

---

*Sources: pointfive.co tier guides (2026), thetokencompany.com + YC listing, portkey.ai pricing + TrueFoundry guide, litellm docs (BerriAI), kong/docs, helicopters docs, redis.io + awesome-llm-token-optimization + awesome-ai-tokenomics GitHub lists, jetbrains.com rtk A/B. Price/feature claims are vendor-published and may change; benchmarks flagged as self-reported where applicable.*