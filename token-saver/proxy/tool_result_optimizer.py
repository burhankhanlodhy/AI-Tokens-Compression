"""Loss-aware, output-side optimization for completed tool-result messages.

Tool calls and their schemas remain protocol data and are never rewritten here.
Only ``role=tool`` content is reduced, after the external tool has completed and
before the request is forwarded upstream.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from .config import get_settings
from .counting import count_text

_TOKEN_MODEL = "gpt-4o"
_RESULT_CACHE_MAX_ENTRIES = 256
_RESULT_CACHE: OrderedDict[tuple[str, str, int, bool, str], str] = OrderedDict()
_RESULT_CACHE_LOCK = threading.Lock()

_NOISY_FILE_SEGMENTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}
_LOG_SEVERITY = re.compile(r"\b(?:fatal|error|warn(?:ing)?|exception|traceback)\b", re.IGNORECASE)
_LOG_NOISE = re.compile(r"\b(?:debug|info|trace)\b", re.IGNORECASE)


def compute_result_fingerprint(tool_call_args: dict) -> str:
    """Return a stable SHA-256 identity for semantically equal tool arguments."""
    try:
        canonical = json.dumps(tool_call_args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(tool_call_args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def clear_result_cache() -> None:
    """Clear the in-process optimization cache (primarily useful for tests)."""
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE.clear()


def _truncation_marker(tokens_removed: int) -> str:
    return f"[... truncated {max(0, tokens_removed)} tokens, full result available on request]"


def _token_count(text: str) -> int:
    return count_text(text, _TOKEN_MODEL)


def _truncate_plain_text(content: str, max_tokens: int, original_tokens: int) -> str:
    """Keep both the beginning (headers/context) and ending (final status)."""
    # Decode tokenizer slices when available. The fallback uses chars and is
    # still bounded by the shared count_text fallback.  Re-render until the
    # notice reports the number of tokens actually removed (the notice itself
    # has a variable-width integer and changes the final token count).
    try:
        from .counting import _encoding  # local cached tokenizer; may be unavailable offline

        tokens = _encoding(_TOKEN_MODEL).encode(content)
        marker = _truncation_marker(original_tokens)
        head_n = max(1, int(max(1, max_tokens - _token_count(marker) - 2) * 0.7))
        tail_n = max(0, max_tokens - _token_count(marker) - 2 - head_n)

        def render() -> str:
            head = _encoding(_TOKEN_MODEL).decode(tokens[:head_n])
            tail = _encoding(_TOKEN_MODEL).decode(tokens[-tail_n:]) if tail_n else ""
            return f"{head}\n{marker}\n{tail}" if tail else f"{head}\n{marker}"

        for _ in range(32):
            candidate = render()
            actual_tokens = _token_count(candidate)
            next_marker = _truncation_marker(max(0, original_tokens - actual_tokens))
            if actual_tokens <= max_tokens and next_marker == marker:
                return candidate
            marker = next_marker
            if actual_tokens > max_tokens:
                excess = actual_tokens - max_tokens
                if tail_n:
                    removed = min(tail_n, excess)
                    tail_n -= removed
                    excess -= removed
                if excess:
                    head_n = max(0, head_n - excess)
        return _truncation_marker(max(0, original_tokens - _token_count(marker)))
    except Exception:  # noqa: BLE001 - tokenizer fallback is intentionally resilient
        marker = _truncation_marker(original_tokens)
        available_chars = max(1, (max_tokens - _token_count(marker)) * 3)
        head_chars = max(1, int(available_chars * 0.7))
        tail_chars = max(0, available_chars - head_chars)

        for _ in range(32):
            head = content[:head_chars]
            tail = content[-tail_chars:] if tail_chars else ""
            candidate = f"{head}\n{marker}\n{tail}" if tail else f"{head}\n{marker}"
            actual_tokens = _token_count(candidate)
            next_marker = _truncation_marker(max(0, original_tokens - actual_tokens))
            if actual_tokens <= max_tokens and next_marker == marker:
                return candidate
            marker = next_marker
            if actual_tokens > max_tokens:
                chars_to_drop = max(1, (actual_tokens - max_tokens) * 4)
                if tail_chars:
                    dropped = min(tail_chars, chars_to_drop)
                    tail_chars -= dropped
                    chars_to_drop -= dropped
                if chars_to_drop:
                    head_chars = max(0, head_chars - chars_to_drop)
        return _truncation_marker(max(0, original_tokens - _token_count(marker)))


def _compact_json(value: Any, *, string_limit: int, list_limit: int) -> Any:
    if isinstance(value, dict):
        return {key: _compact_json(item, string_limit=string_limit, list_limit=list_limit)
                for key, item in value.items()}
    if isinstance(value, list):
        if len(value) <= list_limit:
            return [_compact_json(item, string_limit=string_limit, list_limit=list_limit) for item in value]
        if list_limit <= 0:
            return [f"[... {len(value)} items truncated ...]"]
        left = max(1, list_limit // 2)
        right = max(0, list_limit - left)
        result = [_compact_json(item, string_limit=string_limit, list_limit=list_limit) for item in value[:left]]
        result.append(f"[... {len(value) - left - right} items truncated ...]")
        if right:
            result.extend(_compact_json(item, string_limit=string_limit, list_limit=list_limit) for item in value[-right:])
        return result
    if isinstance(value, str) and len(value) > string_limit:
        if string_limit <= 0:
            return "[... string truncated ...]"
        return value[:string_limit] + " [... string truncated ...]"
    return value


def _json_with_notice(value: Any, original_tokens: int) -> str:
    if isinstance(value, dict):
        result = dict(value)
        result["_tool_result_truncation"] = _truncation_marker(original_tokens)
    else:
        result = {
            "result": value,
            "_tool_result_truncation": _truncation_marker(original_tokens),
        }
    rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    # Recalculate against the actual compact representation rather than making
    # a misleading claim based only on the raw source.
    removed = max(0, original_tokens - _token_count(rendered))
    if isinstance(result, dict):
        result["_tool_result_truncation"] = _truncation_marker(removed)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _minimal_json(value: Any, original_tokens: int, max_tokens: int) -> str:
    """Last-resort valid JSON that retains top-level keys where budget allows."""
    if isinstance(value, dict):
        keys = list(value)
        kept: list[str] = []
        for key in keys:
            candidate = {"_preserved_keys": [*kept, key]}
            rendered = _json_with_notice(candidate, original_tokens)
            if _token_count(rendered) > max_tokens:
                break
            kept.append(key)
        result: Any = {"_preserved_keys": kept}
    else:
        result = {"result": "[... result structure truncated ...]"}
    rendered = _json_with_notice(result, original_tokens)
    if _token_count(rendered) <= max_tokens:
        return rendered
    return _truncation_marker(original_tokens)


def _truncate_json(content: str, parsed: Any, max_tokens: int) -> str:
    original_tokens = _token_count(content)
    for string_limit, list_limit in ((512, 8), (128, 4), (32, 2), (0, 0)):
        candidate = _json_with_notice(
            _compact_json(parsed, string_limit=string_limit, list_limit=list_limit), original_tokens
        )
        if _token_count(candidate) <= max_tokens:
            return candidate
    return _minimal_json(parsed, original_tokens, max_tokens)


def truncate_large_result(content: str, max_tokens: int) -> str:
    """Bound a tool-result string while retaining JSON/table structure when possible."""
    if not isinstance(content, str) or max_tokens < 1 or _token_count(content) <= max_tokens:
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return _truncate_plain_text(content, max_tokens, _token_count(content))
    return _truncate_json(content, parsed, max_tokens)


def filter_result_by_type(content: str, result_type: str) -> str:
    """Apply conservative domain filters without changing unknown result types."""
    if not isinstance(content, str):
        return content
    normalized_type = result_type.lower().replace("-", "_")
    if normalized_type in {"file_listing", "files", "directory_listing"}:
        kept: list[str] = []
        removed = 0
        for line in content.splitlines():
            normalized = line.replace("\\", "/")
            segments = {segment for segment in normalized.split("/") if segment}
            if segments.intersection(_NOISY_FILE_SEGMENTS):
                removed += 1
            else:
                kept.append(line)
        if removed:
            kept.append(f"[... {removed} irrelevant file entries filtered ...]")
        return "\n".join(kept)
    if normalized_type in {"log", "logs"}:
        lines = content.splitlines()
        kept = [line for line in lines if _LOG_SEVERITY.search(line)]
        omitted = sum(1 for line in lines if _LOG_NOISE.search(line) and line not in kept)
        if omitted:
            kept.append(f"[... {omitted} informational/debug log lines omitted ...]")
        # A log with no severity data can still contain useful plain output;
        # retain its first line rather than turning it into an empty result.
        if not kept and lines:
            kept.append(lines[0])
        return "\n".join(kept)
    # JSON/API output is intentionally left structurally intact here. Its
    # status/error keys are retained by the JSON-aware truncator above.
    return content


def _result_type(message: Mapping[str, Any], tool_call_args: Mapping[str, Any] | None = None) -> str:
    explicit = message.get("result_type")
    if isinstance(explicit, str):
        return explicit
    name = str(
        message.get("name")
        or message.get("tool_name")
        or (tool_call_args or {}).get("tool_name")
        or ""
    ).lower()
    if any(token in name for token in ("list", "glob", "find", "directory")):
        return "file_listing"
    if any(token in name for token in ("log", "journal")):
        return "log"
    if any(token in name for token in ("http", "api", "request", "curl")):
        return "api"
    return "unknown"


def optimize_tool_result(
    message: dict,
    *,
    tool_call_args: dict | None = None,
    max_tokens: int | None = None,
    filtering: bool | None = None,
    cache_enabled: bool | None = None,
) -> dict:
    """Return a copied tool message with optional filtering, truncation, and cache reuse.

    The cache is exact on both canonical tool arguments and raw result content;
    it avoids repeated optimization work but never substitutes a stale result
    for changed tool output.
    """
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return dict(message)
    settings = get_settings()
    max_tokens = settings.tool_result_max_tokens if max_tokens is None else max_tokens
    filtering = settings.tool_result_filtering if filtering is None else filtering
    cache_enabled = settings.tool_result_cache_enabled if cache_enabled is None else cache_enabled
    content = message["content"]
    identity = tool_call_args if tool_call_args is not None else {
        "tool_name": message.get("name"), "tool_call_id": message.get("tool_call_id"),
    }
    result_type = _result_type(message, identity)
    cache_key = (
        compute_result_fingerprint(identity),
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        int(max_tokens),
        bool(filtering),
        result_type,
    )
    if cache_enabled:
        with _RESULT_CACHE_LOCK:
            cached = _RESULT_CACHE.get(cache_key)
            if cached is not None:
                _RESULT_CACHE.move_to_end(cache_key)
                return {**message, "content": cached}

    filtered = filter_result_by_type(content, result_type) if filtering else content
    optimized = truncate_large_result(filtered, int(max_tokens))
    if cache_enabled:
        with _RESULT_CACHE_LOCK:
            _RESULT_CACHE[cache_key] = optimized
            _RESULT_CACHE.move_to_end(cache_key)
            while len(_RESULT_CACHE) > _RESULT_CACHE_MAX_ENTRIES:
                _RESULT_CACHE.popitem(last=False)
    return {**message, "content": optimized}
