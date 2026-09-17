"""SD-gate v4 tests: bounded-and-visible predicate (PM ruling).

History of the predicate, each step a real hole caught in review:
  v1 `qualifies = cv<0.35 AND reasoning_separately_reported` rewarded LOUD
     failure (override ignored -> rt>0 -> "reported" -> qualifies).
  v2 split field_accepted/tokens_observed but rewarded QUIET failure
     (field-absent zeros).
  v3 required all-zeros — unsatisfiable on instruments whose floor is
     MINIMAL (Gemini 3.5 Flash-Lite), i.e. a gate no candidate can pass.
  v4 (this file): qualifies = cv < 0.35 AND field_accepted AND
     field_present (every sample) AND reasoning_share <= 1/3
     (ratio-of-sums), with the denominator invariant enforced and the
     differential probe recorded. The share is recomputable from the
     artifact, never asserted.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

from measure_sd import compute_gate, denominator_check, evaluate_probe  # noqa: E402

SHARE_CAP = 1 / 3


def _summary(cv: float, mean: float = 862.0) -> dict:
    # 862.0 = the fixed prompt's expected visible-answer mean (glm on-file:
    # 3,937 billed, of which 3,075 reasoning — the mean is a gate cross-check).
    return {"cv": cv, "mean": mean}


def _gate(cv=0.10, rt=None, ct=None, accepted=True, present=True, denom=True):
    rt = rt if rt is not None else [10, 12, 11]
    ct = ct if ct is not None else [400, 420, 390]
    return compute_gate(_summary(cv), rt, ct, accepted,
                        field_present=present, denominator_ok=denom)


def test_bounded_low_share_qualifies():
    """MINIMAL flooring with a small reasoning share: the pass case."""
    gate = _gate()
    assert gate["reasoning_share_ratio_of_sums"] < SHARE_CAP
    assert gate["share_within_cap"] is True
    assert gate["reasoning_bounded_confirmed"] is True
    assert gate["qualifies"] is True


def test_glm_archived_share_fails_by_wide_margin():
    """glm on-file: 3,937 billed / 3,075 reasoning -> 0.781 ratio-of-sums.
    PM verified this reproduces the finding that triggered the swap; the
    cap must reject it by >2x."""
    gate = _gate(rt=[3075] * 5, ct=[3937] * 5)
    assert abs(gate["reasoning_share_ratio_of_sums"] - 0.781) < 0.01
    assert gate["share_within_cap"] is False
    assert gate["reasoning_bounded_confirmed"] is False
    assert gate["qualifies"] is False


def test_share_is_ratio_of_sums_not_mean_of_ratios():
    rt, ct = [10, 100], [1000, 100]
    gate = compute_gate(_summary(0.10), rt, ct, True)
    assert abs(gate["reasoning_share_ratio_of_sums"] - 110 / 1100) < 1e-9


def test_share_at_cap_passes():
    """Boundary: share == cap is within it (<=)."""
    gate = _gate(rt=[1], ct=[3])
    assert gate["reasoning_share_ratio_of_sums"] <= SHARE_CAP
    assert gate["share_within_cap"] is True
    assert gate["qualifies"] is True


def test_field_absent_does_not_qualify_even_at_low_share():
    """v2 hole regression (PM repro at 6f60399): details={} with a GOOD CV
    and a GOOD share still fails — the numerator is unverifiable."""
    gate = _gate(cv=0.20, rt=[0, 0, 0], ct=[862, 900, 800], present=False)
    assert gate["reasoning_field_present"] is False
    assert gate["reasoning_bounded_confirmed"] is False
    assert gate["qualifies"] is False


def test_control_rejected_does_not_qualify():
    """OpenRouter rejects the control outright (mandatory-reasoning 400):
    field_accepted=False fails the gate regardless of the share."""
    gate = _gate(accepted=False)
    assert gate["reasoning_field_accepted"] is False
    assert gate["reasoning_bounded_confirmed"] is False
    assert gate["qualifies"] is False


def test_no_control_evidence_cannot_qualify():
    """No x-token-saver-reasoning evidence on any sample -> field_accepted
    is None -> boundedness is unattributable -> does not qualify."""
    gate = _gate(accepted=None)
    assert gate["reasoning_field_accepted"] is None
    assert gate["reasoning_bounded_confirmed"] is False
    assert gate["qualifies"] is False


def test_high_cv_fails_even_with_bounded_reasoning():
    gate = _gate(cv=0.50)
    assert gate["cv_lt_0.35"] is False
    assert gate["qualifies"] is False


def test_denominator_violation_fails_closed():
    """PM trap 2: if reasoning tokens are reported OUTSIDE completion_tokens,
    the share's base is wrong. Gate must fail even at a tiny share."""
    gate = _gate(rt=[5, 5, 5], ct=[400, 420, 390], denom=False)
    assert gate["denominator_invariant_ok"] is False
    assert gate["share_within_cap"] is False
    assert gate["reasoning_bounded_confirmed"] is False
    assert gate["qualifies"] is False


def test_mean_completion_tokens_persisted_as_cross_check():
    """PM (b): the gate block must carry the billed mean so a ~3,000-token
    'zero-reasoning' result is self-evidently a lie in the pass/fail record
    (glm billed 3,937 tok/call, 3,075 reasoning, ~862 visible)."""
    gate = _gate()
    assert gate["mean_completion_tokens"] == 862.0


# ---------------------------------------------------------------------------
# Denominator invariant, per-sample
# ---------------------------------------------------------------------------

def test_denominator_check_inclusive_reporting_is_ok():
    """glm-style: completion includes reasoning; visible tracks chars/4."""
    sample = {"completion_tokens": 1000, "reasoning_tokens": 600, "chars": 1600}
    check = denominator_check(sample)
    assert check["visible_tokens"] == 400
    assert check["denominator_ok"] is True


def test_denominator_check_additive_reporting_violates():
    """If completion EXCLUDES reasoning (additive reporting), the subtraction
    collapses and the ratio falls out of band -> untrustworthy base."""
    sample = {"completion_tokens": 1000, "reasoning_tokens": 600, "chars": 4000}
    check = denominator_check(sample)
    assert check["visible_tokens"] == 400
    assert abs(check["visible_est_tokens_chars_over_4"] - 1000) < 1e-9
    assert check["denominator_ok"] is False


# ---------------------------------------------------------------------------
# Differential control-efficacy probe
# ---------------------------------------------------------------------------

def test_probe_demonstrated_when_counts_move():
    probe = evaluate_probe([10, 12], [400, 450])
    assert probe["control_efficacy"] == "demonstrated"
    assert "live control" in probe["boundedness_claim"]


def test_probe_not_demonstrated_when_counts_are_indistinguishable():
    """MINIMAL is the model's DEFAULT: if HIGH doesn't move the counts, the
    parameter is decorative and the record must say the boundedness comes
    from the model default, not from our control."""
    probe = evaluate_probe([10, 12], [10, 12])
    assert probe["control_efficacy"] == "not demonstrated"
    assert "bounded by model default" in probe["boundedness_claim"]


def test_probe_skipped_records_unknown_efficacy():
    probe = evaluate_probe([], [])
    assert probe["control_efficacy"] == "skipped"
    assert "unattributed" in probe["boundedness_claim"]
