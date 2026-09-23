"""Deterministic, codebase-aware prompt cleanup for coding-agent contexts.

This module deliberately only edits text-bearing message content.  It never
modifies roles, tool-call metadata, image parts, or an unclosed code fence.
The proxy applies it before L1 cleanup so the existing structural cleaner sees
the smaller, still-readable coding context.
"""
from __future__ import annotations

from collections import Counter
import re
from collections.abc import Callable

from .config import get_settings

_CODE_FENCE = re.compile(
    r"```(?P<language>[A-Za-z0-9_+.-]+)?[^\n]*\n(?P<body>.*?)(?P<closing>\n?```)",
    re.DOTALL,
)
_FILE_REFERENCE = re.compile(
    r"(?:^|\n)(?:file|path)\s*:\s*[^\n]+\.[A-Za-z0-9]+\s*$", re.IGNORECASE | re.MULTILINE
)
_IMPORT_LINE = re.compile(
    r"^\s*(?:"
    r"(?:from\s+[\w.]+\s+import\s+.+)|"
    r"(?:import\s+.+)|"
    r"(?:(?:const|let|var)\s+\w+\s*=\s*)?require\s*\(.+\)\s*;?|"
    r"(?:use\s+.+;)"
    r")\s*$"
)
_TIMESTAMP = re.compile(r"^\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_CRITICAL = re.compile(
    r"\b(?:error|exception|failed|failure|fatal|warning|warn)\b|"
    r"\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\s*\d+\b",
    re.IGNORECASE,
)
_SHELL_NOISE = re.compile(
    r"^\s*(?:"
    r"debug\b|trace\b|"
    r"npm\s+(?:verb|verbose|silly|timing)\b|"
    r"yarn\s+verbose\b|pnpm\s+(?:debug|verbose)\b|"
    r"git\s+(?:trace|verbose)\b|"
    r"remote:\s+(?:enumerating|counting|compressing)\b|"
    r"(?:enumerating|counting|compressing|receiving|resolving)\s+objects:|"
    r"at\s+.+\(.+:\d+(?::\d+)?\)"
    r")",
    re.IGNORECASE,
)
_PYTEST_PASS = re.compile(
    r"^\s*.+::.+\s+(?:PASSED|SKIPPED|XFAIL|XPASS)\s*$|"
    r"^=+\s+.*\b(?:passed|skipped)\b.*=+$",
    re.IGNORECASE,
)


def _is_code_fence(match: re.Match[str], source: str) -> bool:
    """Treat hinted fences (or a preceding File:/Path: reference) as code."""
    if match.group("language"):
        return True
    prefix = source[max(0, match.start() - 300):match.start()]
    return bool(_FILE_REFERENCE.search(prefix))


def _transform_code_fences(content: str, transform: Callable[[str], str]) -> str:
    """Apply a transform to complete code fences, never to ordinary prose."""
    def replace(match: re.Match[str]) -> str:
        if not _is_code_fence(match, content):
            return match.group(0)
        return match.group(0).replace(match.group("body"), transform(match.group("body")), 1)

    return _CODE_FENCE.sub(replace, content)


def truncate_large_files(content: str) -> str:
    """Truncate oversized fenced file bodies while retaining their beginning/end."""
    max_lines = get_settings().codebase_max_file_lines

    def truncate(body: str) -> str:
        lines = body.splitlines()
        if len(lines) <= max_lines:
            return body
        head_count = max_lines // 2
        tail_count = max_lines - head_count
        omitted = len(lines) - max_lines
        kept = [
            *lines[:head_count],
            f"[... middle {omitted} lines truncated ...]",
            *lines[-tail_count:],
        ]
        return "\n".join(kept)

    return _transform_code_fences(content, truncate)


def _import_runs(lines: list[str]) -> list[tuple[int, int, tuple[str, ...]]]:
    """Return contiguous import runs as (start, stop, exact-line signature)."""
    runs: list[tuple[int, int, tuple[str, ...]]] = []
    index = 0
    while index < len(lines):
        if not _IMPORT_LINE.match(lines[index]):
            index += 1
            continue
        start = index
        index += 1
        while index < len(lines) and (_IMPORT_LINE.match(lines[index]) or not lines[index].strip()):
            index += 1
        stop = index
        while stop > start and not lines[stop - 1].strip():
            stop -= 1
        runs.append((start, stop, tuple(lines[start:stop])))
    return runs


def _import_block_occurrences(messages: list[dict]) -> list[tuple[str, ...]]:
    """Find fenced import blocks in strings and OpenAI-compatible text parts."""

    def text_values(message: dict) -> list[str]:
        content = message.get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [
                part["text"]
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
        return []

    occurrences: list[tuple[str, ...]] = []
    for message in messages:
        for content in text_values(message):
            for match in _CODE_FENCE.finditer(content):
                if not _is_code_fence(match, content):
                    continue
                lines = match.group("body").splitlines()
                occurrences.extend(signature for _, _, signature in _import_runs(lines))
    return occurrences


def _extract_import_lines(messages: list[dict]) -> list[str]:
    """Extract individual import lines from fenced code blocks for per-line dedup.

    Only fenced code counts: the replacement pass also only edits fences, so
    counting unfenced lines would inflate frequencies (a fenced line would be
    counted once here and once again by the fence scan).
    """
    import_lines = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            import_lines.extend(_extract_imports_from_text(content))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    import_lines.extend(_extract_imports_from_text(part["text"]))
    return import_lines


def _extract_imports_from_text(text: str) -> list[str]:
    """Extract import lines from code fences in text."""
    import_lines = []
    for match in _CODE_FENCE.finditer(text):
        if not _is_code_fence(match, text):
            continue
        lines = match.group("body").splitlines()
        for line in lines:
            line = line.strip()
            if line and _IMPORT_LINE.match(line):
                import_lines.append(line)
    return import_lines


def _replace_repeated_import_blocks(content: str, repeated: Counter[tuple[str, ...]], seen: Counter[tuple[str, ...]]) -> str:
    """Keep first import block; replace later matching blocks with one marker."""
    fence_index = 0

    def replace_fence(match: re.Match[str]) -> str:
        nonlocal fence_index
        if not _is_code_fence(match, content):
            return match.group(0)
        lines = match.group("body").splitlines()
        replacement_lines: list[str] = []
        cursor = 0
        for start, stop, signature in _import_runs(lines):
            replacement_lines.extend(lines[cursor:start])
            seen[signature] += 1
            if repeated[signature] > 3 and seen[signature] > 1:
                indent_match = re.match(r"\s*", lines[start])
                indent = indent_match.group(0) if indent_match else ""
                replacement_lines.append(
                    f"{indent}[... import block repeated {repeated[signature]} times ...]"
                )
            else:
                replacement_lines.extend(lines[start:stop])
            cursor = stop
        replacement_lines.extend(lines[cursor:])
        fence_index += 1
        body = "\n".join(replacement_lines)
        return match.group(0).replace(match.group("body"), body, 1)

    return _CODE_FENCE.sub(replace_fence, content)


def _replace_repeated_import_lines(content: str, repeated: Counter[str], seen: Counter[str]) -> str:
    """Replace repeated import lines with a marker, keeping first occurrence per line."""
    
    def replace_fence(match: re.Match[str]) -> str:
        if not _is_code_fence(match, content):
            return match.group(0)
        lines = match.group("body").splitlines()
        replacement_lines: list[str] = []
        
        for line in lines:
            stripped = line.strip()
            if stripped and _IMPORT_LINE.match(stripped):
                seen[stripped] += 1
                if repeated[stripped] > 3 and seen[stripped] > 1:
                    # Replace with marker comment
                    indent_match = re.match(r"\s*", line)
                    indent = indent_match.group(0) if indent_match else ""
                    # Keep the marker shorter than even the smallest import
                    # statement so deduplication never inflates tiny snippets.
                    replacement_lines.append(f"{indent}# [dup]")
                else:
                    replacement_lines.append(line)
            else:
                replacement_lines.append(line)
        
        body = "\n".join(replacement_lines)
        return match.group(0).replace(match.group("body"), body, 1)

    return _CODE_FENCE.sub(replace_fence, content)


def deduplicate_imports(messages: list[dict]) -> list[dict]:
    """Replace repeated import lines after the first (per-line dedup)."""
    copies = _copy_messages(messages)
    import_lines = _extract_import_lines(copies)
    repeated: Counter[str] = Counter(import_lines)
    
    # If no import line appears more than 3 times, no dedup needed
    if not any(count > 3 for count in repeated.values()):
        return copies
    
    seen: Counter[str] = Counter()
    return _map_text_content(
        copies, lambda content: _replace_repeated_import_lines(content, repeated, seen)
    )


def _filter_plain_shell_text(content: str) -> str:
    """Remove recognizable shell noise, retaining errors, warnings and results.
    Preserves Python traceback frames as they are error content.
    """
    kept: list[str] = []
    lines = content.splitlines(keepends=True)
    
    in_traceback = False
    
    for line in lines:
        stripped = line.rstrip("\r\n")
        
        # Check if this line starts a Python traceback
        if stripped == "Traceback (most recent call last):":
            in_traceback = True
            kept.append(line)
            continue
            
        # If we're in a traceback, check if this line is part of it
        if in_traceback:
            # Traceback frame lines match the pattern: whitespace + File "...", line N
            if re.match(r"^\s+File \".*\", line \d+", stripped):
                kept.append(line)  # Keep traceback frame lines
                continue
            # If it doesn't match the frame pattern, we're done with traceback
            elif not stripped.startswith(" "):
                in_traceback = False
        
        # Apply existing filters
        if _CRITICAL.search(stripped):
            kept.append(line)
        elif _TIMESTAMP.search(stripped) or _SHELL_NOISE.search(stripped) or _PYTEST_PASS.search(stripped):
            continue  # Filter out noise
        else:
            kept.append(line)
            
    return "".join(kept)


def filter_shell_output(content: str) -> str:
    """Remove recognizable shell noise, retaining errors, warnings and results.

    Fenced snippets are treated as source code and are deliberately untouched.
    """
    parts: list[str] = []
    position = 0
    for match in _CODE_FENCE.finditer(content):
        parts.append(_filter_plain_shell_text(content[position:match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_filter_plain_shell_text(content[position:]))
    return "".join(parts)


def _copy_messages(messages: list[dict]) -> list[dict]:
    """Copy the envelope and text-part containers without touching opaque data."""
    copied: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            copied.append(message)
            continue
        clone = dict(message)
        if isinstance(message.get("content"), list):
            clone["content"] = [
                dict(part) if isinstance(part, dict) else part for part in message["content"]
            ]
        copied.append(clone)
    return copied


def _map_text_content(messages: list[dict], transform: Callable[[str], str]) -> list[dict]:
    """Apply a string transform to text content and OpenAI text parts only."""
    mapped = _copy_messages(messages)
    for message in mapped:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = transform(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = transform(part["text"])
    return mapped


def optimize_codebase_content(messages: list[dict]) -> list[dict]:
    """Apply enabled codebase prompt optimizations without mutating input data."""
    settings = get_settings()
    optimized = _copy_messages(messages)
    if not settings.codebase_optimization_enabled:
        return optimized
    optimized = _map_text_content(optimized, truncate_large_files)
    if settings.codebase_dedupe_imports:
        optimized = deduplicate_imports(optimized)
    if settings.shell_output_filtering:
        optimized = _map_text_content(optimized, filter_shell_output)
    return optimized
