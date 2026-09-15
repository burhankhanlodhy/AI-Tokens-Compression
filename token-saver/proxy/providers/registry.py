"""Provider registry + routing (adapter-api-surface.md Section 4).

Phase A: registry rows come from config defaults (providers table wiring
lands with PA-0/PA-2 integration). Routing rule: model-string prefix wins,
then explicit override, then default provider.
"""
from __future__ import annotations

from dataclasses import dataclass

from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .openai_compat import OpenAICompatAdapter


@dataclass(frozen=True)
class ProviderRow:
    name: str
    base_url: str
    adapter_class: str  # 'OpenAICompatAdapter' | 'AnthropicAdapter'
    auth_style: str
    enabled: bool = True


# Default registry — mirrors postgres-schema-v2.sql seed set. In Phase A the
# application builds these rows at startup; the providers table becomes the
# source of truth once the app reads its registry from Postgres.
DEFAULT_REGISTRY: list[ProviderRow] = [
    ProviderRow("anthropic", "https://api.anthropic.com", "AnthropicAdapter", "x-api-key"),
    ProviderRow("openai", "https://api.openai.com/v1", "OpenAICompatAdapter", "bearer"),
    ProviderRow("openrouter", "https://openrouter.ai/api/v1", "OpenAICompatAdapter", "bearer"),
    ProviderRow("xai", "https://api.x.ai/v1", "OpenAICompatAdapter", "bearer"),
    ProviderRow("google", "https://generativelanguage.googleapis.com/v1beta/openai", "OpenAICompatAdapter", "x-goog-api-key"),
    ProviderRow("vllm", "http://localhost:8001/v1", "OpenAICompatAdapter", "none"),
    ProviderRow("ollama", "http://localhost:11434/v1", "OpenAICompatAdapter", "none"),
]

# Model-prefix -> provider name. Checked before the default provider.
PREFIX_ROUTES = {
    "anthropic/": "anthropic",
    "claude": "anthropic",
    "openai/": "openai",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "openrouter/": "openrouter",
    "xai/": "xai",
    "grok": "xai",
    "google/": "google",
    "gemini": "google",
    "ollama/": "ollama",
    "vllm/": "vllm",
}


class ProviderRegistry:
    def __init__(self, rows: list[ProviderRow] | None = None):
        self.rows = list(rows if rows is not None else DEFAULT_REGISTRY)
        self._adapters: dict[str, ProviderAdapter] = {}
        for row in self.rows:
            if not row.enabled:
                continue
            if row.adapter_class == "AnthropicAdapter":
                self._adapters[row.name] = AnthropicAdapter()
            else:
                self._adapters[row.name] = OpenAICompatAdapter(
                    name=row.name, auth_style=row.auth_style
                )
        self.default_provider = "openrouter"

    def route(self, model: str, override: str | None = None) -> ProviderAdapter:
        """Model string -> adapter. Prefix wins, then override, then default."""
        if override and override in self._adapters:
            return self._adapters[override]
        lowered = model.lower()
        for prefix, provider in PREFIX_ROUTES.items():
            if lowered.startswith(prefix) and provider in self._adapters:
                return self._adapters[provider]
        return self._adapters[self.default_provider]

    def get(self, name: str) -> ProviderAdapter | None:
        return self._adapters.get(name)

    def adapter_for_row(self, row: ProviderRow) -> ProviderAdapter:
        """Build/return the adapter for a registry row (used by tests/factories)."""
        if row.name not in self._adapters:
            if row.adapter_class == "AnthropicAdapter":
                self._adapters[row.name] = AnthropicAdapter()
            else:
                self._adapters[row.name] = OpenAICompatAdapter(
                    name=row.name, auth_style=row.auth_style
                )
        return self._adapters[row.name]
