"""P6-1/P6-2: grounded-answer detector and dose-tier selection.

SHARED module — imported by BOTH ``proxy/main.py`` (the live request
path) and ``benchmark/run_benchmark.py`` (the harness). C-4a rule
(empty-box precedent): import the production symbol, never re-implement
it — a patched detector here flips the gate's verdict everywhere on the
next request, which is exactly what makes the AC-P6c calibration gate
meaningful.

Design (product-spec-v2.md §P6, ratified by PM 2026-09-18):

- ``grounded_answer_risk(messages, config)`` is a PURE function of
  (messages, config) — no I/O, no process-global reads when a config is
  supplied, no mutation of its input. Same canonical prompt → same
  decision, always (AC-P1d cache-stability preserved, P6-5).

- Signals are CONTENT-SHAPE only, evaluated on the LAST user message:
  1. explicit source-context blocks ("Retrieved knowledge:", "Excerpt",
     "Source A/B", "Runbook excerpt:", "Log excerpt:", "Code context:",
     "Playbook:", quoted policy fragments) → ``fidelity_critical`` —
     the model is being asked to answer FROM quoted material, so the
     full "cut the padding" instruction can drop load-bearing facts;
  2. citation verbs ("per the", "according to", "what are our
     obligations under", "which source", ...) and answer-reference
     language ("based on the above", "correct the excerpt", ...) →
     ``bounded`` — the answer is expected to lean on provided material;
  3. nothing of the above → ``none``.

- A BARE citation verb or the word "policy" with NO source block and NO
  answer-reference language does NOT ground the request (AC-P6a
  negative case): "According to our refund policy, ..." is ordinary
  conversation unless the message actually references provided
  material.

- Dose tiers (P6-2) replace the single binary conciseness instruction
  with an ordered, config-declared set. The discriminator selects the
  MAX tier per request; the request path never injects above the
  selection:

      ungrounded                     -> full   (today's instruction, unchanged)
      grounded ``bounded``           -> bounded
      grounded ``fidelity_critical`` -> bounded  (only AFTER AC-P6c is green)
                                        none    (pre-calibration, default)

  ``grounded_calibration_green`` stays False until the P6-3 calibration
  artifact is committed and green; flipping it is a PM sign-off, not a
  code change.
"""
from __future__ import annotations

import re

from .config import Settings, get_settings

# Ordered tier set — index order IS the severity order (monotonic cap).
DOSE_TIERS: tuple[str, ...] = ("none", "bounded", "full")

RISK_NONE = "none"
RISK_BOUNDED = "bounded"
RISK_FIDELITY_CRITICAL = "fidelity_critical"

# --- Signal 1: explicit source-context blocks (last user message) ---
_SOURCE_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bretrieved knowledge\s*:",
        r"\bknowledge snippets?\s*:",
        r"\b(?:runbook|log|policy|documentation|reference) excerpt\b",
        r"\bexcerpt\s*:",
        r"\bcode context\s*:",
        r"\bsources?\s+[a-d1-9]\s*[:(]",
        r"\bplaybook\s*:",
        r"\bquoted (?:policy|guideline)s?\b",
    )
)

# Quoted policy fragments: a quoted span long enough to be quoted
# MATERIAL (not a scare-quoted word) that carries obligation/condition
# language. Keeps the detector content-shape-only and deterministic.
_QUOTED_SPAN = re.compile(r"[\"']([^\"']{40,})[\"']")
_QUOTED_POLICY_MARKERS = re.compile(
    r"\b(?:must|shall|required|may not|cannot|is not|are not|"
    r"only if|within \d+|no later than|not be (?:accepted|refunded|"
    r"re-shipped|permitted))\b",
    re.IGNORECASE,
)

# --- Signal 2a: citation verbs ---
_CITATION_VERB_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bper the\b",
        r"\baccording to\b",
        r"\bobligations under\b",
        r"\bwhich source\b",
        r"\bwhat does (?:the |that |your )?sources?\b",
        r"\bas stated in\b",
        r"\bas per\b",
        r"\bciting the\b",
    )
)

# --- Signal 2b: answer-reference language ---
_ANSWER_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bbased on the above\b",
        r"\bfrom the above\b",
        r"\babove excerpt\b",
        r"\bcorrect the excerpt\b",
        r"\bthe provided\b",
        r"\bprovided (?:documentation|context|sources?|policy|playbook|"
        r"knowledge|material|snippet)s?\b",
        r"\buse only the\b",
        r"\bcite the\b",
    )
)


def _last_user_text(messages: list[dict]) -> str | None:
    """Text of the LAST user message (str content or joined text parts)."""
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(parts)
        return None
    return None


def _has_quoted_policy_fragment(text: str) -> bool:
    return any(
        _QUOTED_POLICY_MARKERS.search(span) for span in _QUOTED_SPAN.findall(text)
    )


def grounded_answer_risk(
    messages: list[dict], config: Settings | None = None
) -> dict:
    """Classify grounded-answer risk. PURE function of (messages, config).

    Returns ``{"grounded": bool, "risk": "none"|"bounded"|"fidelity_critical"}``.
    Never mutates ``messages``; never touches process state when a
    ``config`` is supplied (the ``config`` parameter exists so tests and
    the harness can pin the decision independent of deployment settings;
    risk itself does not depend on any setting today).
    """
    del config  # risk is content-shape only; config kept for signature parity
    text = _last_user_text(messages)
    if text is None:
        return {"grounded": False, "risk": RISK_NONE}

    has_source_block = (
        any(p.search(text) for p in _SOURCE_BLOCK_PATTERNS)
        or _has_quoted_policy_fragment(text)
    )
    if has_source_block:
        return {"grounded": True, "risk": RISK_FIDELITY_CRITICAL}

    answer_reference = any(p.search(text) for p in _ANSWER_REFERENCE_PATTERNS)
    if answer_reference:
        # Citation verbs co-occurring with answer-reference language are
        # the same bounded case; a bare citation verb alone is NOT a
        # grounding signal (AC-P6a negative: "according to our policy").
        return {"grounded": True, "risk": RISK_BOUNDED}
    return {"grounded": False, "risk": RISK_NONE}


def select_dose_tier(
    messages: list[dict], config: Settings | None = None
) -> str:
    """Pick the dose tier (P6-2). PURE function of (messages, config).

    Returns one of :data:`DOSE_TIERS`. The request path must never
    inject an instruction ABOVE this selection (AC-P6b); it may always
    inject LESS (the length/overhead gate still applies underneath).
    """
    s = config if config is not None else get_settings()
    risk = grounded_answer_risk(messages, s)
    if not risk["grounded"]:
        return "full"
    if risk["risk"] == RISK_FIDELITY_CRITICAL:
        return "bounded" if s.grounded_calibration_green else "none"
    return "bounded"
