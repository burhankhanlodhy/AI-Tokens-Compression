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

- Signals are CONTENT-SHAPE only, evaluated over the SYSTEM messages and
EVERY user message (P6-4 scope fix: grounding frequently lives in the
system prompt — "Use only the provided policy text. POLICY: ..." — and
scanning only the last user turn left prod-shaped requests ungrounded,
i.e. maximum dose with no guard):
  1. explicit source-context blocks ("Retrieved knowledge:", "Excerpt",
     "Source A/B", "Runbook excerpt:", "Log excerpt:", "Code context:",
     "Playbook:", quoted policy fragments, uppercase labeled data blocks
     like "POLICY:", "LEDGER DATA:", "REGULATION 4.2:", "RUNBOOK:")
     → ``fidelity_critical`` — the model is being asked to answer FROM
     quoted material, so the full "cut the padding" instruction can drop
     load-bearing facts;
  2. citation verbs ("per the", "according to", "what are our
     obligations under", "which source", ...) and answer-reference
     language ("based on the above", "use only this schedule",
     "correct the excerpt", ...) → ``bounded`` — the answer is expected
     to lean on provided material;
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
        r"\buse only th(?:is|e|ese)\b",
        r"\bcite the\b",
    )
)


# --- Signal 1c: uppercase labeled data blocks (P6-4 signal-set fix) ---
# "POLICY:", "LEDGER DATA:", "REGULATION 4.2:", "CLAUSE 7 (TERMINATION):",
# "RATES (30yr fixed):", "RUNBOOK:" — a sentence- or line-initial
# ALL-CAPS label (optionally with a parenthetical qualifier) followed by
# a colon is how pasted reference material is introduced. The label core
# must be at least two characters of [A-Z0-9] so ordinary prose
# ("Points:", "Q:") never matches, and a bare capitalized word does not.
_LABELED_BLOCK = re.compile(
    r"(?:^|[\n.])\s*[A-Z][A-Z0-9.\-]*[A-Z0-9](?:\s+[A-Z0-9.\-]+)*"
    r"\s*(?:\([^)]{0,40}\))?:\s"
)


def _last_user_text(messages: list[dict]) -> str | None:
    """Text of the LAST user message (str content or joined text parts)."""
    texts = _user_and_system_texts(messages, roles=("user",))
    return texts[-1] if texts else None


def _user_and_system_texts(
    messages: list[dict], roles: tuple[str, ...] = ("system", "user")
) -> list[str]:
    """Texts of the messages whose role is in ``roles``, in order.

    Handles both content shapes: a plain string (OpenAI ``role: system`` /
    ``role: user``) and a list of typed parts (Anthropic-style content
    blocks — text parts joined, non-text parts ignored).
    """
    texts: list[str] = []
    for msg in messages or []:
        if msg.get("role") not in roles:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            texts.append("\n".join(parts))
    return texts


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
    # P6-4 scope fix: grounding lives in system messages as often as in
    # the user turn ("Use only the provided policy text. POLICY: ...").
    # Scan system + every user message; a source block ANYWHERE grounds
    # the request at fidelity_critical.
    texts = _user_and_system_texts(messages)
    if not texts:
        return {"grounded": False, "risk": RISK_NONE}

    has_source_block = any(
        p.search(t) for t in texts for p in _SOURCE_BLOCK_PATTERNS
    ) or any(_has_quoted_policy_fragment(t) for t in texts) or any(
        _LABELED_BLOCK.search(t) for t in texts
    )
    if has_source_block:
        return {"grounded": True, "risk": RISK_FIDELITY_CRITICAL}

    answer_reference = any(
        p.search(t) for t in texts for p in _ANSWER_REFERENCE_PATTERNS
    )
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
