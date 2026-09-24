"""Provider-gated deferred tool selection with deterministic eager fallback.

Only providers whose wire/client contract explicitly implements the deferred
catalog handshake may be enabled here. None of the currently registered
adapters does, so this module cannot silently change a provider request.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

# Capability is intentionally empty until an adapter implements and contract-tests
# the catalog/search handshake. Provider names are registry names.
_DEFERRED_TOOL_PROVIDERS: frozenset[str] = frozenset()
_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def provider_deferred_tool_support(provider: str) -> bool:
    """Return true only for an adapter with a tested deferred-tool wire contract."""
    return provider in _DEFERRED_TOOL_PROVIDERS


@dataclass(frozen=True)
class ToolSearchResult:
    status: str
    catalog_bytes: int
    search_overhead_bytes: int
    schema_bytes: int
    miss_count: int = 0
    error_count: int = 0
    retry_count: int = 0
    match_count: int = 0
    latency_ms: float = 0.0

    def telemetry(self) -> dict[str, int | float | str]:
        """Non-overlapping observations; byte counts are not token/savings claims."""
        return {
            "status": self.status,
            "catalog_bytes": self.catalog_bytes,
            "search_overhead_bytes": self.search_overhead_bytes,
            "schema_bytes": self.schema_bytes,
            "miss_count": self.miss_count,
            "error_count": self.error_count,
            "retry_count": self.retry_count,
            "match_count": self.match_count,
            "latency_ms": self.latency_ms,
        }


class DeferredToolSelector:
    """Build a stable compact catalog and select exact original schemas by query."""

    def __init__(self, tools: list[dict[str, Any]]):
        if not isinstance(tools, list) or any(not isinstance(item, dict) for item in tools):
            raise ValueError("tools must be a list of schema objects")
        self._tools = tools
        catalog_tools = []
        for tool in tools:
            fn = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
            if not isinstance(fn, Mapping) or not isinstance(fn.get("name"), str):
                raise ValueError("each tool must have a string name")
            entry: dict[str, str] = {"name": fn["name"]}
            if isinstance(fn.get("description"), str):
                entry["description"] = fn["description"]
            catalog_tools.append(entry)
        self._catalog = json.dumps(
            {"tools": catalog_tools}, ensure_ascii=False, separators=(",", ":")
        )

    def catalog(self) -> str:
        return self._catalog

    def search(self, query: str, *, retries: int = 0) -> tuple[list[dict[str, Any]], ToolSearchResult]:
        started = time.perf_counter()
        catalog_bytes = len(self._catalog.encode("utf-8"))
        schema_bytes = len(json.dumps(self._tools, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if retries < 0:
            retries = 0
        try:
            if not isinstance(query, str) or not query.strip():
                raise ValueError("search query must not be empty")
            query_terms = set(_TOKEN.findall(query.lower()))
            scored: list[tuple[int, int, dict[str, Any]]] = []
            for index, tool in enumerate(self._tools):
                fn = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
                haystack = f"{fn.get('name', '')} {fn.get('description', '')}".lower()
                tool_terms = set(_TOKEN.findall(haystack))
                score = len(query_terms & tool_terms)
                if score:
                    scored.append((score, index, tool))
            ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
            if not ranked:
                result = self._result("miss_fallback", catalog_bytes, schema_bytes,
                                      retries, started, miss_count=1,
                                      search_overhead_bytes=len(query.encode("utf-8")))
                return self._tools, result
            selected = [item[2] for item in sorted(ranked, key=lambda item: item[1])]
            result = self._result("hit", catalog_bytes, schema_bytes, retries, started,
                                  search_overhead_bytes=len(query.encode("utf-8")),
                                  match_count=len(ranked))
            return selected, result
        except Exception:
            result = self._result("error_fallback", catalog_bytes, schema_bytes,
                                  retries, started, error_count=1,
                                  search_overhead_bytes=len(query.encode("utf-8")) if isinstance(query, str) else 0)
            return self._tools, result

    def select(
        self, query: str, *, enabled: bool, provider: str, retries: int = 0
    ) -> tuple[list[dict[str, Any]], ToolSearchResult]:
        """Select only when both server policy and adapter contract allow it."""
        started = time.perf_counter()
        catalog_bytes = len(self._catalog.encode("utf-8"))
        schema_bytes = len(json.dumps(self._tools, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if not enabled:
            return self._tools, self._result("disabled_fallback", catalog_bytes, schema_bytes, retries, started)
        if not provider_deferred_tool_support(provider):
            return self._tools, self._result("unsupported_fallback", catalog_bytes, schema_bytes, retries, started)
        return self.search(query, retries=retries)

    @staticmethod
    def _result(status: str, catalog_bytes: int, schema_bytes: int, retries: int,
                started: float, *, miss_count: int = 0, error_count: int = 0,
                match_count: int = 0, search_overhead_bytes: int = 0) -> ToolSearchResult:
        return ToolSearchResult(
            status=status, catalog_bytes=catalog_bytes,
            search_overhead_bytes=search_overhead_bytes, schema_bytes=schema_bytes,
            miss_count=miss_count, error_count=error_count,
            retry_count=max(0, retries), match_count=match_count,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000),
        )
