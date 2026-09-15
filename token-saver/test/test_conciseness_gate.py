"""P1-1 category/length-aware conciseness gate (AC-P1 fix).

Evidence base: benchmark run 20260915T040955Z showed short prompts
(especially short factual QA) get LONGER with the instruction attached,
while long RAG/code prompts save. The gate restricts injection to prompts
where the user's request is long enough for output savings to dominate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.counting import should_inject_conciseness  # noqa: E402


# The benchmark's worst offender: short factual QA (qa-014: 42 -> 93 tokens)
SHORT_QA = [{"role": "user", "content": "How many bones are in the adult human body?"}]

# A long RAG-style request (the kind that saved in the benchmark)
LONG_RAG = [{"role": "user", "content": (
    "Here is my situation in detail. I purchased a jacket from your store two "
    "weeks ago during a summer sale event, and I would like to return it because "
    "the size does not fit me properly. The jacket has only been tried on once at "
    "home over a t-shirt, all the original tags are still attached, and I kept "
    "the paper receipt from the purchase along with the original packaging it came "
    "in. Could you walk me through whether this return is possible, which conditions "
    "apply to sale items, and what the expected refund timeline looks like?"
)}]

# A long factual question ending in '?' — long enough that output dominates
LONG_QUESTION = [{"role": "user", "content": (
    "Walk me through the complete history of the Berlin Wall from its construction "
    "in 1961 through its fall in 1989, including the political events that led up "
    "to each major phase, the role of East Germany's leadership decisions, the "
    "influence of Soviet policy changes under Gorbachev, the escape attempts and "
    "casualties at the wall itself, and the immediate consequences for German "
    "reunification that followed within the next year after the wall opened, "
    "including how the two Germanys negotiated the economic and political union "
    "that the rest of Europe had to accept in short order?"
)}]


def test_short_factual_qa_excluded():
    """qa-014 style prompts were net-negative (-121%); must be gated off."""
    assert should_inject_conciseness(SHORT_QA) is False


def test_long_rag_request_included():
    assert should_inject_conciseness(LONG_RAG) is True


def test_long_question_still_included():
    """Long prompts ending in '?' keep the instruction — length, not '?',
    is the discriminator."""
    assert should_inject_conciseness(LONG_QUESTION) is True


def test_empty_messages_excluded():
    assert should_inject_conciseness([]) is False


def test_no_user_message_excluded():
    assert should_inject_conciseness([{"role": "system", "content": "x" * 500}]) is False


def test_boundary_at_min_chars():
    n = 400
    at = [{"role": "user", "content": "a" * n + " please explain in detail. " * 1}]
    below = [{"role": "user", "content": "a" * (n - 1)}]
    assert should_inject_conciseness(at) is True
    assert should_inject_conciseness(below) is False


def test_multimodal_user_content_excluded_safely():
    """Non-string content returns False rather than crashing."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "a" * 500}]}]
    assert should_inject_conciseness(msgs) is False
