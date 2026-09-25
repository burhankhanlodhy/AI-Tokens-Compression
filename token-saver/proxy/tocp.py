"""Tenant/session-scoped, TTL-bounded in-process TOCP continuation store.

The API accepts only trusted scope values supplied by the authenticated caller.
IDs are random capabilities but scope checks remain mandatory.
"""
from __future__ import annotations

import math
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Mapping


class ContinuationNotFound(LookupError):
    """Missing, expired, or unauthorized continuation (intentionally conflated)."""


class InvalidRange(ValueError):
    pass


class StoreCapacityExceeded(ValueError):
    """The store is full; unexpired continuations are never evicted."""


@dataclass(frozen=True)
class ContinuationPreview:
    continuation_id: str
    preview: str
    omitted_chars: int
    total_chars: int
    available_segments: int
    segment_chars: int


@dataclass(frozen=True)
class _Entry:
    tenant_id: str
    session_id: str
    content: str
    expires_at: float
    metadata: Mapping[str, object]


class ContinuationStore:
    def __init__(self, *, ttl_seconds: int = 900, max_entries: int = 2048,
                 segment_chars: int = 4000, max_total_chars: int = 16_000_000,
                 clock: Callable[[], float] = time.time):
        if ttl_seconds <= 0 or max_entries <= 0 or segment_chars <= 0 or max_total_chars <= 0:
            raise ValueError("limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.segment_chars = segment_chars
        self.max_total_chars = max_total_chars
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def size(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._entries)

    def _purge_expired(self) -> int:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            del self._entries[key]
        return len(expired)

    def cleanup(self) -> int:
        with self._lock:
            return self._purge_expired()

    def save(self, tenant_id: str, session_id: str, content: str,
             metadata: Mapping[str, object] | None = None) -> ContinuationPreview:
        if not tenant_id or not session_id or not isinstance(content, str):
            raise ValueError("trusted tenant_id, session_id, and string content are required")
        if len(content) > self.max_total_chars:
            raise StoreCapacityExceeded("continuation exceeds store capacity")
        with self._lock:
            self._purge_expired()
            total_chars = sum(len(entry.content) for entry in self._entries.values())
            if (len(self._entries) >= self.max_entries
                    or total_chars + len(content) > self.max_total_chars):
                raise StoreCapacityExceeded(
                    "continuation store full; existing unexpired entries were retained"
                )
            continuation_id = secrets.token_urlsafe(24)
            self._entries[continuation_id] = _Entry(
                tenant_id, session_id, content, self._clock() + self.ttl_seconds,
                dict(metadata or {}),
            )
        segments = math.ceil(len(content) / self.segment_chars)
        preview = content[:self.segment_chars]
        return ContinuationPreview(continuation_id, preview, len(content) - len(preview),
                                   len(content), segments, self.segment_chars)

    def _authorized(self, continuation_id: str, tenant_id: str, session_id: str) -> _Entry:
        with self._lock:
            self._purge_expired()
            entry = self._entries.get(continuation_id)
            if entry is None or entry.tenant_id != tenant_id or entry.session_id != session_id:
                raise ContinuationNotFound(continuation_id)
            self._entries.move_to_end(continuation_id)
            return entry

    def get_segment(self, continuation_id: str, tenant_id: str, session_id: str,
                    index: int) -> str:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise InvalidRange("segment index must be a non-negative integer")
        entry = self._authorized(continuation_id, tenant_id, session_id)
        start = index * self.segment_chars
        if start >= len(entry.content) and not (index == 0 and not entry.content):
            raise InvalidRange("segment index is outside the continuation")
        return entry.content[start:start + self.segment_chars]

    def get_range(self, continuation_id: str, tenant_id: str, session_id: str,
                  start: int, end: int) -> str:
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
                or not isinstance(end, int) or start < 0 or end <= start):
            raise InvalidRange("range must be a non-empty half-open character interval")
        entry = self._authorized(continuation_id, tenant_id, session_id)
        if end > len(entry.content):
            raise InvalidRange("range exceeds continuation length")
        return entry.content[start:end]


continuation_store = ContinuationStore()


def build_continuation_result(content: str, tenant_id: str, session_id: str,
                             *, store: ContinuationStore = continuation_store,
                             metadata: Mapping[str, object] | None = None) -> str:
    """Save full output and emit a bounded, machine-readable retrieval envelope."""
    preview = store.save(tenant_id, session_id, content, metadata)
    envelope = {
        "summary": f"Tool output preserved in {preview.available_segments} bounded segment(s); "
                   f"{preview.total_chars} characters across {content.count(chr(10)) + 1} line(s).",
        "_tocp": {
            "continuation_id": preview.continuation_id,
            "omitted_chars": preview.omitted_chars,
            "total_chars": preview.total_chars,
            "available_segments": preview.available_segments,
            "segment_chars": preview.segment_chars,
            "retrieval": "GET /v1/tool-results/{continuation_id}?segment=N (same tenant and session required)",
        },
        "preview": preview.preview,
    }
    try:
        import json
        original = json.loads(content)
        if isinstance(original, dict):
            for key in ("status", "error", "exit_code"):
                if key in original:
                    envelope[key] = original[key]
    except (ValueError, TypeError):
        pass
    import json
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
