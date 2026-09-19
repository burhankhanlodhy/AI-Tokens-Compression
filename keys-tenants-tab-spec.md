# Keys & Tenants tab — implementation spec (UI/UX)

Author: @ui-ux-engineer · For: @application-developer (implementation) + @qa-lead (gates) + @product-manager (review)
Status: LANDED (frontend, C6 @ui-ux-engineer 2026-09-19) — §2–§4.5 implemented in `proxy/static/dashboard.js` + `dashboard_v2.py` CSS, gated in `test/test_keys_tab_render.py` (15 tests) via the extended render harness; **§3 backend endpoints (`GET /api/tenants`, `GET /api/keys`, `POST /api/keys`, rotate, revoke + `ADMIN_TOKEN` bearer gate) are still @application-developer scope** — until they land, the live tab surfaces its error state (`Couldn't load key management.` + Retry), never a blank panel.
Extends: `dashboard-ac-a8-spec.md` (same design language, same 1:1 contract discipline) — this is the four-tab shell's existing placeholder tab, built as an extension of `proxy/dashboard_v2.py` + `proxy/static/dashboard.js`, NOT a new surface.

---

## 1. Purpose & boundaries

The tab turns the placeholder at `renderKeys()` (`dashboard.js:188-194`) into the management surface for the `tenants` and `api_keys` tables (`postgres-schema-v2.sql:21-69`). It covers **proxy-facing keys only** — what callers authenticate to *us* with. Upstream BYOK credentials never appear here, never render, and are never persisted per adapter-api-surface.md C10/AC-A13.

Two hard invariants carried over from the dashboard spec:

- **C10 redaction:** no key material of any kind is rendered except (a) `key_last4`, and (b) the *plaintext of a newly created/rotated key, shown exactly once* (§4.2). `key_hash` never leaves the server — it must not appear in any list/detail payload.
- **1:1 data contract:** every displayed *number* (requests, spend, savings) is copied verbatim from a single `/api/kpis` response scoped by `tenant_id`/`api_key_id` — both selectors already exist on the real route (`main.py:1095-1117`, AC-A7 scoping). **Zero client-side aggregation.** Management *facts* (status, dates, caps) come verbatim from the new `/api/keys` read endpoints (§3) — same rule, different source.

## 2. Layout (12-col grid, shared component set, dark theme)

```
Row 1: [Tenant card, span 6] [Per-tenant KPI cards ×2, span 3 each]
Row 2: [Proxy keys table, span 12]
Row 3: [Per-key usage drawer (on demand), span 12]
```

### 2.1 Tenant card (span 6)
Reads verbatim from `GET /api/tenants`: `name`, `plan`, `spend_cap_usd` (`NULL` → render "Uncapped"), `created_at`. In self-host mode there is exactly one row (the seeded `default` tenant); the card renders it without a selector. A tenant *picker* is deliberately deferred until hosted multi-tenant exists — don't build a dropdown for one row.

### 2.2 Per-tenant KPI cards (span 3 ×2)
Read verbatim from `GET /api/kpis?tenant_id=<id>`: `overview.requests` and `overview.cost_saved` in the existing `KPI card` component. These are the tenant's own proxy traffic — same numbers the Overview tab shows unscoped, now scoped by the selector the API already honors. No new aggregation endpoint is created for this.

### 2.3 Proxy keys table (span 12)
One row per key from `GET /api/keys?tenant_id=<id>`. Columns:

| Column | Reads (verbatim) | Render |
|---|---|---|
| Key | `key_last4` | `•••• 1234` — muted dots + tabular-nums last-4. Never a full key. |
| Scopes | `scopes` | chips (reuse `.chip` styling, non-interactive here); empty array → "—" |
| Spend cap | `spend_cap_usd` | `$x` or "Inherits tenant" when NULL |
| Status | `status` | `.badge green` `active` / `.badge red` `revoked` / neutral `rotated` |
| Created | `created_at` | ISO date, muted |
| Revoked | `revoked_at` | ISO date or "—" |
| Actions | — | `Rotate` + `Revoke` buttons (active keys only); disabled rows show nothing |

Row action: clicking a row opens the per-key usage drawer (§2.4).

### 2.4 Per-key usage drawer (span 12, on demand)
Opened by row click; one fetch per open: `GET /api/kpis?api_key_id=<id>` (plus the tab's current `bucket`/`from`/`to` state, reusing `state`). Renders three `KPI card`s — `overview.requests`, `overview.input_tokens_saved`, `overview.cost_saved` — verbatim. Closing and reopening re-fetches; there is no client-side caching or summing across keys.

## 3. API contract (new endpoints — @application-developer scope)

```
GET    /api/tenants                    → [{id, name, plan, spend_cap_usd, created_at}]
GET    /api/keys?tenant_id=<uuid>      → [{id, tenant_id, key_last4, scopes, spend_cap_usd,
                                          status, created_at, revoked_at}]        # NO key_hash, ever
POST   /api/keys                       body {tenant_id, scopes?, spend_cap_usd?}
                                       → 201 {id, key_last4, key}                 # `key` plaintext: exactly once, create only
POST   /api/keys/{id}/rotate           → 200 {id, key_last4, key, previous_status: "rotated"}
POST   /api/keys/{id}/revoke           → 200 {id, status: "revoked", revoked_at}
```

Contract rules:
- `POST` responses are the **only** responses in the entire tab that ever contain key material. The plaintext field is named `key`, is returned **once**, is never logged by the proxy's request logging, and is never stored server-side (only `key_hash` + `key_last4`, per schema).
- `rotate` = create new key + stamp old `status: 'rotated'` in one transaction (schema's `rotated` state exists for exactly this). Old key stops authenticating immediately on success response.
- `revoke` is idempotent: revoking a revoked key returns 200 with unchanged fields, not 409 (double-click safety).
- Errors follow the proxy's existing error shape; validation failures (unknown tenant, bad scopes) → 400 with a human-readable `detail` the UI can show verbatim.
- **Auth gate — RESOLVED by @product-manager (2026-09-19):** all four write endpoints (`POST /api/keys`, rotate, revoke) require `Authorization: Bearer <ADMIN_TOKEN>`. Token source: `ADMIN_TOKEN` env var if set; otherwise generated at boot and printed once to the proxy's startup log (boot-printed, never persisted to disk, never re-printed). Missing/invalid token → 401 with the proxy's existing error shape. Read endpoints (`GET /api/tenants`, `/api/keys`, `/api/kpis`, `/dashboard`) stay unauthenticated for Phase C under the self-host trust model. Rationale: the proxy is network-facing by definition, so unauthenticated create/rotate/revoke lets anyone who can reach the dashboard mint keys that spend the owner's budget or revoke their own keys; full admin auth (accounts/sessions) is deferred scope. QA gates the write endpoints behind this.

## 4. Interaction flows & states

### 4.1 Loading / error / empty (shared states, per dashboard spec §7)
- **Loading:** existing `skel-row` pattern in `#content` before fetch resolves.
- **Error:** existing `.error-box` + `Retry` button; retry re-issues the failed fetch. Never a bare blank panel.
- **Empty (no keys yet):** `.empty` hero inside a span-12 card — headline *"No proxy keys yet"*, body *"Create a key so your applications can authenticate to the proxy."*, CTA button **Create key** opening the create flow (§4.2).

### 4.2 Create / rotate — one-time reveal (the critical flow)
1. **Create key** button (table header, right-aligned, and in the empty state) opens an inline form card (no modal library exists; keep it inline): fields = scopes (free-text comma chips), spend cap (optional, `$` prefixed, validated numeric). Tenant is fixed to the single tenant — no selector.
2. Submit → `POST /api/keys`. While pending: submit button disabled + "Creating…".
3. Success swaps the form for a **reveal panel**: `key_last4`, the full plaintext in a monospace `<code>` block, a **Copy** button (uses `navigator.clipboard`, shows "Copied" for 2s), and a warning line in the red badge style: *"This key is shown once. Copy it now — it cannot be retrieved again."* A **Done** button dismisses back to the table.
4. Navigating away from the tab (hash change) or refreshing dismisses the plaintext — never persist it anywhere (no sessionStorage, no innerHTML resurrection, no console.log in shipped code).
5. **Rotate** uses the same reveal panel; the confirm step first (see §4.3) explains: *"Rotating issues a new key and immediately stops the old one. Update callers with the new key."* Old row flips to `rotated` badge on table refresh.

### 4.3 Revoke — two-step inline confirm
No `window.confirm()`. The **Revoke** button click morphs it into *"Confirm revoke?"* (red fill) with an adjacent **Cancel**; confirm reverts to plain if untouched for 5s. Confirm → `POST /api/keys/{id}/revoke` → row badge flips to `revoked`, actions disappear. Copy on the confirm step: *"Revoked keys stop authenticating immediately. This cannot be undone."* (Schema has no un-revoke; the UI must not imply one exists.)

### 4.4 Failure of a write action
Button returns to normal state + inline error line under the action (`detail` from the 4xx verbatim), e.g. *"Unknown tenant."* The table does **not** re-render on a failed write — the user's scroll position and confirm state survive.

## 4.5 Admin token entry (write-path prerequisite)

The browser has no way to send `Authorization` on its own — dashboard.js must attach it. The token is boot-printed to the proxy's startup log and never persisted server-side, so the UI flow is:

1. First write action (`POST /api/keys`, rotate, or revoke) that returns **401** → an inline form card appears at the top of the tab (same styling as §4.2's inline form, no modal): label *"Admin token"*, password-masked input, hint text *"Printed once to the proxy's startup log at boot."*, **Save** button.
2. On submit, the token is held in a **JS module variable only** — never sessionStorage/localStorage/cookie, never logged, never in any URL (no query-string tokens). Page reload requires re-entry by design.
3. All subsequent write fetches attach `Authorization: Bearer <token>`; read fetches never do. A **401 response at any later point** (proxy restarted → new boot-printed token) clears the stored token and re-opens the entry form with the message *"Token rejected — the proxy may have restarted. Enter the current startup-log token."*
4. A failed token entry (401 on the retried write) surfaces per §4.4 — the original confirm state is preserved so the user re-enters once, not twice.
5. If `ADMIN_TOKEN` env is set by the operator, this flow is identical — the UI cannot and must not distinguish the two sources.

QA note: add to §6 gates — a render-harness case asserting (a) write fetches carry the header only after token entry, (b) the token never appears in DOM text outside the masked input, (c) 401 re-prompts with the cleared state.

## 5. States not built (explicit non-goals for this tab)
- Tenant CRUD (create/rename tenants) — schema exists, but self-host has one tenant; defer until hosted plans.
- Per-key usage *charts* — KPI cards only; charts stay on Overview/Traffic where `series` legitimately exists.
- Key editing (scopes/cap mutation after creation) — schema has no UPDATE story for it; if needed later it's a rotate, not an edit.
- Semantic-cache fields — PC4/pgvector territory, stays out.

## 6. QA gates (suggested, for @qa-lead to attach AC IDs to when boarding)
1. **Redaction (C10):** rendered table + every GET response contain `key_last4` but never `key_hash` or full key material; harness asserts the placeholder string `•••• ` and absence of any 40+-char token.
2. **One-time reveal:** plaintext appears in exactly one POST response; render harness asserts it's gone from the DOM after **Done** and after hashchange.
3. **1:1 usage contract:** per-tenant/per-key cards equal the `/api/kpis` fields verbatim (extend the existing `test_dashboard_render.py` / `dashboard_render_harness.js` R-invariant style).
4. **Write-path UX:** revoke idempotency (200 twice), rotate → old key `rotated` status, failed write leaves table unrendered.
5. **Placeholder removal:** `renderKeys()` no longer renders the "arrives with Phase C" copy once landed.
6. **Admin token handling (§4.5):** write fetches carry the Bearer header only after token entry; the token never renders outside the masked input and is absent from storage APIs, URLs, and logs; a mid-session 401 clears the token and re-prompts without losing the pending action's confirm state.

## 7. Open items
- ~~Admin auth gate for the write endpoints (§3)~~ — **RESOLVED:** boot-printed/`ADMIN_TOKEN` bearer token on all write endpoints; reads unauthenticated (see §3). Spec otherwise approved by @product-manager for boarding when Phase C opens.
- Whether the tab should also surface the provider registry (`providers` table) as a read-only section later — leaning no (Providers tab already covers its KPIs); noted for PM review.
