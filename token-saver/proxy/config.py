"""Configuration for the token-saver proxy.

All settings are read from environment variables (or a local .env file).
Never put real API keys in this file — the proxy is BYOK and passes the
client's Authorization header straight through; it never stores keys.
"""
from __future__ import annotations

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
    output_conciseness_enabled: bool = True

    # Reasoning models (e.g. glm-5.3-flash) can spend an unpredictable number
    # of hidden "thinking" tokens per request — often far more than anything
    # compression saves on the input side, and with no correlation to prompt
    # content. Suppress it by default via OpenRouter's `reasoning` param so
    # cost stays predictable; a client that explicitly sets its own
    # `reasoning` field in the request body is always respected instead.
    disable_reasoning_by_default: bool = True

    # --- PA-4: exact-prefix cache detection ---
    cache_enabled: bool = True

    # --- Compression (LLMLingua-2) ---
    # Smaller/faster BERT variant first for CPU; swap for
    # "microsoft/llmlingua-2-xlm-roberta-large-meetingbank" if quality demands.
    llmlingua_model: str = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"
    compression_rate: float = 0.6          # target fraction of tokens kept
    force_tokens: str = "!?\n"             # tokens preserved by the compressor
    min_chars_to_compress: int = 400       # don't bother compressing short content
    compress_system_messages: bool = False # system prompts are usually precise specs

    # --- Output conciseness ---
    conciseness_instruction: str = (
        "Answer concisely and directly. Do not restate the question, do not "
        "summarize what you are about to say, and do not add filler or "
        "unnecessary caveats. Skip preamble and postamble."
    )

    # --- Classifier thresholds ---
    code_symbol_density_threshold: float = 0.05  # ratio of code-ish chars to trigger passthrough
    min_chars_to_classify: int = 120             # tiny prompts: just compress

    # --- Storage ---
    database_path: str = str(PROXY_ROOT / "data" / "stats.db")

    # --- Cost estimation (USD per 1M tokens, input/output) ---
    # Small table; extend as needed. Unknown models fall back to these defaults.
    # Prices below are for popular OpenRouter-hosted models; check
    # https://openrouter.ai/models for current pricing.
    default_input_price_per_m: float = 0.50
    default_output_price_per_m: float = 1.50
    model_prices_per_m: dict[str, tuple[float, float]] = {
        "openai/gpt-4o": (2.50, 10.00),
        "openai/gpt-4o-mini": (0.15, 0.60),
        "openai/gpt-4.1": (2.00, 8.00),
        "openai/gpt-4.1-mini": (0.40, 1.60),
        "openai/gpt-4.1-nano": (0.10, 0.40),
        "anthropic/claude-sonnet-4": (3.00, 15.00),
        "anthropic/claude-sonnet-5": (2.00, 10.00),
        "anthropic/claude-haiku-4": (0.80, 4.00),
        "google/gemini-2.5-flash": (0.30, 2.50),
        "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
        "deepseek/deepseek-chat": (0.14, 0.28),
        "z-ai/glm-5.3-flash": (0.10, 0.40),
    }


@lru_cache
def get_settings() -> Settings:
    # Allow env override of nested dict-ish values is not needed for MVP.
    return Settings()


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost for a request."""
    s = get_settings()
    in_price, out_price = s.model_prices_per_m.get(
        model, (s.default_input_price_per_m, s.default_output_price_per_m)
    )
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000
