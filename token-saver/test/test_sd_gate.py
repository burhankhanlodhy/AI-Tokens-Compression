"""SD-gate predicate split (PM ruling at 9b1ac2d).

The v1 gate (`qualifies = cv < 0.35 AND reasoning_separately_reported`)
rewarded the silent mapping-failure mode (override ignored -> rt > 0 ->
"separately reported" -> qualifies) and punished the success mode
(suppression works -> all zeros -> not "reported" -> fails).

v2 records two separate facts and qualifies on SUPPRESSION CONFIRMED:
  - reasoning_field_accepted: override accepted (True) / rejected via the
    proxy's 400-retry path (False) / no override sent (None)
  - reasoning_tokens_observed: raw per-sample counts, zero = pass value
"""
from __future__ import annotations

import sys
from pathlib import Path

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

from measure_sd import compute_gate  # noqa: E402


def _summary(cv: float) -> dict:
    # 862.0 = the fixed prompt's expected visible-answer mean (glm on-file:
    # 3,937 billed, of which 3,075 reasoning — mean is a gate cross-check).
    return {"cv": cv, "mean": 862.0}


def test_working_suppression_qualifies_despite_no_reasoning_tokens():
    """THE regression: suppression works -> all zeros -> must QUALIFY.

    This is exactly what v1 got backwards (Gemini with thinking_level
    minimal reports zero reasoning tokens; the v1 gate called that a fail).
    """
    gate = compute_gate(_summary(0.10), [0, 0, 0, 0, 0], field_accepted=True)
    assert gate["suppression_confirmed"] is True
    assert gate["qualifies"] is True
    assert gate["cv_lt_0.35"] is True


def test_field_absent_does_not_qualify_even_at_zero_tokens_and_good_cv():
    """THE v2 hole (PM repro at 6f60399): details = {}, override accepted,
    CV 0.20 — provider never reported reasoning_tokens, so the zeros are
    unverifiable (the model can be thinking at default level and just not
    saying so). Unattributable => FAIL, same class as field_accepted None.
    """
    gate = compute_gate(_summary(0.20), [0, 0, 0, 0, 0],
                        field_accepted=True, field_present=False)
    assert gate["reasoning_field_present"] is False
    assert gate["suppression_confirmed"] is False
    assert gate["qualifies"] is False


def test_mixed_field_presence_does_not_qualify():
    """One sample with the field absent poisons the record: gate must fail
    closed, not average the evidence."""
    gate = compute_gate(_summary(0.10), [0, 0, 0], field_accepted=True,
                        field_present=False)
    assert gate["qualifies"] is False


def test_mean_completion_tokens_persisted_as_cross_check():
    """PM (b): the gate block must carry the billed mean so a ~3,000-token
    'zero-reasoning' result is self-evidently a lie in the pass/fail record
    (glm billed 3,937 tok/call, 3,075 reasoning, ~862 visible)."""
    gate = compute_gate(_summary(0.10), [0, 0, 0], field_accepted=True)
    assert gate["mean_completion_tokens"] == _summary(0.10)["mean"]


def test_silent_mapping_failure_does_not_qualify():
    """Override 'accepted' but the model thinks anyway (rt > 0) -> FAIL.

    v1 rewarded exactly this failure mode with qualifies=True.
    """
    gate = compute_gate(_summary(0.10), [0, 0, 3000, 0, 0], field_accepted=True)
    assert gate["reasoning_tokens_observed"] == [0, 0, 3000, 0, 0]
    assert gate["suppression_confirmed"] is False
    assert gate["qualifies"] is False


def test_override_rejected_does_not_qualify_even_at_zero_tokens():
    """The 400-retry path (x-token-saver-reasoning: rejected_*) means the
    override never took effect — zero observed tokens prove nothing."""
    gate = compute_gate(_summary(0.10), [0, 0, 0], field_accepted=False)
    assert gate["reasoning_field_accepted"] is False
    assert gate["suppression_confirmed"] is False
    assert gate["qualifies"] is False


def test_no_override_sent_cannot_confirm_suppression():
    """No x-token-saver-reasoning evidence on any sample -> field_accepted
    is None -> suppression is unattributable -> does not qualify."""
    gate = compute_gate(_summary(0.10), [0, 0, 0], field_accepted=None)
    assert gate["reasoning_field_accepted"] is None
    assert gate["suppression_confirmed"] is False
    assert gate["qualifies"] is False


def test_pm_field_absent_repro_is_a_fail():
    """Verbatim repro of the PM's finding at 6f60399: details={},
    override accepted, CV 0.20 -> the v2 gate printed qualifies: True.
    v3 must print qualifies: False."""
    gate = compute_gate({"cv": 0.20, "mean": 862.0}, [0, 0, 0, 0, 0],
                        field_accepted=True, field_present=False)
    assert gate["suppression_confirmed"] is False
    assert gate["qualifies"] is False
    assert gate["mean_completion_tokens"] == 862.0


def test_high_cv_fails_even_with_confirmed_suppression():
    gate = compute_gate(_summary(0.50), [0, 0, 0], field_accepted=True)
    assert gate["cv_lt_0.35"] is False
    assert gate["qualifies"] is False


def test_empty_observed_list_cannot_confirm():
    gate = compute_gate(_summary(0.10), [], field_accepted=True)
    assert gate["suppression_confirmed"] is False
