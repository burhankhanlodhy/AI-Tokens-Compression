"""Configuration for the token-saver proxy.

All settings are read from environment variables (or a local .env file).
Never put real API keys in this file — the proxy is BYOK and passes the
client's Authorization header straight through; it never stores keys.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROXY_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROXY_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Proxy server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Upstream provider (BYOK: client's key is forwarded, never stored) ---
    # OpenRouter (OpenAI-compatible). Any other OpenAI-compatible provider works
    # too — just change this URL.
    upstream_base_url: str = "https://openrouter.ai/api/v1"
    upstream_timeout_seconds: float = 120.0

    # --- Feature flags ---
    compression_enabled: bool = True
    # P1-1 (PM decision): conciseness OFF by default — benchmarks showed no
    # reliable savings. Declared ONCE, here; the duplicate declaration near
    # the Output conciseness section below was a dead trap (removed B2-c).
    output_conciseness_enabled: bool = False

    # Reasoning control (PM v4 ruling): the proxy injects a model-family-
    # specific control by default. The old `{"enabled": false}` suppress
    # control was rejected outright by reasoning-mandatory endpoints (OpenRouter
    # 400s on gemini-3.5-flash-lite), and "zero thinking" is unsatisfiable on
    # models whose floor is MINIMAL. So the Gemini family gets the MINIMAL
    # FLOORING control (bounds hidden reasoning to the model's cheapest level;
    # efficacy is verified by the SD gate's differential probe, never assumed),
    # while families that accept suppression keep it. A client that explicitly
    # sets its own `reasoning` or `thinking_level` field is always respected
    # instead. Keys are BODY-LEVEL keys: the dict is merged into the request
    # body verbatim.
    disable_reasoning_by_default: bool = True
    reasoning_control_default: dict[str, dict] = {
        "reasoning": {"enabled": False},
    }
    reasoning_control_by_prefix: dict[str, dict] = {
        "google/": {"thinking_level": "MINIMAL"},
        "gemini": {"thinking_level": "MINIMAL"},
    }

    # --- PA-4: exact-prefix cache detection ---
    cache_enabled: bool = True

    # --- B2: L1 lossless structural cleanup (Phase B) ---
    # Pure deterministic transform (proxy/l1_clean.py, frozen taxonomy
    # l1-taxonomy.md). Runs BEFORE the PA-4 cache key: cache key = clean
    # bytes (AC-P1f). ON by default since B-26: the §6 contract tests pin
    # byte-identity on the non-target classes enumerated in the pinned
    # corpus (13 controls incl. code fences, YAML, tool_calls, multimodal,
    # markdown tables, mixed prose+JSON, JSON-in-fence — B-28), so
    # default-on is safe for documented inputs. Classes beyond the corpus
    # are not individually pinned. Disable per-deployment with
    # L1_ENABLED=false. Caveat: a message
    # whose ENTIRE content is pretty-printed JSON (no surrounding prose)
    # gets whitespace-compacted — see the README flag note.
    l1_enabled: bool = True

    # --- PA-1: multi-provider routing ---
    # "off" (default): legacy single-upstream behavior — everything goes to
    # UPSTREAM_BASE_URL in OpenAI shape. "on": model string routes through
    # the provider registry; each provider gets its own base_url, auth
    # header placement, and wire shape (Anthropic /v1/messages etc).
    provider_routing: bool = False

    # Per-provider base URLs used when provider_routing is on (mirror of the
    # providers table seed set; the table is the eventual source of truth).
    provider_base_urls: dict[str, str] = {
        "anthropic": "https://api.anthropic.com",
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "xai": "https://api.x.ai/v1",
        "google": "https://generativelanguage.googleapis.com/v1beta/openai",
        "vllm": "http://localhost:8001/v1",
        "ollama": "http://localhost:11434/v1",
    }

    # --- Compression (LLMLingua-2) ---
    # Smaller/faster BERT variant first for CPU; swap for
    # "microsoft/llmlingua-2-xlm-roberta-large-meetingbank" if quality demands.
    llmlingua_model: str = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"
    compression_rate: float = 0.6          # target fraction of tokens kept
    force_tokens: str = "!?\n"             # tokens preserved by the compressor
    min_chars_to_compress: int = 400       # don't bother compressing short content
    compress_system_messages: bool = False # system prompts are usually precise specs

    # Output conciseness instruction + gate — see the single
    # output_conciseness_enabled declaration in Feature flags above
    # (P1-1 decision, PM, after runs 20260915T040955Z / 20260915T043617Z:
    # benchmarked at -1.53% and -2.45% mean output change with parity —
    # NO reliable savings for z-ai/glm-5.3-flash. The
    # X-Token-Saver-Conciseness header can still enable it per-request.)
    conciseness_instruction: str = (
        "Answer concisely and directly. Do not restate the question, do not "
        "summarize what you are about to say, and do not add filler or "
        "unnecessary caveats. Skip preamble and postamble."
    )
    # P1-1 category/length-aware gate: the benchmark (run 20260915T040955Z)
    # showed injection is net-negative on short prompts. Only inject when the
    # last user message is at least this many characters.
    conciseness_min_user_chars: int = 400

    # --- P6-2 grounded-answer dose tiers (config-declared, ordered) ---
    # "bounded" tier: gentler instruction with an explicit fidelity guard —
    # the grounded-traffic-safe alternative to the full P1-1 instruction.
    # "full" IS conciseness_instruction above (unchanged); "none" injects
    # nothing. Selection lives in proxy.grounded.select_dose_tier.
    dose_tier_bounded_instruction: str = (
        "Answer concisely BUT keep every source-attributed fact, number, "
        "deadline, and condition. Shorten the delivery, never the content: "
        "do not drop, round away, summarize away, or paraphrase any fact "
        "taken from the provided sources."
    )
    # AC-P6c gate (P6-3): grounded traffic stays capped at tier "none" until
    # the calibration artifact is committed and green. Flip only on the
    # PM's sign-off — this is the pre-calibration OFF switch for grounded
    # fidelity-critical traffic, never a per-request toggle.
    grounded_calibration_green: bool = False

    def dose_tier_instructions(self) -> dict[str, str | None]:
        """Config-declared tier -> instruction text ("none" -> nothing)."""
        return {
            "none": None,
            "bounded": self.dose_tier_bounded_instruction,
            "full": self.conciseness_instruction,
        }

    # --- Classifier thresholds ---
    code_symbol_density_threshold: float = 0.05  # ratio of code-ish chars to trigger passthrough
    min_chars_to_classify: int = 120             # tiny prompts: just compress

    # --- Storage ---
    database_path: str = str(PROXY_ROOT / "data" / "stats.db")

    # --- Cost estimation (USD per 1M tokens, input/output) ---
    # Prices live in pricing.json (loaded at startup — see load_pricing).
    # Changing a price is a one-file edit, zero code change. This hardcoded
    # dict is the FALLBACK ONLY, used when the file is missing/unreadable or
    # a routed model has no file entry (with a warning). Values below mirror
    # the pricing.json seed (OpenRouter-checked 2026-09-17) so fallback
    # behavior degrades gracefully instead of resurrecting stale rates.
    pricing_file: str = str(PROXY_ROOT / "pricing.json")
    default_input_price_per_m: float = 0.50
    default_output_price_per_m: float = 1.50
    model_prices_per_m: dict[str, tuple[float, float]] = {
        "openai/gpt-4o": (2.50, 10.00),
        "openai/gpt-4o-mini": (0.15, 0.60),
        "openai/gpt-4.1": (2.00, 8.00),
        "openai/gpt-4.1-mini": (0.40, 1.60),
        "openai/gpt-4.1-nano": (0.10, 0.40),
        "openai/gpt-5.6-luna": (0.20, 1.20),
        "anthropic/claude-sonnet-4": (3.00, 15.00),
        "anthropic/claude-sonnet-5": (2.00, 10.00),
        "anthropic/claude-haiku-4.5": (1.00, 5.00),
        "google/gemini-2.5-flash": (0.30, 2.50),
        "google/gemini-3.5-flash": (1.50, 9.00),
        "google/gemini-3.5-flash-lite": (0.30, 2.50),
        "google/gemini-3.5-flash-lite:batch": (0.15, 1.25),
        "google/gemini-3.8-flash": (0.75, 3.75),
        "meta-llama/llama-3.3-70b-instruct": (0.10, 0.32),
        "deepseek/deepseek-chat": (0.2574, 1.0287),
        "deepseek/deepseek-v4-flash": (0.07, 0.14),
        "z-ai/glm-5.3-flash": (0.09, 0.30),
    }


_PRICING_LOGGER = logging.getLogger("token-saver.pricing")
_pricing_cache: dict[str, tuple[float, float]] | None = None
_pricing_warned: set[str] = set()


def load_pricing(force_reload: bool = False) -> dict[str, tuple[float, float]]:
    """Load model prices from pricing.json (USD per 1M, [input, output]).

    Called once at proxy startup and cached. Keys starting with '_' are
    metadata and skipped. If the file is missing, unreadable, or malformed,
    the hardcoded fallback table in Settings.model_prices_per_m takes over
    (with a warning) so cost accounting never silently stops.
    """
    global _pricing_cache
    if _pricing_cache is not None and not force_reload:
        return _pricing_cache
    s = get_settings()
    path = Path(s.pricing_file)
    table: dict[str, tuple[float, float]] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            _PRICING_LOGGER.warning(
                "pricing file %s unreadable (%s); using hardcoded fallback table",
                path, exc,
            )
            raw = None
        if isinstance(raw, dict):
            for model, prices in raw.items():
                if model.startswith("_"):
                    continue  # metadata (_meta etc)
                if (
                    isinstance(prices, (list, tuple))
                    and len(prices) == 2
                    and all(isinstance(p, (int, float)) and p >= 0 for p in prices)
                ):
                    table[model] = (float(prices[0]), float(prices[1]))
                else:
                    _PRICING_LOGGER.warning(
                        "pricing file %s: skipping malformed entry %r (want "
                        "[input_per_m, output_per_m] as non-negative numbers)",
                        path, model,
                    )
        elif raw is not None:
            _PRICING_LOGGER.warning(
                "pricing file %s is not a JSON object; using hardcoded fallback table",
                path,
            )
    else:
        _PRICING_LOGGER.warning(
            "pricing file %s not found; using hardcoded fallback table", path
        )
    _pricing_cache = table
    return table


@lru_cache
def get_settings() -> Settings:
    # Allow env override of nested dict-ish values is not needed for MVP.
    return Settings()


def estimate_cost(model: str, input_tokens: int, output_tokens: float) -> float:
    """Estimated USD cost for a request.

    Prices come from pricing.json (startup-loaded). A routed model with no
    file entry falls back to the hardcoded table / defaults AND logs a
    one-time warning per model so stale-rate drift is visible, not silent.
    """
    table = load_pricing()
    prices = table.get(model)
    if prices is None:
        s = get_settings()
        prices = s.model_prices_per_m.get(
            model, (s.default_input_price_per_m, s.default_output_price_per_m)
        )
        if model not in _pricing_warned:
            _pricing_warned.add(model)
            in_file = bool(table)
            _PRICING_LOGGER.warning(
                "no pricing%s entry for model %r — falling back to hardcoded "
                "rates %s; edit pricing.json to price it correctly",
                ".json" if in_file else "-file", model, prices,
            )
    in_price, out_price = prices
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def reasoning_control_for(model: str) -> dict:
    """The reasoning control injected for `model` (PM v4 ruling).

    Longest matching prefix in reasoning_control_by_prefix wins; unmatched
    models get reasoning_control_default. Returns BODY-LEVEL key/value
    pairs merged verbatim into the request body, e.g.:
      google/gemini-3.5-flash-lite -> {"thinking_level": "MINIMAL"}
      z-ai/glm-5.3-flash           -> {"reasoning": {"enabled": False}}
    """
    s = get_settings()
    for prefix in sorted(s.reasoning_control_by_prefix, key=len, reverse=True):
        if model.startswith(prefix):
            return dict(s.reasoning_control_by_prefix[prefix])
    return dict(s.reasoning_control_default)
