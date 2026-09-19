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
     like "POLICY:", "LEDGER DATA:", "REGULATION 4.2:", "RUNBOOK:",
     structured retrieval envelopes — a JSON object payload carrying a
     ``retrieved_documents`` block whose ``hits`` are ``chunk_id`` +
     ``source``-shaped retrieval results, bare or in a ```json fence —
     the canonical RAG-wrapper shape, AC-P6i; widened to common wrapper
     shapes by AC-P6j — the object parsed wherever it appears in the
     text (embedded in prose, question-suffixed), alternate container
     keys (documents/search_results/passages), LangChain
     context/page_content, id+document hits, one-level-nested
     envelopes, and XML <documents><document> blocks carrying a source)
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

import json
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

# AC-P6h audit feature: retain the per-message fact that a POLICY-labelled
# block was present. This is narrower than source-context: a retrieved-
# document envelope is source context but is not itself a POLICY block.
_POLICY_BLOCK = re.compile(
    r"\b(?:policy\s*:|policy excerpt\b|quoted (?:policy|guideline)s?\b)",
    re.IGNORECASE,
)

# --- Signal 1d: structured retrieval envelopes (AC-P6i canonical shape,
# --- widened to common wrapper shapes by AC-P6j, pre-publication gate) ----
# Canonical RAG-wrapper shape: a JSON object payload carrying a
# ``retrieved_documents`` block whose ``hits`` are retrieval results keyed
# by ``chunk_id`` + ``source``. Widened (AC-P6j, PM scope ruling): the
# envelope object parsed WHEREVER it appears in the text (embedded in
# prose, suffixed by a question, fenced), alternate envelope keys
# (``documents`` / ``search_results`` / ``passages``), LangChain-style
# ``context`` lists of ``page_content`` items, hits keyed ``id`` +
# ``document``, the envelope nested one level under a wrapper key, and
# XML ``<documents><document>`` blocks carrying a ``source`` (Anthropic's
# documented RAG style). Detection stays STRUCTURAL (parsed shapes, not
# regexes over prose): it cannot fire on ordinary prose or on JSON
# documents that merely mention these key names. A hit is "source-
# identifying" only when it carries a recognized key pair — a bare list
# of strings or unidentified dicts is NOT an envelope (false-positive
# control; the identifying-key set is the contract, never widened
# silently).

# Envelope container keys (AC-P6i canonical first) + the identifying
# hit-key contract. LangChain's ``context`` is handled separately (its
# items are ``page_content`` hits, not source-keyed hits).
_RETRIEVAL_CONTAINER_KEYS: tuple[str, ...] = (
    "retrieved_documents", "documents", "search_results", "passages",
)
_IDENTIFYING_HIT_KEY_PAIRS: tuple[tuple[str, str], ...] = (
    ("chunk_id", "source"),  # canonical (AC-P6i)
    ("id", "document"),      # wrapper variant (AC-P6j)
)
_IDENTIFYING_HIT_KEYS: tuple[str, ...] = ("page_content",)

# Cheap pre-filter: the scan only runs when the text could plausibly
# carry an envelope (container key name, page_content, or an XML
# documents block). Prose that merely mentions a key name pays one
# substring check, not the JSON scan.
_ENVELOPE_HINT = re.compile(
    r"retrieved_documents|documents|search_results|passages|"
    r"page_content|<document",
    re.IGNORECASE,
)

_XML_DOCUMENTS_BLOCK = re.compile(
    r"<documents\b[^>]*>([\s\S]*?)</documents\s*>", re.IGNORECASE
)
_XML_SOURCE = re.compile(
    r"<document\b[^>]*\ssource\s*=|<source\b[^>]*>[\s\S]*?</source\s*>",
    re.IGNORECASE,
)

# Bounds the embedded-object scan: at most 256 balanced-object parse
# attempts per message (deterministic, pure; messages carry a handful of
# JSON objects at most — the cap exists so pathological inputs degrade
# linearly, never hang).
_MAX_JSON_SCAN_ATTEMPTS = 256


def _hit_identifies_source(hit: object) -> bool:
    """True when ONE hit dict carries a recognized source-identifying
    key contract (key pair or LangChain ``page_content``)."""
    if not isinstance(hit, dict):
        return False
    if any(k in hit and hit[k] is not None for k in _IDENTIFYING_HIT_KEYS):
        return True
    return any(
        pair[0] in hit and pair[1] in hit for pair in _IDENTIFYING_HIT_KEY_PAIRS
    )


def _hits_identify_sources(hits: object) -> bool:
    return isinstance(hits, list) and any(
        _hit_identifies_source(hit) for hit in hits
    )


def _dict_is_retrieval_payload(obj: object) -> bool:
    """True when a decoded JSON object carries a retrieval envelope shape.

    Checks the object itself AND any direct child dict (one-level
    nesting under a wrapper key). A container key must map to a list of
    source-identifying hits (or a dict with such a ``hits`` list);
    LangChain ``context`` must be a list of ``page_content`` dicts.
    """
    if not isinstance(obj, dict):
        return False
    candidates = [obj] + [
        v for v in obj.values() if isinstance(v, dict)
    ]
    for cand in candidates:
        for key in _RETRIEVAL_CONTAINER_KEYS:
            val = cand.get(key)
            if _hits_identify_sources(val):
                return True
            if isinstance(val, dict) and _hits_identify_sources(val.get("hits")):
                return True
        context = cand.get("context")
        if isinstance(context, list) and context and any(
            isinstance(hit, dict) and "page_content" in hit for hit in context
        ):
            return True
    return False


def _iter_embedded_json_objects(text: str):
    """Yield every balanced JSON object parseable anywhere in ``text``.

    Bounded scan: at most ``_MAX_JSON_SCAN_ATTEMPTS`` parse attempts,
    left to right (deterministic). Pure; raises nothing.
    """
    decoder = json.JSONDecoder()
    idx = text.find("{")
    attempts = 0
    while idx != -1 and attempts < _MAX_JSON_SCAN_ATTEMPTS:
        attempts += 1
        try:
            obj, _end = decoder.raw_decode(text, idx)
        except (ValueError, RecursionError):
            obj = None
        if isinstance(obj, dict):
            yield obj
        idx = text.find("{", idx + 1)


def _is_retrieval_envelope(text: str) -> bool:
    """True when ``text`` carries a structured retrieval envelope.

    Pure, deterministic, no I/O (AC-P6i canonical + AC-P6j widened
    shapes). Covers: the whole message being the JSON object, a
    ```json-fenced block, the object embedded in prose or followed by a
    question in the same message (balanced-object scan at any offset),
    alternate container keys, LangChain ``context``/``page_content``,
    ``id``+``document`` hits, one-level-nested envelopes, and XML
    ``<documents>`` blocks carrying a ``source``.
    """
    if not text:
        return False
    if not _ENVELOPE_HINT.search(text):
        return False
    # Fast path: the whole (stripped) message is the JSON object — the
    # canonical bare shape and the common case for fixture-scale texts.
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except (ValueError, RecursionError):
            obj = None
        if _dict_is_retrieval_payload(obj):
            return True
    # Widened path: the object parsed wherever it appears in the text.
    if any(
        _dict_is_retrieval_payload(obj)
        for obj in _iter_embedded_json_objects(text)
    ):
        return True
    # XML retrieval blocks (Anthropic's documented RAG style): a
    # <documents> block must contain at least one <document> element AND
    # a source (attribute or <source> element) — a block without source
    # is not an envelope.
    for block in _XML_DOCUMENTS_BLOCK.findall(text):
        if _XML_SOURCE.search(block) and re.search(
            r"<document\b", block, re.IGNORECASE
        ):
            return True
    return False


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


def _source_block_present(text: str) -> bool:
    """Whether one message carries a detector source-context signal."""
    return (
        any(pattern.search(text) for pattern in _SOURCE_BLOCK_PATTERNS)
        or _has_quoted_policy_fragment(text)
        or bool(_LABELED_BLOCK.search(text))
        or _is_retrieval_envelope(text)
    )


def grounding_features(messages: list[dict]) -> list[dict]:
    """Return raw, per-message AC-P6h evidence for a calibration artifact.

    Role/text is retained with source/POLICY-block facts so clean post-fix
    detector code can re-derive historical labels. Every message is emitted
    for audit completeness; the detector itself continues to read only
    system/user turns. Typed content follows the detector's text-part rule.
    """
    rows: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = ""
        rows.append({
            "role": message.get("role"),
            "text": text,
            "source_block_present": _source_block_present(text),
            "policy_block_present": bool(_POLICY_BLOCK.search(text)),
        })
    return rows


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

    has_source_block = any(_source_block_present(text) for text in texts)
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


def envelope_shape_present(messages: list[dict]) -> bool:
    """AC-P6f live observation channel: does ANY system/user text carry
    retrieval-envelope structure per the AC-P6j shape scanner?

    PURE function of ``messages``. Production records this flag on every
    ledger row INDEPENDENTLY of the tier decision — the tripwire then
    catches requests classified tier ``full`` (or never classified at all:
    conciseness off, passthrough route) whose content nevertheless carries
    envelope structure, from the moment P6 monitoring exists, without
    re-running or weakening the discriminator (C-4a: this imports the
    production scanner symbol; no duplicate detection logic).
    """
    texts = _user_and_system_texts(messages)
    return any(_is_retrieval_envelope(t) for t in texts)


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
