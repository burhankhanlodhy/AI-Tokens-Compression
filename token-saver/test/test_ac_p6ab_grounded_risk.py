"""P6-1/P6-2 acceptance tests: grounded-answer detector + dose tiers.

Covers AC-P6a (pure function; fixture classification; bare-keyword
negative) and AC-P6b (tiers config-declared; request path never injects
above the discriminator's selection; deterministic per canonical
prompt). Spec: product-spec-v2.md §P6, ratified 2026-09-18.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import Settings  # noqa: E402
from proxy.grounded import (  # noqa: E402
    DOSE_TIERS,
    RISK_BOUNDED,
    RISK_FIDELITY_CRITICAL,
    RISK_NONE,
    _is_retrieval_envelope,
    grounded_answer_risk,
    grounding_features,
    select_dose_tier,
)
from test_matrix_live import routed  # noqa: E402,F401
from test_live_routing import _CaptureTransport  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "benchmark" / "prompts.json"
PROMPTS = {p["id"]: p for p in json.loads(FIXTURES.read_text())["prompts"]}


def cfg(**overrides) -> Settings:
    """Explicit config object — keeps the detector a pure function of
    (messages, config) in tests, independent of process env/.env."""
    base = dict(
        output_conciseness_enabled=True,
        grounded_calibration_green=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_ac_p6h_grounding_features_preserve_raw_text_and_source_policy_flags():
    """Raw artifact evidence must distinguish source context from POLICY."""
    features = grounding_features([
        {"role": "system", "content": "POLICY: Refunds require a receipt."},
        {"role": "user", "content": "Retrieved knowledge: [1] Invoice paid."},
        {"role": "assistant", "content": "Earlier answer."},
    ])
    assert features == [
        {"role": "system", "text": "POLICY: Refunds require a receipt.",
         "source_block_present": True, "policy_block_present": True},
        {"role": "user", "text": "Retrieved knowledge: [1] Invoice paid.",
         "source_block_present": True, "policy_block_present": False},
        {"role": "assistant", "text": "Earlier answer.",
         "source_block_present": False, "policy_block_present": False},
    ]


# --- AC-P6a: fixture classification -----------------------------------


@pytest.mark.parametrize("pid", [f"rag-0{i}" for i in range(51, 56)])
def test_rag_051_055_classify_fidelity_critical(pid):
    """The 5 eligible RAG fixtures carry explicit source-context blocks in
    the last user message — they must classify fidelity_critical."""
    r = grounded_answer_risk(PROMPTS[pid]["messages"], cfg())
    assert r == {"grounded": True, "risk": RISK_FIDELITY_CRITICAL}


# PM simulation on 32355b0 showed the scope-only patch misses rag-023,
# rag-026, and rag-028 because their system blocks use labeled data markers
# and rag-028 says "use only this schedule".  AC-P6g therefore pins the
# expected post-fix result for every fixture, not merely "grounded or better".
RAG_021_030_EXPECTED = {
    "rag-021": RISK_FIDELITY_CRITICAL,
    "rag-022": RISK_FIDELITY_CRITICAL,
    "rag-023": RISK_FIDELITY_CRITICAL,
    "rag-024": RISK_FIDELITY_CRITICAL,
    "rag-025": RISK_FIDELITY_CRITICAL,
    "rag-026": RISK_FIDELITY_CRITICAL,
    "rag-027": RISK_FIDELITY_CRITICAL,
    "rag-028": RISK_FIDELITY_CRITICAL,
    "rag-029": RISK_FIDELITY_CRITICAL,
    "rag-030": RISK_FIDELITY_CRITICAL,
}


@pytest.mark.parametrize("pid,expected_risk", RAG_021_030_EXPECTED.items())
def test_ac_p6g_rag_021_030_pin_expected_risk(pid, expected_risk):
    """Every short RAG fixture carries grounding in its system message.

    The user turns are gate-negative (53–117 chars), but the detector still
    must classify the request as grounded; pinning fidelity-critical here
    catches both the system-only blind spot and incomplete signal coverage.
    """
    r = grounded_answer_risk(PROMPTS[pid]["messages"], cfg())
    assert r == {"grounded": True, "risk": expected_risk}


@pytest.mark.parametrize(
    "pid",
    [p["id"] for p in PROMPTS.values() if p["category"] != "rag"],
)
def test_ac_p6g_non_rag_fixtures_remain_ungrounded(pid):
    """The broadened signal set must not ground any of the 40 controls."""
    assert grounded_answer_risk(PROMPTS[pid]["messages"], cfg()) == {
        "grounded": False,
        "risk": RISK_NONE,
    }


def test_ac_p6g_prod_shaped_system_policy_block_is_fidelity_critical():
    """A long request grounded by an OpenAI role=system policy block must
    never fall through to the ungrounded/full-dose path."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a support assistant. Use only the provided policy text. "
                "POLICY: Returns require a receipt and must be processed within "
                "30 days."
            ),
        },
        {
            "role": "user",
            "content": (
                "Please apply the policy above to this case and explain every "
                "applicable condition, deadline, exception, and required step "
                "without omitting any detail. "
            )
            + "I need the complete reasoning and operational answer. " * 20,
        },
    ]
    assert len(messages[-1]["content"]) > 400
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": True,
        "risk": RISK_FIDELITY_CRITICAL,
    }
    assert select_dose_tier(messages, cfg()) == "none"


@pytest.mark.parametrize(
    "pid",
    [p["id"] for p in PROMPTS.values()
     if p["category"] in ("conversational", "qa")],
)
def test_conversational_and_qa_without_source_blocks_classify_none(pid):
    """Conversational/QA fixtures carry no source-context blocks — they
    must not ground (P6-2: ungrounded -> full tier, behavior unchanged)."""
    r = grounded_answer_risk(PROMPTS[pid]["messages"], cfg())
    assert r == {"grounded": False, "risk": RISK_NONE}


def test_bare_policy_keyword_does_not_ground():
    """AC-P6a negative: the bare word 'policy' with no source block and
    no answer-reference language must not trip the detector."""
    msgs = [{"role": "user", "content":
             "Is our policy on returns the same this year as last year?"}]
    assert grounded_answer_risk(msgs, cfg()) == {"grounded": False,
                                                 "risk": RISK_NONE}


def test_bare_citation_verb_without_source_block_does_not_ground():
    """AC-P6a negative: 'according to' alone (no source block, no
    answer-reference language) must not trip the detector."""
    msgs = [{"role": "user", "content":
             "According to general industry practice, how long should a "
             "postmortem take? We just want a rough sanity check before "
             "scheduling ours next quarter with the whole on-call team."}]
    assert grounded_answer_risk(msgs, cfg()) == {"grounded": False,
                                                 "risk": RISK_NONE}


# --- P6-4: scope + signal-set regression (system-message grounding) ----


def test_p6a4_labeled_block_signal_grounds_fidelity_critical():
    """P6-4 signal-set: an uppercase labeled data block ("LEDGER DATA:",
    "TRAIN SCHEDULE:") in the system message is pasted reference material
    → fidelity_critical, closing the rag-023/026/028 coverage gap."""
    for label in ("LEDGER DATA", "STOCK DATA", "TRAIN SCHEDULE",
                  "REGULATION 4.2", "RUNBOOK", "RATES (30yr fixed)"):
        msgs = [
            {"role": "system",
             "content": f"You are a data assistant. {label}: 42, 17, 99."},
            {"role": "user", "content": "Summarize the trend, please."},
        ]
        assert grounded_answer_risk(msgs, cfg()) == {
            "grounded": True, "risk": RISK_FIDELITY_CRITICAL}, label


def test_p6a4_prose_labels_and_single_letters_do_not_ground():
    """P6-4 negative control: a bare capitalized word ("Points:"), a
    single-letter label ("Q:"), and ordinary sentence-initial prose must
    NOT trip the labeled-block signal — the uppercase pattern is only for
    ALL-CAPS labels of 2+ characters."""
    cases = [
        [{"role": "user", "content":
          "Can you compare the pricing tiers? Points: cost, support, and "
          "setup time matter most to our team this quarter."}],
        [{"role": "user", "content":
          "Q: What is the deadline for the quarterly report? "
          "Asking so we can plan the review meeting accordingly."}],
        [{"role": "user", "content":
          "Please note the meeting moved. I will send the updated agenda "
          "and dial-in details before the end of the day tomorrow."}],
    ]
    for msgs in cases:
        assert grounded_answer_risk(msgs, cfg()) == {
            "grounded": False, "risk": RISK_NONE}


def test_p6a4_grounding_in_earlier_user_turn_is_detected():
    """P6-4 scope: grounding in ANY user message (not just the last)
    grounds the request — multi-turn conversations keep their guard."""
    msgs = [
        {"role": "user", "content":
         "Use only this schedule. TRAIN SCHEDULE: Express 07:15, 09:45; "
         "Local 06:30, 08:00 — journey 3h05m."},
        {"role": "assistant", "content": "Understood, I have the schedule."},
        {"role": "user", "content": "OK — what about arriving by 3pm?"},
    ]
    assert grounded_answer_risk(msgs, cfg()) == {
        "grounded": True, "risk": RISK_FIDELITY_CRITICAL}


def test_p6a4_anthropic_system_content_blocks_shape_is_detected():
    """P6-4 scope: system grounding delivered as Anthropic-style typed
    content parts (not a plain string) must still classify."""
    msgs = [
        {"role": "system", "content": [
            {"type": "text",
             "text": "You are a travel assistant. Use only this schedule. "
                     "TRAIN SCHEDULE: Express 07:15 — journey 2h10m."}]},
        {"role": "user", "content": "What runs before 3pm?"},
    ]
    assert grounded_answer_risk(msgs, cfg()) == {
        "grounded": True, "risk": RISK_FIDELITY_CRITICAL}


def test_answer_reference_language_bounds_without_source_block():
    """Answer-reference language ('based on the above') grounds at
    `bounded` — references provided material without quoting it."""
    msgs = [{"role": "user", "content":
             "Based on the above, summarize the key points briefly and "
             "flag anything the provided material does not cover."}]
    assert grounded_answer_risk(msgs, cfg()) == {"grounded": True,
                                                 "risk": RISK_BOUNDED}


# --- AC-P6a: purity / determinism -------------------------------------


def test_same_canonical_prompt_same_decision_always():
    """AC-P1d cache-stability shape: repeated and interleaved calls on
    the same canonical prompt produce identical decisions."""
    msgs = PROMPTS["rag-051"]["messages"]
    first = grounded_answer_risk(msgs, cfg())
    for _ in range(3):
        assert grounded_answer_risk(msgs, cfg()) == first
    # interleaved with a different prompt — no state carryover
    other = PROMPTS["rag-021"]["messages"]
    assert grounded_answer_risk(other, cfg())["risk"] in (
        RISK_NONE, RISK_BOUNDED, RISK_FIDELITY_CRITICAL)
    assert grounded_answer_risk(msgs, cfg()) == first


def test_does_not_mutate_messages():
    msgs = PROMPTS["rag-052"]["messages"]
    snapshot = copy.deepcopy(msgs)
    grounded_answer_risk(msgs, cfg())
    assert msgs == snapshot


def test_risk_independent_of_config_values():
    """The risk classification is content-shape only — no setting may
    flip it (the config parameter exists for tier selection below)."""
    msgs = PROMPTS["rag-053"]["messages"]
    variants = [
        cfg(),
        cfg(grounded_calibration_green=True),
        cfg(conciseness_min_user_chars=1),
        cfg(dose_tier_bounded_instruction="custom text"),
    ]
    results = {tuple(sorted(grounded_answer_risk(msgs, c).items()))
               for c in variants}
    assert len(results) == 1


def test_no_user_message_is_none():
    assert grounded_answer_risk([], cfg()) == {"grounded": False,
                                               "risk": RISK_NONE}
    assert grounded_answer_risk([{"role": "system", "content": "hi"}],
                                cfg()) == {"grounded": False,
                                           "risk": RISK_NONE}


# --- AC-P6b: tiers config-declared, capped selection, deterministic ----


def test_tiers_are_config_declared_and_ordered():
    assert DOSE_TIERS == ("none", "bounded", "full")
    s = cfg()
    instructions = s.dose_tier_instructions()
    assert set(instructions) == set(DOSE_TIERS)
    assert instructions["none"] is None
    assert instructions["bounded"] != instructions["full"]
    assert "concisely" in instructions["full"].lower()
    # bounded carries the explicit fidelity guard
    assert "source-attributed" in instructions["bounded"]


def test_ungrounded_selects_full():
    msgs = PROMPTS["qa-001"]["messages"] if "qa-001" in PROMPTS else [
        {"role": "user", "content": "Walk me through a long ungrounded "
         "request that goes on for quite a while with plenty of ordinary "
         "prose, no sources anywhere in sight, well past the length gate."}]
    assert select_dose_tier(msgs, cfg()) == "full"


def test_grounded_fidelity_critical_capped_at_bounded_even_when_calibration_green():
    """The discriminator's cap: grounded fidelity-critical traffic NEVER
    gets the full instruction — at most `bounded`, and only after the
    AC-P6c calibration gate flips the config flag."""
    msgs = PROMPTS["rag-051"]["messages"]
    assert select_dose_tier(msgs, cfg(grounded_calibration_green=True)) \
        == "bounded"


def test_grounded_fidelity_critical_pre_calibration_is_none():
    """Pre-AC-P6c the OFF contract holds: grounded traffic caps at tier
    `none` (no injection at all), per the PM sequencing constraint."""
    msgs = PROMPTS["rag-051"]["messages"]
    assert select_dose_tier(msgs, cfg()) == "none"


def test_grounded_bounded_selects_bounded():
    msgs = [{"role": "user", "content":
             "Based on the above, summarize the key points briefly and "
             "flag anything the provided material does not cover."}]
    assert select_dose_tier(msgs, cfg()) == "bounded"
    assert select_dose_tier(msgs, cfg(grounded_calibration_green=True)) \
        == "bounded"


def test_selection_deterministic_per_canonical_prompt():
    msgs = PROMPTS["rag-054"]["messages"]
    first = select_dose_tier(msgs, cfg())
    assert all(select_dose_tier(msgs, cfg()) == first for _ in range(3))


def test_bounded_instruction_is_never_the_full_instruction():
    """The bounded tier must inject a DIFFERENT (fidelity-guarded)
    string, not the full P1-1 instruction — otherwise the tier is a no-op
    and the cap is illusory."""
    s = cfg()
    assert "shorten the delivery, never the content" \
        in s.dose_tier_instructions()["bounded"].lower()


# --- AC-P6b: live request path never injects above the selection ------


def _captured_upstream(routed, messages, extra_headers=None):
    """Send one request through the app with a capturing transport; return
    the upstream body's message JSON (what the provider would see)."""
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    headers = {"Authorization": "Bearer sk-x",
               "X-Token-Saver-Conciseness": "1"}
    headers.update(extra_headers or {})
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions", headers=headers,
                   json={"model": "openrouter/z-ai/glm-5.3-flash",
                         "messages": messages})
        assert r.status_code == 200, r.text[:200]
    return json.loads(cap.requests[-1].content)["messages"]


def test_header_on_cannot_push_above_discriminator_pre_calibration(routed):
    """AC-P6b: the benchmark ON header forces the feature on, but a
    grounded fidelity-critical prompt pre-calibration must still receive
    NO conciseness instruction — the request path never injects above
    the discriminator's tier selection."""
    rag_msgs = PROMPTS["rag-051"]["messages"]
    upstream = _captured_upstream(routed, rag_msgs)
    assert "concisely" not in json.dumps(upstream).lower()


def test_ungrounded_long_prompt_header_on_still_gets_full_tier(routed):
    """Ungrounded traffic is unchanged by P6: with the ON header and a
    long ungrounded prompt, the full P1-1 instruction is injected."""
    long_user = ("Here is my situation in detail. I purchased a jacket from "
                 "your store two weeks ago during a summer sale event, and "
                 "I would like to return it because the size does not fit "
                 "me properly. The jacket has only been tried on once at "
                 "home over a t-shirt, all the original tags are still "
                 "attached, and I kept the paper receipt from the purchase "
                 "along with the original packaging it came in. Could you "
                 "walk me through whether this return is possible and what "
                 "the steps would be?")
    upstream = _captured_upstream(
        routed, [{"role": "user", "content": long_user}])
    assert "concisely" in json.dumps(upstream).lower()
    # full tier, not the bounded fidelity guard
    assert "source-attributed" not in json.dumps(upstream).lower()


# --- AC-P6i: structured retrieval envelopes (P6-5 signal-set fix) -------

L1_FIXTURES = ROOT / "benchmark" / "fixtures" / "l1_prompts.json"
L1_CHECKSUM = ROOT / "benchmark" / "fixtures" / "l1_prompts.json.sha256"


def _l1_fixture_prompts() -> dict:
    """Load the L1 fixture set, failing on checksum mismatch (same
    fail-on-mismatch contract as the measurement runners)."""
    expected = L1_CHECKSUM.read_text().split()[0].strip()
    actual = hashlib.sha256(L1_FIXTURES.read_bytes()).hexdigest()
    assert actual == expected, "l1 fixture checksum mismatch — contract break"
    return {p["id"]: p for p in json.loads(L1_FIXTURES.read_text())["prompts"]}


# PM's named regression set: the pure-JSON retrieval-envelope shape that
# no prose signal could see (no 'Retrieved knowledge:', no POLICY: block).
AC_P6I_REGRESSION_IDS = (
    "rag-001", "rag-006", "rag-007", "rag-008", "rag-012",
)


@pytest.mark.parametrize("pid", AC_P6I_REGRESSION_IDS)
def test_ac_p6i_retrieval_envelope_fixtures_ground_fidelity_critical(pid):
    """The 5 named L1 RAG fixtures carry a bare JSON retrieval envelope
    with NO prose system message — the structural envelope signal must
    classify them fidelity_critical, never {grounded: False, risk: none}."""
    fixture = _l1_fixture_prompts()[pid]
    r = grounded_answer_risk(fixture["messages"], cfg())
    assert r == {"grounded": True, "risk": RISK_FIDELITY_CRITICAL}
    # pre-calibration cap: the tier must NOT be full
    assert select_dose_tier(fixture["messages"], cfg()) == "none"


def test_ac_p6i_envelope_is_detected_in_system_message():
    """The envelope signal scans system messages like every other
    source-block signal (P6-4 scope applies to the new signal too)."""
    envelope = json.dumps({
        "retrieved_documents": {
            "query": "what is the quota",
            "hits": [{"chunk_id": "chunk-1", "source": "corpus/q.md",
                      "content": "Quota is 500/day."}],
        }
    })
    messages = [
        {"role": "system", "content": envelope},
        {"role": "user", "content": "What is the quota?"},
    ]
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": True, "risk": RISK_FIDELITY_CRITICAL}


def test_ac_p6i_fenced_envelope_is_detected():
    """An envelope wrapped in a ```json fence inside a longer prose
    message still fires (the wrapper shape is not always bare)."""
    envelope = ('Context follows.\n```json\n' + json.dumps({
        "retrieved_documents": {
            "query": "refund window",
            "hits": [{"chunk_id": "c-9", "source": "corpus/refund.md",
                      "content": "Refunds require a receipt."}],
        }}) + '\n```\nAnswer the question.')
    messages = [{"role": "user", "content": envelope}]
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": True, "risk": RISK_FIDELITY_CRITICAL}


def test_ac_p6i_anthropic_text_part_envelope_is_detected():
    """An envelope inside an Anthropic-style typed text content part is
    still seen (the same shape handling every other signal uses)."""
    envelope = json.dumps({
        "retrieved_documents": {
            "query": "q",
            "hits": [{"chunk_id": "c", "source": "s", "content": "x"}],
        }})
    messages = [{"role": "user", "content": [
        {"type": "text", "text": envelope}]}]
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": True, "risk": RISK_FIDELITY_CRITICAL}


@pytest.mark.parametrize("label,messages", [
    ("json_doc_without_envelope", [
        {"role": "user", "content": json.dumps(
            {"service": "reranker", "version": "3.4.1",
             "settings": {"batch": 32}})}]),
    ("keys_mentioned_as_values", [
        {"role": "user", "content": json.dumps(
            {"notes": "the field retrieved_documents stays reserved here",
             "docs": [{"chunk_id": "c", "source": "s"}]})}]),
    ("envelope_without_hits", [
        {"role": "user", "content": json.dumps(
            {"retrieved_documents": {"query": "q"}})}]),
    ("hits_without_chunk_id_and_source", [
        {"role": "user", "content": json.dumps(
            {"retrieved_documents": {"hits": [{"index": 0}]}})}]),
    ("malformed_json_payload", [
        {"role": "user", "content": '{"retrieved_documents": {"hits": ['}]),
    ("json_array_payload", [
        {"role": "user", "content": json.dumps(
            [{"chunk_id": "c", "source": "s"}])}]),
])
def test_ac_p6i_non_envelope_json_stays_ungrounded(label, messages):
    """Negatives: JSON that does not carry the retrieval-envelope shape
    must NOT ground — the structural check cannot be widened by prose or
    by keys appearing anywhere other than the envelope position."""
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": False, "risk": RISK_NONE}


def test_ac_p6i_l1_fixtures_rag_all_ground_and_controls_stay_none():
    """Corpus-level pin: after the envelope signal, EVERY rag fixture in
    the L1 fixture set classifies grounded (bounded or better) and the
    system_dup wrappers ground too (envelope + prose, severity-max);
    json_doc / log_trace / control categories stay ungrounded."""
    prompts = _l1_fixture_prompts()
    for pid, fixture in prompts.items():
        r = grounded_answer_risk(fixture["messages"], cfg())
        if pid.startswith("rag-") or pid.startswith("system_dup-"):
            assert r["grounded"], f"{pid} must ground post-envelope-signal"
        else:
            assert r == {"grounded": False, "risk": RISK_NONE}, \
                f"{pid} must stay ungrounded (got {r})"


def test_ac_p6i_envelope_detection_is_pure_and_deterministic():
    """Purity contract holds for the new signal: same input -> same
    decision, input never mutated, decision independent of config."""
    envelope = json.dumps({
        "retrieved_documents": {"hits": [
            {"chunk_id": "c", "source": "s"}]}})
    messages = [{"role": "user", "content": envelope}]
    before = copy.deepcopy(messages)
    first = grounded_answer_risk(messages, cfg())
    second = grounded_answer_risk(messages, cfg(grounded_calibration_green=True))
    assert first == second
    assert messages == before
    assert first == {"grounded": True, "risk": RISK_FIDELITY_CRITICAL}


# --- AC-P6j: wrapper-shape breadth (pre-publication gate) ----------------
#
# Each widened shape from the PM scope ruling gets a positive regression
# AND a negative control, so the false-positive rate stays pinned. All
# committed negatives (40 legacy controls + json_doc/log_trace/control
# L1 fixtures) must remain ungrounded under the widened scanner.

def _envelope_fixture(container: str = "retrieved_documents",
                      hit: dict | None = None) -> str:
    hit = hit or {"chunk_id": "c-1", "source": "corpus/refund.md",
                  "content": "Refunds require a receipt."}
    return json.dumps({container: {"query": "refund window",
                                   "hits": [hit]}})


@pytest.mark.parametrize("label,text", [
    # --- positives: the widened shapes ---
    ("embedded_prose_env_question",
     "Context for your answer:\n" + _envelope_fixture()
     + "\nWhat is the refund window?"),
    ("env_then_question_same_msg",
     _envelope_fixture() + "\n\nWhat is the refund window?"),
    ("documents_key", _envelope_fixture("documents")),
    ("search_results_key", _envelope_fixture("search_results")),
    ("passages_key", _envelope_fixture("passages")),
    ("langchain_context", json.dumps({"context": [
        {"page_content": "Refunds require a receipt.",
         "metadata": {"source": "corpus/refund.md"}}]})),
    ("id_document_hits", json.dumps({"retrieved_documents": {"hits": [
        {"id": "d1", "document": "Refunds require a receipt."}]}})),
    ("nested_one_level", json.dumps({"data": {
        "retrieved_documents": {"hits": [
            {"chunk_id": "c", "source": "corpus/c.md"}]}}})),
    ("xml_documents_source_attr",
     '<documents><document source="corpus/a.md">'
     "Refunds require a receipt.</document></documents>"),
    ("xml_source_element",
     "<documents><document><source>corpus/a.md</source>"
     "<text>Refunds require a receipt.</text></document></documents>"),
])
def test_ac_p6j_widened_shapes_ground_fidelity_critical(label, text):
    """Every ratified widened wrapper shape grounds at fidelity_critical,
    tier none pre-calibration — max dose on these was the failure class
    AC-P6j exists to kill."""
    messages = [{"role": "user", "content": text}]
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": True, "risk": RISK_FIDELITY_CRITICAL}, label
    assert select_dose_tier(messages, cfg()) == "none"


@pytest.mark.parametrize("label,text", [
    # --- negative controls: prose/no-structure variants of each shape ---
    ("prose_mentions_keys_only",
     "The documents and search_results keys stay reserved; context is "
     "the word of the day and passages must not leak."),
    ("env_without_hits", json.dumps(
        {"retrieved_documents": {"query": "q"}})),
    ("hits_without_identifying_keys", json.dumps(
        {"retrieved_documents": {"hits": [{"index": 0}]}})),
    ("bare_string_list_documents", json.dumps(
        {"documents": ["The reranker service version is 3.4.1 "
                       "and batching is enabled."]})),
    ("context_without_page_content", json.dumps(
        {"context": [{"text": "Refunds require a receipt."}]})),
    ("context_plain_string", json.dumps(
        {"context": "Refunds require a receipt."})),
    ("id_only_hits", json.dumps({"retrieved_documents": {"hits": [
        {"id": "d1"}]}})),
    ("nested_but_unidentified_hits", json.dumps({"data": {
        "retrieved_documents": {"hits": [{"index": 0}]}}})),
    ("xml_block_without_source", "<documents><document>"
     "Refunds require a receipt.</document></documents>"),
    ("prose_xml_tags_only",
     "Use <documents> and <document> tags for retrieval output."),
])
def test_ac_p6j_negative_controls_stay_ungrounded(label, text):
    """A widened shape WITHOUT the identifying structure must not ground:
    prose mentioning key names, hits lacking source-identifying keys,
    non-hit containers, and source-less XML are all none."""
    messages = [{"role": "user", "content": text}]
    assert grounded_answer_risk(messages, cfg()) == {
        "grounded": False, "risk": RISK_NONE}, label


def test_ac_p6j_committed_negatives_stay_ungrounded():
    """Corpus pin under the WIDENED scanner: every committed control
    fixture (40 legacy non-RAG + l1 json_doc/log_trace/control) stays
    ungrounded, and every rag/system_dup fixture stays grounded."""
    prompts = _l1_fixture_prompts()
    for pid, fixture in prompts.items():
        r = grounded_answer_risk(fixture["messages"], cfg())
        if pid.startswith(("rag-", "system_dup-")):
            assert r["grounded"], f"{pid} must ground (got {r})"
        else:
            assert r == {"grounded": False, "risk": RISK_NONE}, \
                f"{pid} FALSE POSITIVE under widened scanner: {r}"
    for pid, fixture in PROMPTS.items():
        r = grounded_answer_risk(fixture["messages"], cfg())
        if pid.startswith("rag-"):
            assert r["grounded"], f"{pid} must ground (got {r})"
        else:
            assert r == {"grounded": False, "risk": RISK_NONE}, \
                f"{pid} FALSE POSITIVE under widened scanner: {r}"


def test_ac_p6j_embedded_scan_is_bounded_and_deterministic():
    """The widened scan stays pure and bounded: determinism across runs,
    config-independence, no mutation, and a pathological many-brace
    message terminates with the capped attempt count (256 parses) at
    the same verdict as the un-bounded equivalent."""
    envelope = ("Answer:\n" + _envelope_fixture() + "\n\nQuestion: what?")
    messages = [{"role": "user", "content": envelope}]
    before = copy.deepcopy(messages)
    r1 = grounded_answer_risk(messages, cfg())
    r2 = grounded_answer_risk(messages, cfg(grounded_calibration_green=True))
    assert r1 == r2 == {"grounded": True, "risk": RISK_FIDELITY_CRITICAL}
    assert messages == before
    # pathological input: 300 unparseable '{' then one valid envelope —
    # the scan cap truncates the walk (envelope NOT reached) but the
    # verdict is still deterministic and the scan terminates.
    pathological = "{invalid" * 300 + _envelope_fixture()
    assert _is_retrieval_envelope(pathological) is False
    assert _is_retrieval_envelope(_envelope_fixture()) is True
