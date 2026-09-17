"""P1-1 headline estimator — SINGLE SOURCE OF TRUTH.

Imported by BOTH run_benchmark.py (the harness that spends provider money)
and empty_box.py (the calibration gate that clears or blocks it). Neither
may re-implement this math: a gate that computes its own statistics cannot
fail when the estimator it is meant to calibrate is sabotaged (the exact
defect the 2026-09 PM audit caught in the first empty_box draft — a patched
`mean_reduction = 99.0` left the gate green).

CURRENT BODY (C-4b, the corrected AC-P1a mathematics that replaced the
condemned k=1 mean-of-per-prompt-ratios):

- HEADLINE: ratio-of-sums — 100 · (1 − Σtᵢ/Σbᵢ) over the per-prompt token
  pairs. The mean-of-ratios it replaces collapses toward ~0% under realistic
  output-length CV and cannot distinguish "no effect" from "~17% effect".
- INFERENCE: paired prompt-level bootstrap (percentile 95% CI) over the
  prompts themselves, seeded deterministically so every run of the same
  input yields the identical interval (AC-P1b reproducibility). The public
  halfwidth is the conservative symmetric max distance from the headline to
  either percentile bound, so consumers reading `est ± ci95` never see a
  narrower interval than the bootstrap produced.
- SAMPLING: HARNESS_K samples per arm per prompt, aggregated into
  per-prompt (baseline_total, treatment_total) sums by the harness BEFORE
  estimate() is called. HARNESS_K = 30 per the PM ruling measured at
  n=15 (2026-09-16): k=12 fails the control-success contract (81.2%),
  k=20 lands on the threshold inside its own binomial noise, k=30 passes
  (null FP 0.8%, control success 97.0% at 400 seeds).

`estimate()` returns the headline population it is handed: the harness feeds
it the ELIGIBLE subset (prompts where the conciseness gate fires — the
published headline, per the PM subset-headline ruling) and separately the
full corpus (published beside it, explicitly labelled blended). The
estimator itself is population-agnostic.
"""
from __future__ import annotations

import random

# Samples per arm per prompt the harness ACTUALLY takes (run_benchmark.py
# loops run_one HARNESS_K times per arm and aggregates the token sums).
# Keep this and the harness sampling loop in lockstep — the calibration
# gate simulates exactly this regime.
HARNESS_K = 30

# Paired bootstrap resamples for the 95% CI. Deterministic seed so the same
# input always produces the same interval (results JSON is reproducible).
BOOTSTRAP_RESAMPLES = 1000
_BOOTSTRAP_SEED = 20260916


def estimate(pairs: list[tuple[float, float]]) -> dict:
    """Headline reduction + 95% CI from per-prompt token pairs.

    pairs: list of (baseline_tokens, treatment_tokens) per prompt, already
    aggregated over the arm's HARNESS_K samples.
    Returns {"mean_reduction_pct": float, "ci95": float,
             "ci95_interval": [lo, hi]}  (percentile bounds).
    """
    valid = [(b, t) for b, t in pairs if b > 0]
    if not valid:
        return {"mean_reduction_pct": 0.0, "ci95": 0.0,
                "ci95_interval": [0.0, 0.0]}

    sum_b = sum(b for b, _ in valid)
    sum_t = sum(t for _, t in valid)
    headline = 100.0 * (sum_b - sum_t) / sum_b

    n = len(valid)
    if n < 2:
        # A single prompt carries no resampling information: report the
        # point estimate with a degenerate interval rather than crash.
        return {"mean_reduction_pct": headline, "ci95": 0.0,
                "ci95_interval": [headline, headline]}

    rng = random.Random(_BOOTSTRAP_SEED)
    boot: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = rng.choices(valid, k=n)
        sb = sum(b for b, _ in sample)
        st = sum(t for _, t in sample)
        boot.append(100.0 * (sb - st) / sb)
    boot.sort()
    lo = boot[int(0.025 * BOOTSTRAP_RESAMPLES)]
    hi = boot[min(int(0.975 * BOOTSTRAP_RESAMPLES), BOOTSTRAP_RESAMPLES - 1)]
    ci95 = max(headline - lo, hi - headline)
    return {"mean_reduction_pct": headline, "ci95": ci95,
            "ci95_interval": [lo, hi]}
