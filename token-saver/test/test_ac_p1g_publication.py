"""AC-P1g publication-contract regressions for the PM's C-7a amendments.

QA's test_ac_p1bcd.py pins the headline sub-2pp case and the estimator
interval consumption. This file pins the three amendment rulings that rode
the same C-7a commit:

1. the publication keys are emitted on ``blended_corpus_wide`` too (a
   suppressed headline next to a bare blended percentage is the same
   noise-dressed-as-signal publication one field over);
2. a degenerate interval (fewer than 2 valid pairs / zero-width CI) never
   publishes a percentage, even at a healthy point estimate;
3. a genuine measurable effect (>= 2pp, CI excluding 0) DOES carry the
   honest percent — the guard must suppress noise, not signal;
4. a CI that includes 0 is ``no_measurable_effect`` regardless of the
   point estimate magnitude.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

import run_benchmark  # noqa: E402


def _sub2pp_fixture() -> list[dict]:
    return [
        {"id": "eligible", "eligible": True,
         "baseline_tokens": 10000.0, "treatment_tokens": 9850.0},
        {"id": "ineligible", "eligible": False,
         "baseline_tokens": 100.0, "treatment_tokens": 100.0},
    ]


def test_ac_p1g_publication_keys_on_blended_corpus_wide():
    """Every published figure carries the contract, blended included."""
    blended = run_benchmark.summarize(_sub2pp_fixture())["blended_corpus_wide"]
    assert "publication_status" in blended
    assert "reported_reduction_pct" in blended
    assert "publication_note" in blended
    assert blended["publication_status"] == "no_measurable_effect"
    assert blended["reported_reduction_pct"] is None
    assert "2pp" in blended["publication_note"]


def test_ac_p1g_degenerate_interval_never_publishes_a_percentage():
    """The n=1-at-3.1pp zero-width-CI probe: a healthy point estimate on a
    degenerate interval still ships no percentage — no variance information,
    no claim (PM amendment 3)."""
    stats = run_benchmark.summarize([
        {"id": "solo", "eligible": True,
         "baseline_tokens": 1000.0, "treatment_tokens": 969.0},
    ])
    headline = stats["headline"]
    assert headline["mean_output_reduction_pct"] == 3.1
    assert headline["publication_status"] == "no_measurable_effect"
    assert headline["reported_reduction_pct"] is None


def test_ac_p1g_measurable_reduction_publishes_the_percentage():
    """The guard suppresses noise, not signal: 15 valid pairs, every one
    reducing 12-18%, estimate >= 2pp, 95% CI excluding 0 -> the honest
    measured percent is published."""
    entries = [{"id": f"p{i}", "eligible": True,
                "baseline_tokens": 900.0 + i * 7.0,
                "treatment_tokens": (900.0 + i * 7.0) * 0.85}
               for i in range(15)]
    headline = run_benchmark.summarize(entries)["headline"]
    est = headline["mean_output_reduction_pct"]
    assert est >= run_benchmark.PUBLICATION_FLOOR_PP
    assert headline["ci95_interval"][0] > 0.0
    assert headline["publication_status"] == "measurable_reduction"
    assert headline["reported_reduction_pct"] == est


def test_ac_p1g_ci_including_zero_is_no_measurable_effect():
    """4.33pp point estimate, but the 95% CI straddles 0: no measured
    effect, regardless of the point estimate magnitude."""
    entries = [{"id": f"p{i}", "eligible": True,
                "baseline_tokens": 900.0 + i,
                "treatment_tokens": (900.0 + i)
                * (0.70 if i % 2 == 0 else 1.25)}
               for i in range(15)]
    headline = run_benchmark.summarize(entries)["headline"]
    assert headline["mean_output_reduction_pct"] > run_benchmark.PUBLICATION_FLOOR_PP
    assert headline["ci95_interval"][0] < 0.0 < headline["ci95_interval"][1]
    assert headline["publication_status"] == "no_measurable_effect"
    assert headline["reported_reduction_pct"] is None
