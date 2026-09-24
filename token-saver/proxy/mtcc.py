"""Experimental multi-turn context compression (MTCC), default-off at the caller.

This module is deterministic and performs no model summarization. Original UTF-8
turn text is retained in a bounded, TTL-scoped process-local store; production
Postgres wiring and quality evidence are still deployment gates.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Sequence


class TurnNotFound(LookupError):
    """Turn is missing, expired, or outside the authenticated scope."""


class StoreCapacityExceeded(ValueError):
    """Store is full; fail open rather than evict unexpired source turns."""


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("role must be a non-empty string")
        if not isinstance(self.content, str):
            raise ValueError("content must be a string")

    def to_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class MTCCConfig:
    recent_turns: int = 6
    ttl_seconds: int = 900
    max_turns: int = 10000
    max_total_chars: int = 16_000_000

    def __post_init__(self) -> None:
        if min(self.recent_turns, self.ttl_seconds, self.max_turns, self.max_total_chars) <= 0:
            raise ValueError("MTCC limits must be positive")


@dataclass(frozen=True)
class _StoredTurn:
    tenant_id: str
    api_key_id: str
    session_id: str
    turn_index: int
    role: str
    content: str
    digest: str
    expires_at: float


class MTCCStore:
    """Bounded in-memory exact source store; reads require all trusted scope keys."""

    def __init__(self, *, config: MTCCConfig = MTCCConfig(), clock: Callable[[], float] = time.time):
        self.config = config
        self._clock = clock
        self._entries: OrderedDict[str, _StoredTurn] = OrderedDict()
        self._lock = threading.RLock()

    def _purge(self) -> int:
        now = self._clock()
        expired = [ref for ref, item in self._entries.items() if item.expires_at <= now]
        for ref in expired:
            del self._entries[ref]
        return len(expired)

    def cleanup(self) -> int:
        with self._lock:
            return self._purge()

    def save(self, tenant_id: str, api_key_id: str, session_id: str,
             turn_index: int, turn: ConversationTurn) -> str:
        if not all(isinstance(value, str) and value.strip()
                   for value in (tenant_id, api_key_id, session_id)):
            raise ValueError("trusted tenant, API-key, and session scope are required")
        if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
            raise ValueError("turn_index must be a non-negative integer")
        content_bytes = turn.content.encode("utf-8")
        if len(content_bytes) > self.config.max_total_chars:
            raise ValueError("turn exceeds MTCC store capacity")
        ref = secrets.token_urlsafe(24)
        item = _StoredTurn(tenant_id, api_key_id, session_id, turn_index, turn.role,
                           turn.content, hashlib.sha256(content_bytes).hexdigest(),
                           self._clock() + self.config.ttl_seconds)
        with self._lock:
            self._purge()
            total_bytes = sum(len(x.content.encode("utf-8")) for x in self._entries.values())
            if (len(self._entries) >= self.config.max_turns
                    or total_bytes + len(content_bytes) > self.config.max_total_chars):
                raise StoreCapacityExceeded("MTCC store full; originals were not evicted")
            self._entries[ref] = item
        return ref

    def retrieve(self, ref: str, tenant_id: str, api_key_id: str,
                 session_id: str) -> ConversationTurn:
        with self._lock:
            self._purge()
            item = self._entries.get(ref)
            if (item is None or (item.tenant_id, item.api_key_id, item.session_id)
                    != (tenant_id, api_key_id, session_id)):
                raise TurnNotFound(ref)
            if hashlib.sha256(item.content.encode("utf-8")).hexdigest() != item.digest:
                raise TurnNotFound(ref)
            self._entries.move_to_end(ref)
            return ConversationTurn(item.role, item.content)


@dataclass(frozen=True)
class MTCCResult:
    messages: list[dict[str, str]]
    source_refs: list[str]
    protected_turns: int
    original_chars: int
    output_chars: int
    input_tokens_before: int
    input_tokens_after: int
    policy_overhead_tokens: int
    summarizer_provider_cost_usd: float
    summarizer_latency_ms: float
    relevance: dict[int, str] = field(default_factory=dict)


_PATH = re.compile(r"(?:^|[\s`'\"])([\w./-]+\.[A-Za-z0-9]{1,12})(?=$|[\s`'\":,;()])")
_SYMBOL = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*\s*\(|[A-Z][A-Z0-9_]{2,}|[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*)")
_ERROR = re.compile(r"\b(error|exception|failed|failure|traceback|assertionerror|errno|panic)\b", re.I)
_CONSTRAINT = re.compile(r"\b(must|never|don't|do not|keep|preserve|require|constraint|ensure|without|only if|instead of)\b", re.I)
_TOOL_REF = re.compile(r"\b(?:tool output|tool result|continuation|result ref|reference id)\b|(?:_tocp|continuation_id|result-[\w-]+)", re.I)
_FILLER = re.compile(r"^(?:thanks|thank you|ok(?:ay)?|got it|sounds good|you're welcome|noted|hello|hi)[.!\s]*$", re.I)


def _entities(content: str) -> list[str]:
    values = _PATH.findall(content) + _SYMBOL.findall(content)
    unique: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s*\($", "", value).strip()
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return unique[:12]


def _tier(turn: ConversationTurn) -> str:
    content = turn.content.strip()
    role = turn.role.lower()
    if role in {"system", "developer"} or _CONSTRAINT.search(content) or _ERROR.search(content) or _TOOL_REF.search(content):
        return "high"
    if role == "user" and not _FILLER.fullmatch(content):
        return "medium"  # Keep older user intent inspectable; only explicit constraints are verbatim-protected.
    if role == "tool":
        return "high"
    if _entities(content):
        return "medium"
    return "low"


def _estimate_tokens(text: str) -> int:
    # Stable, intentionally conservative proxy for replay comparisons; not provider token accounting.
    return (len(text.encode("utf-8")) + 3) // 4


def compress_history(turns: Sequence[ConversationTurn], *, tenant_id: str,
                     api_key_id: str, session_id: str, store: MTCCStore,
                     config: MTCCConfig | None = None, full_context: bool = False) -> MTCCResult:
    """Build a safe prompt view and keep exact references for every omitted turn.

    High-relevance older turns remain verbatim. Medium turns become explicit
    entity/fact records; low turns are represented by a reference-bearing
    collapsed filler record. Recent turns always remain byte-for-byte text.
    """
    cfg = config or store.config
    started = time.perf_counter()
    if full_context or not turns:
        messages = [turn.to_message() for turn in turns]
        elapsed = (time.perf_counter() - started) * 1000
        text = "\n".join(turn.content for turn in turns)
        return MTCCResult(messages, [], 0, sum(len(t.content) for t in turns), len(text),
                          _estimate_tokens(text), _estimate_tokens(text), 0, 0.0, elapsed, {})
    saved_refs = [store.save(tenant_id, api_key_id, session_id, i, turn) for i, turn in enumerate(turns)]
    recent_start = max(0, len(turns) - cfg.recent_turns)
    messages: list[dict[str, str]] = []
    refs: list[str] = list(saved_refs)
    protected = 0
    relevance: dict[int, str] = {}
    facts: list[str] = []
    low_refs: list[str] = []
    for i, turn in enumerate(turns):
        tier = _tier(turn)
        relevance[i] = tier
        if i >= recent_start:
            messages.append(turn.to_message())
        elif tier == "high":
            messages.append(turn.to_message())
            protected += 1
        elif tier == "medium":
            entities = _entities(turn.content)
            facts.append(f"turn {i} ({turn.role}); source_ref={saved_refs[i]}; entities: {', '.join(entities)}; fact: {turn.content.strip()}")
        else:
            low_refs.append(f"{i}:{saved_refs[i]}")
    if facts or low_refs:
        sections = ["MTCC structured facts (source turns retained; retrieve by reference):"]
        sections.extend(f"- {fact}" for fact in facts)
        if low_refs:
            sections.append(f"- Collapsed low-relevance/filler turns: {len(low_refs)}; turn_indexes=" + ",".join(ref.split(":", 1)[0] for ref in low_refs))
        messages.insert(0, {"role": "system", "content": "\n".join(sections)})
    output = "\n".join(f"{m['role']}:{m['content']}" for m in messages)
    original = "\n".join(f"{t.role}:{t.content}" for t in turns)
    elapsed = (time.perf_counter() - started) * 1000
    before, after = _estimate_tokens(original), _estimate_tokens(output)
    return MTCCResult(messages, refs, protected, len(original), len(output), before, after,
                      max(0, after - _estimate_tokens("\n".join(t.content for t in turns[recent_start:]))),
                      0.0, elapsed, relevance)


@dataclass(frozen=True)
class ReplayReport:
    sessions: int
    turns: int
    context_reduction_pct: float
    task_success_regression_pp: float | None
    wrong_diagnosis_delta: int | None
    summarizer_provider_cost_usd: float
    summarizer_latency_ms: float
    policy_overhead_tokens: int
    evidence_kind: str = "synthetic_deterministic_fixture"


def replay_fixture_sessions(*, session_count: int = 30, turns_per_session: int = 10) -> ReplayReport:
    """Deterministic local corpus replay; explicitly not task-quality evidence."""
    if session_count < 1 or turns_per_session < 10:
        raise ValueError("replay requires at least one session and 10 turns per session")
    store = MTCCStore(config=MTCCConfig(recent_turns=3, max_turns=session_count * turns_per_session + 1))
    before = after = overhead = latency = 0
    for s in range(session_count):
        turns = [ConversationTurn("user" if i % 2 == 0 else "assistant",
                    ("Thanks." if i % 2 == 0 else "Okay.")
                    if i not in {0, 1, turns_per_session - 1}
                    else f"For src/module_{s}.py, preserve public API and fix RuntimeError at handle_{s}().")
                 for i in range(turns_per_session)]
        result = compress_history(turns, tenant_id=f"tenant-{s % 3}", api_key_id=f"key-{s % 3}",
                                  session_id=f"session-{s}", store=store)
        before += result.input_tokens_before
        after += result.input_tokens_after
        overhead += result.policy_overhead_tokens
        latency += result.summarizer_latency_ms
    reduction = (before - after) * 100 / before if before else 0.0
    return ReplayReport(session_count, session_count * turns_per_session, reduction, None, None,
                        0.0, latency, overhead)
