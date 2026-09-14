# AI Token Compression Proxy — Project Plan

A drop-in proxy that reduces LLM API token costs (input and output) while preserving answer quality, built as a self-hosted/local-first tool using your own BYOK (bring-your-own-key) API access.

---

## 1. Overview

**Core value proposition:** "Change one line of code, keep your existing API calls exactly the same, and cut your LLM token bill — without losing answer quality."

**Who it's for:** Solo AI app builders and small teams who feel real LLM spend pain but can't justify enterprise-priced tools or the effort of self-hosting an open-source library.

**How it works:** The user points their existing app at your proxy instead of the LLM provider directly (only the `base_url` changes). The proxy intelligently compresses the input prompt/context and encourages more concise output, then forwards the request to the real provider using the user's own API key. Nothing is stored or resold — you're selling the optimization layer, not the underlying model access.

---

## 2. Market Landscape (as of research date)

This is a real, validated category — not virgin territory. Known players:

| Player | What it does |
|---|---|
| **LLMLingua / LLMLingua-2** (Microsoft Research) | Open-source technical baseline. A small model scores tokens by information value and drops low-value ones. LLMLingua-2 uses a fast BERT-based classifier. |
| **The Token Company** (YC W26) | Commercial compression API, drop-in middleware, compresses ~100k tokens in under 100ms, claims accuracy gains alongside cost cuts. |
| **TokenShift** | Endpoint-local compressor specifically for coding agents; runs locally so code never leaves the device; ~12–21% average reduction. |
| **Headroom** | Open-source (Apache 2.0), fast-growing, claims 60–95% reduction across agents/RAG/logs/code; multiple deployment modes (library, proxy, MCP server). |
| **Portkey / LangChain** | Compression as a built-in feature of a broader gateway/framework, not a standalone product. |

**Conclusion:** don't try to out-research the core compression algorithm (that's a genuine ML research problem, already iterated on by a serious research team). Differentiate on product-level gaps instead (see below).

---

## 3. Differentiation Strategy

Two real, documented gaps exist in how current tools apply compression — neither requires new ML research to address:

1. **The output-token gap.** Research surveying major compression papers found none of them account for output token costs — only input. Output tokens are usually priced higher than input tokens. A tool that treats input compression *and* output conciseness as one unified cost problem addresses something the whole category currently ignores.

2. **Compression is task-dependent, and most tools apply it uniformly.** Research shows compression tends to help or be neutral on QA/summarization/conversational tasks (sometimes even improving accuracy by removing noise), but can hurt code generation and tasks requiring precise technical specs. A product that automatically detects task type and adjusts compression aggressiveness — or skips it entirely for code/exact specs — is a real, buildable differentiator.

**Product differentiators to build:**
- Task-aware routing (compress hard on chat/context-heavy requests, skip or lightly touch code/precise specs)
- Output-side conciseness handling (not just input compression)
- Transparent, honest quality verification (side-by-side before/after comparison) rather than blanket "zero quality loss" marketing claims
- Dead-simple integration (true drop-in proxy, zero code rewrite) vs. self-hosting OSS or enterprise sales cycles

---

## 4. Target Audience & Positioning

- **Primary audience:** solo indie developers and small teams building AI features, who are priced out of enterprise tools and don't want to manage/self-host open-source infrastructure.
- **Positioning line:** "We only really win if you save money" — pairs well with a savings-based pricing model.
- **Not the target:** large enterprises (too slow a sales cycle for a solo builder) or consumers (this is a developer/B2B tool, not B2C).

---

## 5. Business Model & Billing Strategy

**Key risk flagged:** unlike most free SaaS tools, this product's infrastructure cost scales almost exactly with usage — a heavy user costs more to run *in direct proportion* to how much they use the product. This means "fully free and unmetered while building an audience" is dangerous — a single high-volume user could generate a large, unbounded bill before there's any revenue.

**Recommended approach (pick one, or combine):**
- **Volume-capped free tier** — e.g., 1M tokens/month free. Generous enough to fully evaluate the product, bounded enough that worst-case cost exposure is known.
- **Percentage-of-savings pricing from day one** — no charge on the first $X saved per month, then a cut of savings beyond that. This keeps revenue and cost moving together automatically.
- **Hard rate limits regardless of model chosen** — non-negotiable from day one, to stop a single heavy user (or bad actor) from generating an unbounded bill while monetization isn't yet active.

*(Guardrails/anti-abuse specifics — to be designed later, flagged as a follow-up item.)*

---

## 6. Infrastructure & Hosting Plan

### Cost reality check
- The LLM inference cost itself is the user's own (BYOK) — not your cost.
- Your costs: the proxy server itself, the compression model's live compute per request, and dashboard/database/auth — all of which scale with token volume, the same metric that signals product-market fit.

### Domain name
- ~$10–15/year (e.g., Namecheap, or Cloudflare at-cost ~$9–10/year for a .com). SSL is free via Let's Encrypt/Cloudflare.

### Stage 1 — Local development (current stage)
- Run everything on your own PC. Cost: effectively $0/month beyond the domain.
- **Obstacles to know about:**
  - Residential ISPs often assign dynamic IPs, and many put you behind CGNAT (no real public IP at all — port forwarding won't work).
  - Upload speed (not CPU) is the real bottleneck — residential plans are asymmetric.
  - Uptime is tied to your PC/power/ISP — fine for testing, not for a public launch.
  - Some residential ISP terms technically restrict running servers (rarely enforced at hobby scale).
- **Fix: Cloudflare Tunnel.** Free, outbound-only connection from your PC to Cloudflare, which handles the public domain, HTTPS, and routing. Works even behind CGNAT, no port-forwarding, no exposing your home IP.
- **Critical practice: containerize everything in Docker from day one**, even locally. Makes the later move to a real server a simple `docker compose up` + repoint domain, not a rebuild.

### Stage 2 — Move to a real server (once there's real usage signal)
- **CPU-only start is fine.** LLMLingua-2's model is a small BERT-class classifier (~110–340M params) — runs fine on CPU at moderate volume, no GPU needed yet.
- **Recommended spec:** 8 vCPU / 16GB RAM.
- **Recommended provider: Hetzner** (cheapest reliable option, ~2–3x cheaper than DigitalOcean/Vultr for equivalent specs, generous bandwidth included).
  - Estimated cost: **~$15–30/month** all-in (domain + server).

### Stage 3 — Scale to serverless GPU (only if/when CPU latency becomes a real bottleneck)
- Move just the compression step (not the whole proxy) to a serverless GPU provider like **RunPod**.
- Pay-per-second billing, scales to zero when idle — cost moves in lockstep with usage rather than being a fixed cost.
- Entry-level GPU tier starts around **$0.58/hour of active compute** — because compression calls are quick, even meaningful daily volume can land well under $20/month in GPU cost.
- Keep the lightweight proxy/auth/dashboard layer on the cheap always-on CPU box; only the compression inference moves to serverless GPU.

---

## 7. Growth & Analytics Expectations (honest, not hype)

**Launch channels considered:**
- **Show HN** — 10M+ monthly HN visitors overall, but front-page visibility window is short (2–6 hours) and ~20–30 posts compete daily. Can send a lot of traffic if it lands; most posts don't reach the front page.
- **Product Hunt** — ~200 products launch per day on the same 24-hour clock. A well-prepped launch (personal network mobilized in the first 4 hours) can meaningfully multiply normal traffic/signups; a quiet launch gets much less.
- **Organic (Reddit dev communities, Indie Hackers, dev Twitter, search)** — slow trickle at first, tens of visits/week until backlinks and search ranking build up.

**Realistic first-month total traffic (solo launch, no existing audience):** low hundreds to a couple thousand visitors is a reasonable expectation; a viral HN hit could reach 5,000–10,000+; a quiet launch could be under 300. High variance — don't plan finances around a specific number.

**Conversion benchmarks (freemium model):**
- ~9–16% of visitors become free signups.
- ~2–5% of free users eventually convert to paid — but this typically plays out over **months**, not the first 30 days, since users need real usage/trust before paying.

**Illustrative funnel (not a guarantee):**

| Scenario | Visitors (month 1) | Free signups (~12%) | Paying users (month 1) |
|---|---|---|---|
| Quiet launch | ~500 | ~60 | 0–2 |
| Solid launch | ~1,500 | ~180 | 1–5 |
| One channel goes viral | ~5,000+ | ~600+ | 3–15 |

**Takeaway:** month one is about signups and feedback, not revenue. Meaningful paid conversion tends to show up in months 2–4.

**Recommended analytics tooling:**
- **Umami** or **Plausible** (self-hostable, lightweight, privacy-friendly) for traffic tracking.
- **PostHog** (generous free tier) for actual product usage — signups, activation, who's really sending requests through the proxy.

---

## 8. Technical Implementation Roadmap

### Tech stack
- **Language/framework:** Python + FastAPI (async, matches the ecosystem LLMLingua is built in — avoids cross-language complexity).
- **Compression engine:** `llmlingua` (LLMLingua-2 variant — fast, open, BERT-based).
- **Tokenizer/cost counting:** `tiktoken`.
- **HTTP forwarding:** `httpx`.
- **Logging/storage:** SQLite (simple, local, no extra service needed at this stage).
- **Containerization:** Docker + Docker Compose.
- **Tunneling (local phase):** Cloudflare Tunnel.

### Project structure
```
token-saver/
├── docker-compose.yml
├── .env                    # API key lives here, never committed
├── proxy/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py             # FastAPI app, routes
│   ├── classifier.py       # decides compress / pass-through
│   ├── compression.py      # LLMLingua-2 wrapper
│   ├── stats.py            # token/cost logging + aggregation
│   └── config.py
└── test/
    └── compare.py          # side-by-side quality check
```

### Phase 0 — Environment setup
- Install Python 3.11+, Docker, Docker Compose.
- Set up the project structure above.

### Phase 1 — Barebones passthrough proxy
- FastAPI app exposing `/v1/chat/completions` (OpenAI-compatible shape — the most widely supported format, so existing tools can point at it by changing only the base URL).
- Reads the incoming request, forwards it untouched to the real provider using the `Authorization` header the client sent (BYOK key passes straight through — the proxy never stores it), returns the response untouched.
- **Test before moving on:** point any existing script at `http://localhost:8000/v1/chat/completions` (same key) and confirm identical behavior to hitting the provider directly.

### Phase 2 — Baseline token counting
- Add `tiktoken` to count input/output tokens per request.
- Log every request's token counts and estimated dollar cost to SQLite — *before* adding compression, to get a real, verifiable "before" baseline.

### Phase 3 — Add the compression engine
- Integrate LLMLingua-2 to compress the prompt/message content before forwarding.
- Log before/after token counts side by side in the same SQLite table.

### Phase 4 — Task-aware routing (the actual differentiator)
- Simple heuristic classifier (no ML training needed): detect code fences, file extensions, `def `/`class `/`import` keywords, JSON/structured markers → route to "pass through, don't compress." Everything else → route to "compress."

### Phase 5 — Output-side conciseness
- For requests classified as conversational, inject a lightweight system instruction discouraging restating the question or padding the answer.
- **Note the distinction:** this is *not* true output compression (which would mean a second pass/second LLM call to shrink an already-generated answer — extra cost and risk of altering meaning). This approach gets the model to generate fewer tokens up front, at no extra cost or risk.
- Log output token counts before/after this change.

### Phase 6 — Quality verification harness
- `test/compare.py`: run 20–30 real prompts twice (direct vs. through the proxy), compare token counts and answer quality side by side.
- Run this on real workloads before trusting or showing the tool to anyone.

### Phase 7 — Containerize
- Wrap everything in `docker-compose.yml` — the whole stack starts with one `docker compose up`. Makes the later move to a real server or Cloudflare Tunnel a non-event.

### Phase 8 — Aggregate monitoring (identified gap — not yet in earlier phases)
- A `/stats` endpoint (or minimal local page) querying the SQLite log for: total tokens processed, total tokens saved, estimated dollars saved — broken down by day and by route (compressed vs. pass-through).
- Simple aggregation over data already being collected in Phase 2/3 — no new instrumentation needed.

**Suggested build order:** get Phases 1–3 working first for an honest "it compresses and I can see the number" loop as fast as possible. Phases 4–8 turn it into a real, differentiated, trustworthy product.

---

## 9. Testing With Your Own BYOK Key

1. Store your real key in `.env` (`ANTHROPIC_API_KEY=...` or `OPENAI_API_KEY=...`) — never hardcoded, never committed.
2. Change one line in any existing script/app: swap the `base_url` to `http://localhost:8000`, keep the same key.
3. Run your own real prompts through both the direct path and the proxy path, and compare the SQLite logs — token counts, estimated savings, and the Phase 6 quality-check output.

---

## 10. Open Questions / Next Steps

- [ ] Design specific anti-abuse guardrails and rate limits (flagged, not yet detailed)
- [ ] Decide final pricing model: volume-capped free tier vs. percentage-of-savings vs. hybrid
- [ ] Build Phase 1 (barebones proxy) and validate end-to-end before adding compression
- [ ] Once real usage data exists, revisit the Stage 2/3 infrastructure migration timing
