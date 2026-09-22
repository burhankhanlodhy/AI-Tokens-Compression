"""Detection and preservation rules for agent/tool protocol traffic."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any


_REDUNDANT_PARAMETER_DESCRIPTIONS = {
    "email": {
        "email",
        "email address",
        "user email",
        "user email address",
        "the user email address",
    },
}


def _normalized_description(value: str) -> str:
    """Return a conservative comparison form for a property description."""
    return " ".join(value.lower().strip(". :;,-").split())


def _is_redundant_parameter_description(name: str, description: object) -> bool:
    """Return whether ``description`` only restates a parameter's name.

    This is deliberately an allow-list rather than heuristic NLP: removing a
    hint such as an API's required format or business meaning can break tool
    selection even when the property name looks familiar.
    """
    if not isinstance(description, str):
        return False
    return _normalized_description(description) in _REDUNDANT_PARAMETER_DESCRIPTIONS.get(
        name.lower(), set()
    )


def _minify_parameter_descriptions(schema: dict[str, Any]) -> None:
    """Remove only safe redundant descriptions from JSON-schema properties."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, property_schema in properties.items():
            if not isinstance(name, str) or not isinstance(property_schema, dict):
                continue
            if _is_redundant_parameter_description(
                name, property_schema.get("description")
            ):
                property_schema.pop("description", None)
            _minify_parameter_descriptions(property_schema)

    for branch_key in ("items", "allOf", "anyOf", "oneOf", "not"):
        branch = schema.get(branch_key)
        if isinstance(branch, dict):
            _minify_parameter_descriptions(branch)
        elif isinstance(branch, list):
            for member in branch:
                if isinstance(member, dict):
                    _minify_parameter_descriptions(member)


def minify_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Copy a tool schema while stripping safely redundant parameter details.

    Type, validation, required, and function-level information stays verbatim;
    callers can therefore continue to send the result to strict providers.
    """
    minified = copy.deepcopy(tool)
    function = minified.get("function")
    if not isinstance(function, dict):
        return minified
    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        _minify_parameter_descriptions(parameters)
    return minified


def compact_schema_bytes(tools: list[dict[str, Any]]) -> int:
    """Return the UTF-8 byte size of deterministic compact schema JSON."""
    return len(
        json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )


class SchemaCache:
    """In-process cache of minified schema arrays, isolated from callers."""

    def __init__(
        self, *, max_entries: int = 256, max_bytes: int = 1_048_576
    ) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("schema cache limits must be positive")
        self._cache: OrderedDict[str, tuple[list[dict[str, Any]], int]] = OrderedDict()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._cached_bytes = 0
        self.compression_count = 0
        self.cache_hits = 0

    @property
    def cached_entries(self) -> int:
        """Current bounded-entry count, exposed for observability and tests."""
        return len(self._cache)

    @property
    def cached_bytes(self) -> int:
        """Current bounded compact-schema bytes held by this process."""
        return self._cached_bytes

    @staticmethod
    def _compute_fingerprint(tools: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def clear(self) -> None:
        self._cache.clear()
        self._cached_bytes = 0
        self.compression_count = 0
        self.cache_hits = 0

    def get_or_compress(
        self, tools: list[dict[str, Any]], *, use_cache: bool = True
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return a minified copy and whether the result came from the cache."""
        fingerprint = self._compute_fingerprint(tools)
        cached = self._cache.get(fingerprint) if use_cache else None
        if cached is not None:
            self.cache_hits += 1
            self._cache.move_to_end(fingerprint)
            return copy.deepcopy(cached[0]), True

        minified = [minify_tool_schema(tool) for tool in tools]
        self.compression_count += 1
        minified_bytes = compact_schema_bytes(minified)
        # Unchanged schemas have no compression benefit, and an oversized
        # single schema must not monopolize this process-global cache.
        if use_cache and minified != tools and minified_bytes <= self._max_bytes:
            while self._cache and (
                len(self._cache) >= self._max_entries
                or self._cached_bytes + minified_bytes > self._max_bytes
            ):
                _, (_, evicted_bytes) = self._cache.popitem(last=False)
                self._cached_bytes -= evicted_bytes
            self._cache[fingerprint] = (copy.deepcopy(minified), minified_bytes)
            self._cached_bytes += minified_bytes
        return minified, False


def is_tool_protocol_message(message: Mapping[str, Any]) -> bool:
    """Return whether a message carries machine-readable tool state.

    Tool results and assistant tool-call envelopes are protocol data, not prose;
    lossily rewriting either can make a valid agent turn unparsable upstream.
    The key-presence check deliberately treats ``tool_calls: []`` as protocol
    state too, keeping malformed/partial envelopes fail-safe.
    """
    return message.get("role") == "tool" or "tool_calls" in message


def has_tool_calling_state(body: Mapping[str, Any]) -> bool:
    """Return whether a request must bypass lossy/L1 body transforms.

    A declared non-empty ``tools`` list, any present ``tool_choice`` field, a
    message carrying ``tool_calls``, or a ``role=tool`` result is enough to
    activate the hard passthrough gate. This is intentionally conservative:
    protocol state must remain unchanged even when the envelope is partial.
    """
    if body.get("tools"):
        return True
    if "tool_choice" in body:
        return True
    messages = body.get("messages")
    if isinstance(messages, list):
        return any(
            isinstance(message, Mapping) and is_tool_protocol_message(message)
            for message in messages
        )
    return False
