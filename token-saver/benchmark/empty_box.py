"""Empty-box calibration for the corrected P1 estimator (AC-P1a mandatory gate).

WHY THIS EXISTS (the audit's exact failure mode, 2026-09):
The audited P1-1 harness used a mean-of-per-prompt-ratios estimator with one
sample per arm, which collapses to ~0% (and can report negative savings) under
realistic output-length CV regardless of the true effect — it COULD NOT
distinguish "no effect" from "~17% effect". Before any provider dollar is spent
on a re-run, this script proves the corrected estimator can read the truth when
the truth is KNOWN by construction:

  - EMPTY BOX (null effect):   treatment tokens drawn from the SAME distribution
                               as baseline. A healthy instrument reports ~0%
                               reduction with a 95% CI that CONTAINS 0.
  - POSITIVE CONTROL (15%):    treatment tokens drawn at 85% of baseline.
                               The instrument must report ~15% with a 95% CI
                               that EXCLUDES 0.

Design follows AC-P1a exactly: ratio-of-sums headline, k >= 5 samples per arm,
temperature pinned (simulated single temperature), bootstrap CI over paired
per-prompt ratios, and per-prompt token variability seeded from the measured
CV in results/sd_gate_z-ai__glm-5.3-flash.json (CV = 0.2415 — the C2 gate
already on file). Synthetic, deterministic, offline, sub-second: no network, no
API key, no upstream model.

Exit code 0 = both gates green (the corrected estimator is calibrated);
exit code 1 = at least one gate red (do NOT spend provider money on a re-run).
"""
from __future__ import annotations

import math
import random
import statistics
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Measured inputs (committed artifact — change only with a new SD gate run)
# --------------------------------------------------------------------------
MEASURED_CV = 0.2415309851520115   # sd_gate z-ai/glm-5.3-flash, n=5, temp=0.0
MEASURED_MEAN_TOKENS = 3937.4      # same artifact, provider usage.completion_tokens
EFFECT_15PCT = 0.85                # treatment multiplier for the positive control
K_SAMPLES = 5                      # AC-P1a: k >= 5 per arm per prompt
N_PROMPTS = 40                     # matches the pinned fixture-set size (AC-P1)
BOOTSTRAP_REPS = 2000
SEED = 20260916                    # deterministic across runs and machines
TOLERANCE_PCT = 5.0                # null gate: |estimate| <= 5pp
EFFECT_WINDOW = (12.0, 18.0)       # control gate: estimate inside [12, 18]pp


def _lognormal_params(mean: float, cv: float) -> tuple[float, float]:
    """mu, sigma for lognormal with the given mean and coefficient of variation."""
    sigma = math.sqrt(math.log(1.0 + cv * cv))
    mu = math.log(mean) - 0.5 * sigma * sigma
    return mu, sigma


def draw_tokens(rng: random.Random, mean: float, cv: float) -> float:
    """One output-token draw (float; used in sums, which is what billing sees)."""
    mu, sigma = _lognormal_params(mean, cv)
    return math.exp(rng.gauss(mu, sigma))


def sample_arm(rng: random.Random, base_means: list[float], multiplier: float,
               cv: float, k: int) -> list[float]:
    """k draws per prompt at the given multiplier; returns per-prompt sums."""
    sums = []
    for base in base_means:
        s = 0.0
        for _ in range(k):
            s += draw_tokens(rng, max(base * multiplier, 1.0), cv)
        sums.append(s)
    return sums


def ratio_of_sums(treatment: list[float], baseline: list[float]) -> float:
    """Headline metric (AC-P1): Sigma_treatment / Sigma_baseline."""
    return sum(treatment) / sum(baseline)


def pct_reduction(treatment: list[float], baseline: list[float]) -> float:
    return (1.0 - ratio_of_sums(treatment, baseline)) * 100.0


def bootstrap_ci(treatment: list[float], baseline: list[float], reps: int,
                 rng: random.Random) -> tuple[float, float]:
    """Percentile bootstrap CI over PROMPTS (the independent units).

    Each resample picks prompts with replacement, recomputes the pooled
    ratio-of-sums, and yields a reduction. 2.5% / 97.5% percentiles = 95% CI.
    """
    n = len(treatment)
    reductions = []
    for _ in range(reps):
        idx = [rng.randrange(n) for _ in range(n)]
        t = [treatment[i] for i in idx]
        b = [baseline[i] for i in idx]
        reductions.append(pct_reduction(t, b))
    reductions.sort()
    lo = reductions[int(0.025 * reps)]
    hi = reductions[int(0.975 * reps)]
    return lo, hi


def simulate(cv: float, k: int, n_prompts: int, rng: random.Random,
             effect: float | None) -> dict:
    """One simulated experiment. effect=None => empty box; else treatment = effect*base."""
    rng = random.Random(rng.randrange(1 << 63))  # child stream, reproducible from SEED
    # Across-prompt baseline lengths vary (different prompts, different lengths):
    # lognormal spread so the pooled estimator must work under realistic mixes.
    mu_b, sigma_b = _lognormal_params(MEASURED_MEAN_TOKENS, cv * 2.0)
    base_means = [math.exp(rng.gauss(mu_b, sigma_b)) for _ in range(n_prompts)]
    baseline = sample_arm(rng, base_means, 1.0, cv, k)
    mult = effect if effect is not None else 1.0
    treatment = sample_arm(rng, base_means, mult, cv, k)
    est = pct_reduction(treatment, baseline)
    lo, hi = bootstrap_ci(treatment, baseline, BOOTSTRAP_REPS, rng)
    return {"estimate_pct": est, "ci_lo": lo, "ci_hi": hi,
            "baseline_total": sum(baseline), "treatment_total": sum(treatment)}


def main() -> int:
    rng = random.Random(SEED)
    failures: list[str] = []

    # ---- gate 1: empty box (null effect) ----
    null = simulate(MEASURED_CV, K_SAMPLES, N_PROMPTS, rng, effect=None)
    contains_zero = null["ci_lo"] <= 0.0 <= null["ci_hi"]
    small_est = abs(null["estimate_pct"]) <= TOLERANCE_PCT
    null_pass = contains_zero and small_est
    if not null_pass:
        failures.append(
            f"EMPTY BOX: estimate {null['estimate_pct']:+.2f}pp, "
            f"95% CI [{null['ci_lo']:+.2f}, {null['ci_hi']:+.2f}] — an "
            f"instrument that reports savings on a null effect is broken.")

    # ---- gate 2: positive control (true 15% effect) ----
    ctrl = simulate(MEASURED_CV, K_SAMPLES, N_PROMPTS, rng, effect=EFFECT_15PCT)
    in_window = EFFECT_WINDOW[0] <= ctrl["estimate_pct"] <= EFFECT_WINDOW[1]
    excludes_zero = ctrl["ci_lo"] > 0.0
    ctrl_pass = in_window and excludes_zero
    if not ctrl_pass:
        failures.append(
            f"POSITIVE CONTROL: estimate {ctrl['estimate_pct']:.2f}pp, "
            f"95% CI [{ctrl['ci_lo']:.2f}, {ctrl['ci_hi']:.2f}] — the estimator "
            f"cannot resolve a true 15% effect at k={K_SAMPLES}/arm (the "
            f"audit's 'could not distinguish no effect from ~17% effect').")

    print(f"Empty-box calibration  |  k={K_SAMPLES}/arm, {N_PROMPTS} prompts, "
          f"CV={MEASURED_CV:.4f} (measured), seed={SEED}, bootstrap={BOOTSTRAP_REPS}")
    print(f"  empty box (null):     {null['estimate_pct']:+6.2f}pp  "
          f"CI [{null['ci_lo']:+6.2f}, {null['ci_hi']:+6.2f}]  "
          f"{'PASS' if null_pass else 'FAIL'}")
    print(f"  positive control:     {ctrl['estimate_pct']:+6.2f}pp  "
          f"CI [{ctrl['ci_lo']:+6.2f}, {ctrl['ci_hi']:+6.2f}]  "
          f"{'PASS' if ctrl_pass else 'FAIL'}  (true effect: 15.00pp)")

    if failures:
        print("\nCALIBRATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("Do NOT run the P1-1 re-run on provider money until this is green.")
        return 1
    print("\nCALIBRATION PASSED — estimator reads ~0 on a null effect and "
          "resolves a true 15% effect. Re-run harness is cleared to spend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())