"""P6-1/P6-2 acceptance tests: grounded-answer detector + dose tiers.

Covers AC-P6a (pure function; fixture classification; bare-keyword
negative) and AC-P6b (tiers config-declared; request path never injects
above the discriminator's selection; deterministic per canonical
prompt). Spec: product-spec-v2.md §P6, ratified 2026-09-18.
"""
from __future__ import annotations

import copy
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
    grounded_answer_risk,
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
@pytest.mark.xfail(
    reason="AC-P6g pending P6-4 scope + signal-set fix (@application-developer)",
    strict=False,
)
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


@pytest.mark.xfail(
    reason="AC-P6g pending P6-4 scope + signal-set fix (@application-developer)",
    strict=False,
)
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
    assert grounded_answer_risk(other, cfg())["risk"] in (RISK_NONE,
                                                          RISK_BOUNDED)
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
