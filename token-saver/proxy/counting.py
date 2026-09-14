"""Token counting (tiktoken, with heuristic fallback) and conciseness injection."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .config import get_settings


@lru_cache(maxsize=8)
def _encoding(model: str):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # o-series and unknown models: use the modern default.
        return tiktoken.get_encoding("o200k_base")


def count_text(text: str, model: str) -> int:
    try:
        return len(_encoding(model).encode(text))
    except Exception:  # noqa: BLE001 — offline / unknown tokenizer
        return max(1, len(text) // 4)


def count_messages(messages: list[dict], model: str) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += count_text(content, model) + 4  # per-message overhead
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += count_text(part.get("text", ""), model) + 4
        if "name" in msg:
            total += count_text(str(msg["name"]), model)
    total += 2  # reply priming
    return total


def count_output(response_json: dict[str, Any], model: str) -> int:
    """Output tokens from a non-streaming completion response."""
    usage = response_json.get("usage") or {}
    if usage.get("completion_tokens"):
        return int(usage["completion_tokens"])
    choices = response_json.get("choices") or []
    text = "".join(
        (c.get("message") or {}).get("content") or c.get("text") or ""
        for c in choices
    )
    return count_text(text, model)


def extract_output_text_from_sse_chunk(chunk_json: dict[str, Any]) -> str:
    """Pull incremental text out of a streaming chunk."""
    parts: list[str] = []
    for choice in chunk_json.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content"):
            parts.append(delta["content"])
        elif choice.get("text"):
            parts.append(choice["text"])
    return "".join(parts)


def inject_conciseness(messages: list[dict]) -> list[dict]:
    """Prepend (or merge into) a system message discouraging padding.

    This is NOT second-pass output compression — it makes the model generate
    fewer tokens up front, at no extra cost or risk (plan §8 Phase 5).
    """
    s = get_settings()
    instruction = s.conciseness_instruction
    if messages and messages[0].get("role") == "system":
        first = messages[0]
        content = first.get("content")
        if isinstance(content, str):
            messages = [
                {**first, "content": f"{content}\n\n{instruction}"},
                *messages[1:],
            ]
        else:
            messages = [
                {
                    **first,
                    "content": [
                        *(
                            content
                            if isinstance(content, list)
                            else [{"type": "text", "text": str(content)}]
                        ),
                        {"type": "text", "text": instruction},
                    ],
                },
                *messages[1:],
            ]
    else:
        messages = [{"role": "system", "content": instruction}, *messages]
    return messages


def parse_body(raw: bytes) -> dict[str, Any]:
    return json.loads(raw)
