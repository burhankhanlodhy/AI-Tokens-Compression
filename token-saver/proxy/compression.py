"""LLMLingua-2 wrapper. Lazy-loaded singleton so the model loads once.

Compression only applies to long string contents; roles and message
structure are preserved exactly so the upstream request stays valid.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .config import get_settings
from .tool_protocol import is_tool_protocol_message

logger = logging.getLogger(__name__)

_compressor: Any = None
_lock = threading.Lock()

# LLMLingua-2's bundled BERT path has a 512-token input window. Never invoke
# it on a longer block: transformers otherwise emits a warning and may index
# past the model's positional embeddings, producing corrupted output.
MAX_LMLINGUA_INPUT_TOKENS = 512


def _get_compressor() -> Any:
    global _compressor
    if _compressor is None:
        with _lock:
            if _compressor is None:  # double-checked
                from llmlingua import PromptCompressor

                s = get_settings()
                logger.info("Loading LLMLingua-2 model: %s", s.llmlingua_model)
                # CPU-only: torch may be installed without CUDA; force device_map='cpu'
                # so llmlingua doesn't try to initialize CUDA.
                _compressor = PromptCompressor(
                    model_name=s.llmlingua_model, use_llmlingua2=True, device_map="cpu"
                )
    return _compressor


def _input_token_count(compressor: Any, text: str) -> int | None:
    """Count tokens without truncation, or return None if unavailable."""
    tokenizer = getattr(compressor, "tokenizer", None)
    if tokenizer is None:
        return None
    encoded = tokenizer(
        text,
        add_special_tokens=True,
        truncation=False,
        return_attention_mask=False,
    )
    input_ids = encoded.get("input_ids") if hasattr(encoded, "get") else None
    if input_ids is None:
        return None
    # A single string normally returns a flat list; tolerate a one-item batch
    # from lightweight test doubles and alternate tokenizer implementations.
    if input_ids and isinstance(input_ids[0], (list, tuple)):
        input_ids = input_ids[0]
    return len(input_ids)


def compress_text(text: str) -> str:
    """Compress a single string, conservatively skipping unsafe inputs."""
    s = get_settings()
    if len(text) < s.min_chars_to_compress:
        return text
    try:
        compressor = _get_compressor()
        token_count = _input_token_count(compressor, text)
        if token_count is not None and token_count > MAX_LMLINGUA_INPUT_TOKENS:
            logger.warning(
                "Skipping LLMLingua compression: input has %d tokens, "
                "maximum is %d",
                token_count,
                MAX_LMLINGUA_INPUT_TOKENS,
            )
            return text
        result = compressor.compress_prompt(
            [text],
            rate=s.compression_rate,
            force_tokens=list(s.force_tokens),
            drop_consecutive=True,
        )
        compressed = result.get("compressed_prompt", "")
        # Safety net: never return something longer than the input.
        return compressed if compressed and len(compressed) < len(text) else text
    except Exception:  # noqa: BLE001 — compression must never break the proxy
        logger.exception("Compression failed; passing text through unchanged")
        return text


def has_compressible_content(messages: list[dict]) -> bool:
    """True if any eligible message is long enough for compress_text to act on.

    Used to gate conciseness injection: on short prompts, compression saves
    nothing, so the injected system message would be pure input-token
    overhead rather than a net win.
    """
    s = get_settings()
    for msg in messages:
        content = msg.get("content")
        eligible = msg.get("role") != "system" or s.compress_system_messages
        if not eligible:
            continue
        if isinstance(content, str):
            if len(content) >= s.min_chars_to_compress:
                return True
        elif isinstance(content, list):
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and len(part.get("text", "")) >= s.min_chars_to_compress
                ):
                    return True
    return False


def compress_messages(messages: list[dict]) -> list[dict]:
    """Compress eligible message contents, preserving structure."""
    s = get_settings()
    out: list[dict] = []
    for msg in messages:
        # Tool results and assistant tool-call envelopes are machine-readable
        # protocol state. Keep the entire message untouched even if a future
        # caller invokes this helper outside the request-level hard gate.
        if is_tool_protocol_message(msg):
            out.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            eligible = msg.get("role") != "system" or s.compress_system_messages
            out.append(
                {**msg, "content": compress_text(content) if eligible else content}
            )
        elif isinstance(content, list):
            parts = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and (msg.get("role") != "system" or s.compress_system_messages)
                ):
                    parts.append({**part, "text": compress_text(part.get("text", ""))})
                else:
                    parts.append(part)
            out.append({**msg, "content": parts})
        else:
            out.append(msg)
    return out
