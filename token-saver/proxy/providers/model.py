"""Normalized request/response model shared by all provider adapters.

The proxy pipeline (classify -> compress -> inject -> forward -> relay)
operates ONLY on these types; adapters translate at the edges. This keeps
compression/counting provider-agnostic (adapter-api-surface.md Section 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContentPart:
    """One part of a multimodal message, normalized across providers."""

    type: str  # "text" | "image" | "file"
    text: str | None = None
    source: dict[str, Any] | None = None  # {media_type, data|url}


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list[ContentPart]
    tool_calls: list[dict[str, Any]] | None = None  # assistant tool_use
    tool_call_id: str | None = None  # for role="tool" results
    name: str | None = None


@dataclass
class NormalizedRequest:
    model: str
    messages: list[Message] = field(default_factory=list)
    system: str | None = None
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # provider passthrough


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int | None = None  # None means no provider evidence
    cache_write_tokens: int | None = None


@dataclass
class ProviderError:
    kind: str  # "auth" | "rate_limit" | "overloaded" | "invalid_request" | "upstream"
    message: str
    retry_after_s: float | None
    status: int


@dataclass
class AdapterRequest:
    """Wire-level request produced by an adapter."""

    path: str  # e.g. "/v1/chat/completions" or "/v1/messages"
    headers: dict[str, str]
    json_body: dict[str, Any]


@dataclass
class StreamEvent:
    """One provider-agnostic SSE event (adapter-stream re-emit)."""

    kind: str  # "delta" | "usage" | "done" | "error"
    delta_text: str | None = None
    usage: Usage | None = None
    error: ProviderError | None = None
    raw_line: str | None = None  # original SSE line, re-emitted to the client


@dataclass
class NormalizedResponse:
    status: int
    content: bytes
    output_text: str = ""
    usage: Usage | None = None
    error: ProviderError | None = None
