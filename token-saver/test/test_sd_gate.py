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
    return {"cv": cv}


def test_working_suppression_qualifies_despite_no_reasoning_tokens():
    """THE regression: suppression works -> all zeros -> must QUALIFY.

    This is exactly what v1 got backwards (Gemini with thinking_level
    minimal reports zero reasoning tokens; the v1 gate called that a fail).
    """
    gate = compute_gate(_summary(0.10), [0, 0, 0, 0, 0], field_accepted=True)
    assert gate["suppression_confirmed"] is True
    assert gate["qualifies"] is True
    assert gate["cv_lt_0.35"] is True


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


def test_high_cv_fails_even_with_confirmed_suppression():
    gate = compute_gate(_summary(0.50), [0, 0, 0], field_accepted=True)
    assert gate["cv_lt_0.35"] is False
    assert gate["qualifies"] is False


def test_empty_observed_list_cannot_confirm():
    gate = compute_gate(_summary(0.10), [], field_accepted=True)
    assert gate["suppression_confirmed"] is False
