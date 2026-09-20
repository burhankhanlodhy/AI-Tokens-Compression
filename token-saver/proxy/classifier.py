"""Heuristic task-aware router: decide whether to compress or pass through.

The differentiator (plan §8 Phase 4): code and precise technical specs must
NOT be compressed — compression hurts those tasks. Everything conversational
gets compressed.
"""
from __future__ import annotations

import re
from typing import Literal

from .config import get_settings
from .tool_protocol import has_tool_calling_state

Route = Literal["compress", "passthrough"]

# Markers that strongly indicate code / exact specs.
_CODE_FENCE = re.compile(r"```")
_KEYWORDS = re.compile(
    r"\b(def|class|import|from|function|const|let|var|return|async|await|"
    r"public|private|void|struct|impl|fn|package|namespace|typedef)\b"
)
_FILE_EXT = re.compile(
    r"\b[\w./-]+\.(py|js|ts|tsx|jsx|java|c|cpp|h|hpp|go|rs|rb|php|sh|"
    r"sql|json|yaml|yml|toml|xml|html|css|swift|kt|scala)\b"
)
_STRUCTURED = re.compile(r"^\s*[{[]", re.MULTILINE)  # JSON-ish blobs
_DIFF = re.compile(r"^(---|\+\+\+|@@|diff --git)", re.MULTILINE)

_CODE_CHARS = re.compile(r"[{}()\[\];=<>|&*/\\+\-]")


def _looks_like_code(text: str) -> bool:
    s = get_settings()
    if len(text) < s.min_chars_to_classify:
        return False
    if _CODE_FENCE.search(text):
        return True
    if _DIFF.search(text):
        return True
    if _STRUCTURED.search(text) and text.count("\n") >= 3:
        return True
    if _FILE_EXT.search(text) and _KEYWORDS.search(text):
        return True
    # Symbol density: code has far more punctuation than prose.
    sample = text[:4000]
    density = len(_CODE_CHARS.findall(sample)) / max(len(sample), 1)
    if density > s.code_symbol_density_threshold and _KEYWORDS.search(sample):
        return True
    return False


def classify(messages: list[dict]) -> Route:
    """Return 'passthrough' for code/exact-spec requests, else 'compress'."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            if _looks_like_code(content):
                return "passthrough"
        elif isinstance(content, list):  # multimodal parts
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    if _looks_like_code(part.get("text", "")):
                        return "passthrough"
    return "compress"
