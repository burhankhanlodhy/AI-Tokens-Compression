-- V2.2 runtime configuration persistence (t_d86fe22b).
--
-- PM contract docs/v2.2-ui-ux-overhaul-spec.md §3.2: dashboard/runtime
-- overrides must survive a proxy restart and always lose to environment
-- variables, which lose to built-in defaults. Precedence (highest wins):
--   1. per-request control headers (unchanged, benchmark-only)
--   2. runtime overrides            -> this table (PG) / settings.json (SQLite)
--   3. environment variables        -> config.py (unchanged)
--   4. built-in defaults            -> config.py
--
-- Scope: ONE allowlisted per-deployment feature switch per row (see
-- proxy/settings.py RUNTIME_ALLOWED, PM §3.1 — enforced server-side in
-- code, B2). Numbers, deployment-only, secret, and infrastructure settings
-- are structurally excluded: the writer refuses any name outside the
-- allowlist and any non-boolean payload, so no migration change is needed
-- to widen or restrict the surface (the code is the gate).
--
-- Auditability (PM §3.2): every override records updated_at and updated_by
-- ("admin" or the proxy-key id — never a token fragment). Deleting a row
-- reverts the effective value to env/default immediately.
--
-- Tenancy: overrides are per-deployment operator state, not per-tenant
-- data — V2.2 ships a single-operator settings surface (PM §7.3, no tenant
-- picker), so there is deliberately NO tenant_id column. Isolation of
-- ledger/KPI/provider behavior is untouched (no requests/ledger columns
-- change in this migration).
--
-- Secrets: no credential-bearing setting is writable at runtime (PM §3.1);
-- the value column holds only booleans and nothing that needs masking.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so
-- one file serves the fresh Compose initdb mount (50-v22-runtime-settings)
-- and re-runs on existing volumes (same convention as migrations
-- 20260922_*, 20260924_v2, and 20260924_v21). Apply with ON_ERROR_STOP=1.
--
-- Rollback: additive-only — DROP TABLE restores the prior schema; the
-- proxy degrades to env/default precedence (log a warning, never crash).

BEGIN;

CREATE TABLE IF NOT EXISTS app_settings (
    name         TEXT PRIMARY KEY,
    value        BOOLEAN NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by   TEXT NOT NULL,
    CONSTRAINT chk_app_settings_updated_by CHECK (length(updated_by) > 0)
);

COMMENT ON TABLE app_settings IS
    'V2.2 runtime configuration overrides (PM §3.2). One allowlisted '
    'deployment switch per row; the server-side allowlist in '
    'proxy/settings.py RUNTIME_ALLOWED is the only writer gate. Precedence: '
    'runtime override -> environment variable -> built-in default. No '
    'secret, deployment-only, or numeric setting is persisted here.';
COMMENT ON COLUMN app_settings.updated_by IS
    'Identity of the last override write: "admin" or the proxy-key id. '
    'Never a token, hash, or key fragment (PM §3.2).';
COMMENT ON COLUMN app_settings.value IS
    'Boolean override only. NULL is unreachable (NOT NULL): clearing an '
    'override DELETES the row, which reverts to env/default immediately.';

COMMIT;
