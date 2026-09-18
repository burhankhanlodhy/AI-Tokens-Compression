"""P2: per-category + ratified headline-carve emission (AC-P1 / option (a)).

Before this file, the published 57.71pp carve (option (a), user
2026-09-17, @product-manager in-file) was a hand computation that existed
NOWHERE in machine-emitted output — the run artifact carried only
`headline` / `blended_corpus_wide` / `quality_parity`, and
`category` appeared exactly once in run_benchmark.py (per-row tagging).
These tests pin the P2 emission contract:

1. `summarize()` emits a `by_category` block (one entry per category
   present in the results) and a `headline_carve` block (the eligible
   non-code non-RAG subset), each carrying the full AC-P1g triplet
   (`publication_status`, `reported_reduction_pct`, `publication_note`);
2. the carve is the PUBLISHED claim: it excludes code and rag, ships the
   ratified calibration caveat, and its own parity gate is scoped to the
   carve population (a category must not inherit a green from the
   run-level population);
3. RAG lands with its parity failure attached — 26.18pp measured,
   `no_measurable_effect` + suppressed-pending-fix note, reported
   percentage null: visible beside the headline, never sellable;
4. a zero-eligible category (code: all 10 fixtures gate-negative, K-6)
   emits the triplet with a construction note, not a floor note;
5. the committed P1-1 artifact re-summarized through the shipped
   `summarize()` lands EXACTLY on the ratified arithmetic: carve
   57.71pp / signed regression −0.35pt, RAG 26.18pp / −2.00pt,
   conversational 54.39 / qa 59.96.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

import run_benchmark  # noqa: E402

ARTIFACT = (BENCHMARK / "results"
            / "benchmark_google_gemini-3.5-flash-lite_20260918T040421Z.json")


def _judged(pid: str, category: str, base: float, treat: float,
            score_a: int = 9, score_b: int = 9) -> dict:
    return {"id": pid, "category": category, "eligible": True,
            "baseline_tokens": base, "treatment_tokens": treat,
            "treatment_sampled": True,
            "mode": "model_judge",
            "score_a": score_a, "score_b": score_b, "winner": "tie"}


def _synthetic_run(rag_regression: bool = False) -> list[dict]:
    """15 eligible (5 per non-code category, K-6 shape) at a ~50% effect
    with per-item variance (so the bootstrap interval is non-degenerate —
    a uniform effect is a zero-width CI the contract rightly suppresses),
    plus 40 ineligible code prompts (gate-negative)."""
    rows: list[dict] = []
    for cat in ("conversational", "qa", "rag"):
        for j in range(5):
            a, b = 9, 9
            if rag_regression and cat == "rag" and j == 0:
                a, b = 9, 3          # a >1pt parity regression in RAG
            base = 900.0 + j * 137.0
            # varied true effects (45-55% non-RAG, 57-65% RAG) => variance
            # for the bootstrap AND carve != headline (RAG differs)
            ratio = (0.65 - j * 0.025) if cat == "rag" else (0.55 - j * 0.025)
            rows.append(_judged(f"{cat}-{j}", cat, base, base * ratio, a, b))
    for i in range(40):
        rows.append({"id": f"code-{i}", "category": "code",
                     "eligible": False, "baseline_tokens": 500.0,
                     "treatment_tokens": 500.0, "treatment_sampled": True,
                     "baseline_ok": True, "treatment_ok": True})
    return rows


# ---------------------------------------------------------------------------
# 1. every subset block carries the full AC-P1g triplet
# ---------------------------------------------------------------------------

def test_ac_p2_by_category_blocks_carry_the_full_triplet():
    stats = run_benchmark.summarize(_synthetic_run())
    blocks = stats["by_category"]["categories"]
    assert set(blocks) == {"code", "conversational", "qa", "rag"}
    for cat, blk in blocks.items():
        for key in ("publication_status", "reported_reduction_pct",
                    "publication_note"):
            assert key in blk, f"{cat} missing {key}"
        assert "quality_parity" in blk
        assert "n_eligible" in blk and "n_valid" in blk
        assert "ci95_interval" in blk and len(blk["ci95_interval"]) == 2


def test_ac_p2_headline_carve_block_carries_the_full_triplet():
    carve = run_benchmark.summarize(_synthetic_run())["headline_carve"]
    assert carve["publication_status"] == "measurable_reduction"
    assert carve["reported_reduction_pct"] == carve["mean_output_reduction_pct"]
    assert "publication_note" in carve
    assert "quality_parity" in carve


def test_ac_p2_measurable_category_publishes_and_sub_floor_suppresses():
    """The contract suppresses noise, not signal, per category too."""
    stats = run_benchmark.summarize(_synthetic_run())
    qa = stats["by_category"]["categories"]["qa"]
    assert qa["publication_status"] == "measurable_reduction"
    assert qa["reported_reduction_pct"] == qa["mean_output_reduction_pct"]


# ---------------------------------------------------------------------------
# 2. the carve is the ratified option-(a) population
# ---------------------------------------------------------------------------

def test_ac_p2_headline_carve_excludes_code_and_rag():
    stats = run_benchmark.summarize(_synthetic_run())
    carve = stats["headline_carve"]
    assert carve["n_eligible"] == 10   # 5 conversational + 5 qa, no rag
    assert "non-code non-RAG" in carve["population"]
    # and the carve is the mean of exactly its own subset, not the
    # run-level headline (which includes RAG here: 15 eligible items).
    assert stats["n_eligible"] == 15
    assert carve["mean_output_reduction_pct"] != stats["headline"][
        "mean_output_reduction_pct"]


def test_ac_p2_carve_ships_the_ratified_calibration_caveat():
    carve = run_benchmark.summarize(_synthetic_run())["headline_carve"]
    assert "calibration_caveat" in carve
    assert "CV=0.137" in carve["calibration_caveat"]
    assert "N_PROMPTS=10" in carve["calibration_caveat"]


def test_ac_p2_carve_parity_gate_is_scoped_to_the_carve_population():
    """A RAG-only regression must NOT red the carve, and must not green
    RAG itself: the carve block's parity reads only carve items."""
    stats = run_benchmark.summarize(_synthetic_run(rag_regression=True))
    carve = stats["headline_carve"]
    rag = stats["by_category"]["categories"]["rag"]
    assert carve["quality_parity"]["parity_holds"] is True
    assert carve["quality_parity"]["n_judged"] == 10
    assert carve["publication_status"] == "measurable_reduction"
    # RAG carries the failure in its own block
    assert rag["quality_parity"]["parity_holds"] is False
    assert rag["quality_parity"]["n_regressions_over_1pt"] == 1


# ---------------------------------------------------------------------------
# 3. RAG: measured beside the headline, suppressed-pending-fix
# ---------------------------------------------------------------------------

def test_ac_p2_rag_parity_failure_suppresses_even_a_healthy_interval():
    """The suppression contract extends to subsets: a healthy estimate on
    a CI excluding 0 is still no_measurable_effect when the subset's own
    parity gate fails (AC-P1g: a sellable percentage beside a red parity
    gate is the same noise-dressed-as-signal defect one field over)."""
    entries = [_judged(f"rag-{i}", "rag", 900.0 + i * 137.0,
                       (900.0 + i * 137.0) * (0.72 - i * 0.02))
               for i in range(5)]                 # ~20-28% true effect
    # two parity regressions (6pt + 3pt): mean = 9/5 = 1.8pt > 1pt -> FAILS
    entries[3] = _judged("rag-x", "rag", 1448.0, 1013.6, 9, 3)
    entries[4] = _judged("rag-y", "rag", 1585.0, 1109.5, 9, 6)
    stats = run_benchmark.summarize(entries)
    rag = stats["by_category"]["categories"]["rag"]
    assert rag["mean_output_reduction_pct"] > 20.0   # the raw mean is real
    assert rag["ci95_interval"][0] > 0.0              # CI excludes 0
    assert rag["quality_parity"]["parity_holds"] is False
    assert rag["publication_status"] == "no_measurable_effect"
    assert rag["reported_reduction_pct"] is None
    assert "parity" in rag["publication_note"]
    assert "suppressed-pending-fix" in rag["publication_note"]


def test_ac_p2_rag_still_publishes_when_parity_holds():
    """Suppression is conditional, not a permanent RAG brand: a clean RAG
    subset publishes its honest percent like any other category (P6's fix
    must be able to un-suppress by MEASUREMENT, not by editing tests)."""
    entries = [_judged(f"rag-{i}", "rag", 900.0 + i * 137.0,
                       (900.0 + i * 137.0) * (0.72 - i * 0.02))
               for i in range(5)]
    stats = run_benchmark.summarize(entries)
    rag = stats["by_category"]["categories"]["rag"]
    assert rag["quality_parity"]["parity_holds"] is True
    assert rag["publication_status"] == "measurable_reduction"
    assert rag["reported_reduction_pct"] == rag["mean_output_reduction_pct"]


# ---------------------------------------------------------------------------
# 4. zero-eligible categories state construction, not a floor miss
# ---------------------------------------------------------------------------

def test_ac_p2_zero_eligible_code_block_is_a_construction_note():
    entries = [{"id": f"code-{i}", "category": "code", "eligible": False,
                "baseline_tokens": 500.0, "treatment_tokens": 500.0,
                "treatment_sampled": False}
               for i in range(10)]
    code = run_benchmark.summarize(entries)[
        "by_category"]["categories"]["code"]
    assert code["n_eligible"] == 0
    assert code["publication_status"] == "no_measurable_effect"
    assert code["reported_reduction_pct"] is None
    assert "no eligible" in code["publication_note"]
    assert "by construction" in code["publication_note"]
    assert "3pp" not in code["publication_note"]  # NOT a floor miss


def test_ac_p2_subset_upstream_loss_suppresses_that_category_only():
    """A category that lost items upstream inherits no green from the
    run-level population: with the planned denominator supplied, the rag
    block is suppressed at precedence-0 while conversational still
    publishes off its complete population."""
    planned = ([f"conv-{i}" for i in range(5)]
               + [f"rag-{i}" for i in range(5)])
    entries = [_judged(f"conv-{i}", "conversational", 900.0 + i * 137.0,
                       (900.0 + i * 137.0) * (0.55 - i * 0.025))
               for i in range(5)]
    entries += [_judged(f"rag-{i}", "rag", 900.0 + i * 137.0,
                        (900.0 + i * 137.0) * 0.6)
                for i in range(2)]   # 3 of 5 lost upstream
    planned_cats = {**{f"conv-{i}": "conversational" for i in range(5)},
                    **{f"rag-{i}": "rag" for i in range(5)}}
    stats = run_benchmark.summarize(
        entries, n_eligible_planned=10, planned_judge_ids=planned,
        planned_categories=planned_cats)
    rag = stats["by_category"]["categories"]["rag"]
    conv = stats["by_category"]["categories"]["conversational"]
    assert rag["publication_status"] == "no_measurable_effect"
    assert "population incomplete" in rag["publication_note"]
    assert rag["quality_parity"]["upstream_lost_ids"] == ["rag-2", "rag-3",
                                                          "rag-4"]
    assert conv["publication_status"] == "measurable_reduction"


# ---------------------------------------------------------------------------
# 5. THE audit-hole closer: re-summarize the committed artifact through the
#    shipped code path — the ratified carve arithmetic must land exactly.
# ---------------------------------------------------------------------------

def test_ac_p2_committed_artifact_carve_numbers_land():
    """The published 57.71pp carve exists in machine-emitted output.

    Loads the committed P1-1 both-orders artifact, feeds its rows through
    the shipped summarize(), and asserts the ratified option-(a) numbers:
    carve 57.71pp / signed −0.35pt, RAG 26.18pp / −2.00pt, per-category
    conversational 54.39 / qa 59.96. Any regression in the estimator, the
    carve definition, or the parity accounting breaks this test — the
    hand-computation can never silently drift from the code again.
    """
    d = json.loads(ARTIFACT.read_text())
    rows = d["results"]
    assert d["fixture_checksum"] == d["fixture_checksum_pinned"]
    valid = [r for r in rows
             if r.get("baseline_ok") and r.get("treatment_ok")]
    assert len(valid) == d["n_valid"] == 55
    planned = [r["id"] for r in valid if r["eligible"]]
    stats = run_benchmark.summarize(valid, n_eligible_planned=len(planned),
                                    planned_judge_ids=planned)

    carve = stats["headline_carve"]
    assert carve["n_eligible"] == 10
    assert carve["mean_output_reduction_pct"] == 57.71
    assert carve["publication_status"] == "measurable_reduction"
    assert carve["reported_reduction_pct"] == 57.71
    assert carve["meets_15pct"] is True
    qp = carve["quality_parity"]
    assert qp["signed_mean_regression_pt"] == -0.35
    assert qp["mean_regression_pt"] == 0.35          # gate convention
    assert qp["n_regressions_over_1pt"] == 1         # qa-049
    assert qp["parity_holds"] is True
    assert carve["calibration_caveat"]

    cats = stats["by_category"]["categories"]
    assert set(cats) == {"code", "conversational", "qa", "rag"}
    conv = cats["conversational"]
    qa = cats["qa"]
    rag = cats["rag"]
    code = cats["code"]
    assert conv["mean_output_reduction_pct"] == 54.39
    assert conv["quality_parity"]["signed_mean_regression_pt"] == -0.4
    assert conv["publication_status"] == "measurable_reduction"
    assert qa["mean_output_reduction_pct"] == 59.96
    assert qa["quality_parity"]["signed_mean_regression_pt"] == -0.3
    assert qa["publication_status"] == "measurable_reduction"
    assert rag["mean_output_reduction_pct"] == 26.18   # measured, visible
    assert rag["quality_parity"]["signed_mean_regression_pt"] == -2.00
    assert rag["quality_parity"]["parity_holds"] is False
    assert rag["publication_status"] == "no_measurable_effect"
    assert rag["reported_reduction_pct"] is None
    assert "suppressed-pending-fix" in rag["publication_note"]
    assert code["n_eligible"] == 0
    assert code["reported_reduction_pct"] is None
    assert "no eligible" in code["publication_note"]


def test_ac_p2_status_literals_are_the_pinned_contract_strings():
    """The new blocks reuse the SAME pinned literals as headline/blended
    (spec: implement to those strings, do not rename)."""
    stats = run_benchmark.summarize(_synthetic_run(rag_regression=True))
    assert (stats["by_category"]["categories"]["rag"]["publication_status"]
            == "no_measurable_effect")
    assert stats["headline_carve"]["publication_status"] == (
        "measurable_reduction")
    code = stats["by_category"]["categories"]["code"]
    assert code["publication_status"] == "no_measurable_effect"
