-- V2.0 attribution audit (t_ef7f0f71): persist the provider-returned
-- native-cache usage the adapters already parse but the ledger never stored.
--
-- Gap being closed (AC-V2-4/AC-V2-6): NormalizedUsage carries
-- cache_read_tokens / cache_write_tokens on every provider response
-- (proxy/providers/model.py:47), but stats.log_request / _log_postgres never
-- receive or write them. On the live volume every exact_hit row therefore has
-- cache_savings = 0 and no row anywhere records provider-native cache tokens:
-- an exact_hit is the proxy's own prefix detection, NOT provider-returned
-- evidence, so the existing schema cannot attribute provider-native cache
-- savings at all. est_cost_before/after are priced at full input rate
-- (estimate_cost has no cached-token rate), so provider discounts are also
-- invisible in the cost columns. No double counting is introduced: these
-- columns are measured provider facts, disjoint from cache_savings
-- (proxy/semantic/exact detection), l1_savings (stripped tokens), and
-- tool_compression_saved (bounded component estimate).
--
-- Design: NULLABLE INTEGERs. NULL = "the provider returned no cache usage
-- fields" (AC-V2-6: no observable evidence -> zero cache savings); 0 = "the
-- provider returned usage showing zero cached tokens". Pricing/multipliers
-- stay OUT of the ledger; any dollar attribution derives at read time from
-- measured tokens × provider rate, the same SUM-over-ledger rule as
-- AC-A6/A12. The canonical fresh-volume schema already declares these final
-- columns; IF NOT EXISTS makes both initialization sequences valid without
-- masking unrelated migration errors.
--
-- Rollback: additive-only. Dropping both columns restores the prior schema
-- with no rewrite; no row data is lost (writers degrade to the current
-- not-persisted behavior, attribution merely stops accruing).

BEGIN;

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS provider_cache_read_tokens INTEGER NULL,
    ADD COLUMN IF NOT EXISTS provider_cache_write_tokens INTEGER NULL;

COMMENT ON COLUMN requests.provider_cache_read_tokens IS
    'AC-V2-6: provider-returned cache-read tokens (Anthropic cache_read_input_tokens / OpenAI-compat prompt_tokens_details.cached_tokens) actually observed on the response. NULL = provider returned no cache usage evidence; never inferred, never merged into cache_savings.';
COMMENT ON COLUMN requests.provider_cache_write_tokens IS
    'AC-V2-6: provider-returned cache-write tokens (Anthropic cache_creation_input_tokens) observed on the response. NULL = provider returned no cache usage evidence; attribution only, never summed with cache_savings or l1_savings.';

COMMIT;
