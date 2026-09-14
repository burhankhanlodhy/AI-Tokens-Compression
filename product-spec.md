# AI Token Compression Proxy — Product Spec v1

Status: Draft for review
Author: @product-manager
Gates: This spec unblocks @ui-ux-engineer (flows) and @application-developer (impl). Project Manager finalizes the task board from it.

---

## 1. Problem & Goal

**Problem (user-facing):** LLM API bills grow with token volume. Existing compression tools treat only *input* tokens, apply compression uniformly regardless of task, and make quality claims that can't be verified — so solo builders and small teams can't trust a cheap, easy, drop-in cost-cutter.

**Goal (this build):** Ship a self-hosted, BYOK, dockerized **AI token compression proxy** that is a true drop-in (change one `base_url`, keep the same key), reduces total (input + output) cost, is task-aware, and lets the user *see and verify* the savings. Right now the project is a working passthrough core with several verified runtime bugs + no monitoring/DX surfaces.

**Product statement (from plan, must stay true):** "Change one line of code, keep your API calls exactly the same, and cut your LLM token bill — without losing answer quality."

---

## 2. Target Users & Jobs (summary)

- **Primary:** solo indie developers / small teams building AI features, priced out of enterprise tools.
- **Job 1:** point an existing app at the proxy with zero code rewrite and keep it working.
- **Job 2:** see, on first run, that the proxy is actually saving tokens and dollars.
- **Job 3:** trust that quality isn't silently degraded (side-by-side verify, honest numbers).

---

## 3. Feature Set (prioritized)

**P0 — Core proxy must work (drop-in, correct, observable):**
- F1. OpenAI-compatible `/v1/chat/completions` passthrough + compression + output conciseness.
- F2. OpenAI-compatible `/v1/embeddings` passthrough that actually works.
- F3. Health endpoint (`/health`) returning 200 and a simple status payload.
- F4. Startup must never crash (no uncaught worker/thread errors).
- F5. Every request logged to SQLite with before/after token counts + estimated cost.

**P1 — Monitoring / savings visibility (the "I can see it" loop):**
- G1. `/stats` — human-readable and JSON: totals (tokens processed, tokens saved, dollars saved), broken down by day and by route (compressed vs. pass-through).
- G2. `/metrics` — valid Prometheus text exposition format (content-type `text/plain; version=0.0.4`), scrape-able by any Prometheus setup.

**P2 — DX / operators:**
- H1. README: one-command run story, BYOK setup, base_url swap, how to read the numbers.
- H2. `.env` env-config (already exists in `config.py`) documented; real keys never in the repo.
- H3. `docker-compose up` runs the whole stack (must work — Phase 7 was never completed/verified).
- H4. `.gitignore` covers `.venv/`, `.env`, `data/*.db`, `__pycache__/` — no secrets or runtime artifacts committed.

**Deferred (confirmed out of scope for this sprint):** Task-aware routing (Phase 4 classifier) and side-by-side quality verify harness (Phase 6) exist in code/plan but are NOT part of this bugfix/monitoring sprint. Anti-abuse/rate-limit finalization deferred pending @user scope decision (see §5).

---

## 4. Acceptance Criteria

The proxy is "done" when all of the following can be demonstrated:

### Core reliability (P0) — verified by QA
- AC-1. `pytest` suite green (all existing tests + new regression tests added by dev).
- AC-2. `POST /v1/chat/completions` with a valid OpenAI-shaped body and `Authorization` header returns the upstream response with correct status/content-type; the client's key is forwarded, never stored.
- AC-3. `POST /v1/embeddings` with a valid body returns 200 and a valid embeddings response. **Regression:** must not raise `NameError: out_headers`.
- AC-4. No duplicate route registration: `/health`, `/v1/embeddings`, `/v1/metrics` (as appropriate) each map to exactly one handler. **Regression test asserts each is registered once.**
- AC-5. `GET /health` returns HTTP 200 with a short JSON status payload (e.g. `{"status":"ok"}`).
- AC-6. Server starts clean: uvicorn boots with no uncaught exception on any worker/thread (rate-limiter/prometheus included). **Regression: startup within test**.
- AC-7. Every proxied request writes a row to SQLite: input_tokens (before/after), output_tokens, model, route, estimated cost. No request path throws due to logging.

### Monitoring surfaces (P1) — verified by QA
- AC-8. `GET /stats` returns 200 and aggregates correctly: totals + per-day + per-route breakdowns, matching the underlying SQLite rows.
- AC-9. `GET /metrics` returns HTTP 200, content-type `text/plain; version=0.0.4`, with real newline-delimited Prometheus lines and numeric samples (not a JSON-escaped string, not literal `\n`). No undefined variables (`t`, `up`, `out_headers`) anywhere in the served output.

### UX / first-run experience (P1) — defined by @ui-ux-engineer, verified by QA
- AC-13. `/stats` renders: header band with lifecycle totals (requests, tokens saved raw + %, est. $ saved); tabs `By day` (default) / `By route` / `By model`; route chips filter the detail table.
- AC-14. **Empty state:** no-requests-yet hero copy + a one-line CTA pointing to the BYOK endpoint. No zero-padded charts, no empty tables. **Required.**
- AC-15. **First-run flip:** on the first request after an empty deployment, the header's "tokens saved" transitions from `0 (0%)` to the first real non-zero number. **Assert this exact transition in QA.**
- AC-16. Loading = skeleton rows; error = inline "Couldn't load stats" + retry button (never a broken/blank page).

### DX (P2) — verified by @pm + QA
- AC-10. `docker compose up` (or the documented single command) starts the full stack healthily and serves `/health`.
- AC-11. README documents: prerequisites, `.env` setup (which vars, what they mean), the one-line `base_url` swap to integrate an existing app, and how to read `/stats` and `/metrics`.
- AC-12. `.gitignore` excludes `.venv/`, `.env`, `data/*.db`, `__pycache__/`; `git status` shows no secrets or venv bytes staged.
- AC-17. **(P1 hardening)** `init_db()` sets `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`; concurrent `/v1/*` traffic + `/stats` yields no `database is locked` errors.

---

## 5. Open Scope Decisions (blocking Dev task board; need @user judgment)

These are the two flags @project-manager raised. Default recommendation stated; needing sign-off:
- **S1. `order_book.py`** — unrelated trading code committed to this repo. Recommend **REMOVE** (it is not part of the product).
- **S2. `rate_limit.py`** (~550 lines) — imports an undefined global `config`; appears dead/over-engineered relative to actual request path. Recommend **consolidate** — either wire it to real config or strip to the thin, working middleware the proxy actually needs. Do not blind-delete without an owner decision.

---

## 6. Team Handoffs

1. **@project-manager** — finalize task board from this spec (F1–F5, G1–G2, H1–H4 + S1/S2). Add: T10 `/stats` frontend build (AC-13..16) owned by @application-developer.
2. **@ui-ux-engineer** — design the `/stats` + `/metrics` surfaces and the "see your savings on first run" flow (AC-8, AC-9, H1). Flow complete; copy strings passed to PM.
3. **@application-developer** — fix the 4 verified bugs (embeddings, duplicate routes, metrics format, `main.py:147` tautology), implement AC-1..17, resolve S1/S2, and build the `/stats` frontend (AC-13..16) alongside T8.
4. **@database-administrator** — review SQLite schema/indexing + migration safety for the `requests` table alongside Dev.
5. **@qa-lead** — once Dev lands fixes, run regression + new tests for embeddings, metrics formatting/content-type, route uniqueness, startup, and stats accuracy (AC-2..9).

---

*Revision history: v1 initial spec by @product-manager.*