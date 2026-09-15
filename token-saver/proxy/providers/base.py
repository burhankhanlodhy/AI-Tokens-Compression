"""ProviderAdapter protocol + shared helpers (adapter-api-surface.md Section 3)."""
from __future__ import annotations

from typing import Protocol

import httpx

from .model import (
    AdapterRequest,
    NormalizedRequest,
    NormalizedResponse,
    ProviderError,
    StreamEvent,
)

# auth_style values (providers table constraint):
#   bearer | x-api-key | api-key | query-param | x-goog-api-key | none
AUTH_STYLES = ("bearer", "x-api-key", "api-key", "query-param", "x-goog-api-key", "none")


class ProviderAdapter(Protocol):
    name: str

    def matches(self, model: str) -> bool: ...

    def normalize_model(self, model: str) -> str: ...

    def auth_headers(self, credential: str) -> dict[str, str]: ...

    def translate_request(self, req: NormalizedRequest) -> AdapterRequest: ...

    def translate_response(
        self, raw: httpx.Response, req: NormalizedRequest
    ) -> NormalizedResponse: ...

    def translate_stream_chunk(self, line: str, req: NormalizedRequest) -> StreamEvent: ...


def auth_headers_for(style: str, credential: str) -> dict[str, str]:
    """Credential placement per registry auth_style (C2)."""
    if style == "bearer":
        return {"Authorization": f"Bearer {credential}"}
    if style == "x-api-key":
        return {"x-api-key": credential}
    if style == "api-key":
        return {"api-key": credential}
    if style == "x-goog-api-key":
        return {"x-goog-api-key": credential}
    if style == "query-param":
        return {}  # credential appended to URL by caller; never logged
    return {}  # "none": local backends needing no auth


def error_from_status(status: int, message: str, retry_after_s: float | None = None) -> ProviderError:
    """Map an HTTP status to exactly one ProviderError.kind (C8)."""
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limit"
    elif status in (502, 503, 529):
        kind = "overloaded"
    elif 400 <= status < 500:
        kind = "invalid_request"
    else:
        kind = "upstream"
    return ProviderError(kind=kind, message=message, retry_after_s=retry_after_s, status=status)
