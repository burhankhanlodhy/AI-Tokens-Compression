"""P1-1 headline estimator — SINGLE SOURCE OF TRUTH.

Imported by BOTH run_benchmark.py (the harness that spends provider money)
and empty_box.py (the calibration gate that clears or blocks it). Neither
may re-implement this math: a gate that computes its own statistics cannot
fail when the estimator it is meant to calibrate is sabotaged (the exact
defect the 2026-09 PM audit caught in the first empty_box draft — a patched
`mean_reduction = 99.0` left the gate green).

CURRENT BODY: the production estimator as it ships today — mean-of-
per-prompt-ratios with a normal 1.96·SE CI, fed by HARNESS_K = 1 sample per
arm per prompt. This is the estimator the 2026-09 audit condemned
(collapses toward ~0% under realistic output-length CV; cannot distinguish
"no effect" from "~17% effect").

C-4b contract (@application-developer): replace `estimate()` with the
corrected AC-P1a estimator — ratio-of-sums headline, bootstrap/Wilcoxon
inference, t(39)=2.023, and HARNESS_K >= 5 samples per arm — keeping the
same signature. Nothing else changes: empty_box.py automatically re-simulates
the new regime and flips green, which is C-4b's acceptance evidence.
"""
from __future__ import annotations

import statistics

# Samples per arm per prompt the harness ACTUALLY takes (run_benchmark.py
# currently calls run_one once per arm). The gate simulates this regime.
# C-4b raises this to >= 5; do NOT change it here without changing the
# harness to match.
HARNESS_K = 1


def estimate(pairs: list[tuple[float, float]]) -> dict:
    """Headline reduction + 95% CI from per-prompt token pairs.

    pairs: list of (baseline_tokens, treatment_tokens) per prompt, already
    aggregated over the arm's HARNESS_K samples (k=1 today).
    Returns {"mean_reduction_pct": float, "ci95": float}.
    """
    reductions = [100.0 * (b - t) / b for b, t in pairs if b > 0]
    if not reductions:
        return {"mean_reduction_pct": 0.0, "ci95": 0.0}
    mean = statistics.mean(reductions)
    if len(reductions) >= 2:
        se = statistics.stdev(reductions) / (len(reductions) ** 0.5)
        ci95 = 1.96 * se
    else:
        ci95 = 0.0
    return {"mean_reduction_pct": mean, "ci95": ci95}