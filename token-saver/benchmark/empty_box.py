"""Empty-box calibration for the P1-1 re-run (AC-P1a-gate).

Measures the PRODUCTION estimator — imported BY REFERENCE from the harness
(`run_benchmark.estimate`, backed by the shared `estimator.estimate`) —
never a local re-implementation. If the estimator is patched or the harness
stops routing through it, THIS GATE SEES IT: a gate that imports the
estimator cannot stay green while the thing it calibrates is sabotaged
(the defect class the 2026-09 audit caught in the first draft, proved by
patching `mean_reduction = 99.0` and watching the gate stay green).

What the gate asserts (two synthetic experiments with KNOWN truth, per
AC-P1a): 
  - EMPTY BOX (null effect):   both arms drawn from the SAME measured
    output-length distribution (CV=0.2415, on-file SD gate). A calibrated
    instrument reports ~0%; a false positive here = claims savings that
    are not there.
  - POSITIVE CONTROL (15%):    treatment drawn at 85% of baseline. A
    calibrated instrument must resolve it.

Gating contract (PM ruling, supersedes the single-seed check): run N
seeded replications and gate on the FAILURE RATE —
  - null false-positive rate  <= 5%   (|est| > 5pp AND 95% CI excludes 0 — a
    publishable false claim; the OR reading is unpassable by construction)
  - positive-control success  >= 90%  (est in [12,18] AND CI excludes 0)
A single seed can be lucky (the old gate was green only 75.7% of 300
seeds); the RATE is what "the instrument is calibrated" actually means.

Simulation mirrors the harness: HARNESS_K samples per arm per prompt
(estimator.py declares what the harness takes — 30 since C-4b), aggregated
into per-prompt (baseline_total, treatment_total) pairs, fed to `estimate()`.

N_PROMPTS = 15 per the PM subset-headline ruling (2026-09-16): the
published headline is measured over the ELIGIBLE subset — the prompts whose
last user message clears the production gate. Corpus v2 (7cae1b1) has 15/55
gate-clearing fixtures, so the headline instrument calibrates at n=15; the
corpus-wide blended figure is a dilution, published beside the headline and
labelled, and is NOT what this gate calibrates.

P4 (ratified carve, option (a) 2026-09-18): the PUBLISHED claim is the
non-code non-RAG carve at n=10, so the gate must ALSO clear at n=10 with
the RUN instrument's measured CV (gemini: 0.137,
`sd_gate_google__gemini-3.5-flash-lite.json` — the archived CV=0.2415
FAILS at n=10/k=30, control 89.0% < 90%). N_PROMPTS, CV and mean tokens
are therefore CLI parameters (`--n-prompts`, `--cv`, `--mean-tokens`);
the constants above are only their DEFAULTS. `--out` commits a JSON
artifact (params + rates + verdicts) — until that artifact exists at
N_PROMPTS=10/CV=0.137/k=30, the 57.71pp carve is uncalibrated and must
not be published.

HEAD STATUS: GREEN since C-4b. The corrected estimator (ratio-of-sums +
bootstrap, HARNESS_K=30) is what this gate imports and simulates; a red
exit here means the shipped math regressed — do NOT weaken thresholds to
force green.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from run_benchmark import estimate  # noqa: E402  — THE harness's estimator symbol
from estimator import HARNESS_K    # noqa: E402  — samples/arm the harness takes

# --------------------------------------------------------------------------
# Measured inputs (committed artifact — change only with a new SD gate run)
# --------------------------------------------------------------------------
MEASURED_CV = 0.2415309851520115   # sd_gate z-ai/glm-5.3-flash, n=5, temp=0.0
MEASURED_MEAN_TOKENS = 3937.4      # same artifact, provider usage.completion_tokens
EFFECT_15PCT = 0.85                # treatment multiplier for the positive control
# Headline population = eligible subset (PM subset-headline ruling): corpus
# v2 has 15/55 gate-clearing fixtures, so the headline instrument is n=15.
N_PROMPTS = 15
SEED_BASE = 20260916               # deterministic across runs and machines
NULL_TOL_PP = 5.0                  # null gate: |estimate| must stay within 5pp
CONTROL_WINDOW = (12.0, 18.0)      # control gate: estimate inside [12, 18]pp
NULL_FP_MAX = 0.05                 # rate contract: <= 5% false positives
CONTROL_MIN_SUCCESS = 0.90         # rate contract: >= 90% in-window successes


def _lognormal_params(mean: float, cv: float) -> tuple[float, float]:
    """mu, sigma for lognormal with the given mean and coefficient of variation."""
    sigma = math.sqrt(math.log(1.0 + cv * cv))
    mu = math.log(mean) - 0.5 * sigma * sigma
    return mu, sigma


def _draw(rng: random.Random, mean: float, cv: float) -> float:
    mu, sigma = _lognormal_params(mean, cv)
    return math.exp(rng.gauss(mu, sigma))


def _arm_totals(rng: random.Random, base_means: list[float],
                multiplier: float, cv: float, k: int) -> list[float]:
    """Per-prompt totals: k draws per prompt aggregated, exactly as the
    harness aggregates its HARNESS_K samples before calling estimate()."""
    totals = []
    for base in base_means:
        s = 0.0
        for _ in range(k):
            s += _draw(rng, max(base * multiplier, 1.0), cv)
        totals.append(s)
    return totals


def _experiment(seed: int, multiplier: float | None, k: int,
                n_prompts: int = N_PROMPTS,
                mean_tokens: float = MEASURED_MEAN_TOKENS,
                cv: float = MEASURED_CV) -> dict:
    rng = random.Random(SEED_BASE + seed)
    # Across-prompt baseline lengths vary (different prompts, different
    # lengths): lognormal spread so the estimator must work on a realistic mix.
    mu_b, sigma_b = _lognormal_params(mean_tokens, cv * 2.0)
    base_means = [math.exp(rng.gauss(mu_b, sigma_b)) for _ in range(n_prompts)]
    baseline = _arm_totals(rng, base_means, 1.0, cv, k)
    treatment = _arm_totals(rng, base_means,
                            multiplier if multiplier is not None else 1.0,
                            cv, k)
    e = estimate(list(zip(baseline, treatment)))
    # AC-P1b: consume the estimator's exact ci95_interval percentile bounds —
    # never rebuild a symmetric interval as est +/- ci95. An asymmetric
    # bootstrap interval is the honest one; the halfwidth is the conservative
    # symmetric summary for consumers, not the gate's read.
    lo, hi = e["ci95_interval"]
    return {"est": e["mean_reduction_pct"], "lo": lo, "hi": hi}


def _null_is_false_positive(r: dict) -> bool:
    """Null arm should read ~0. A harmful FP = the gate would have PUBLISHED
    a savings claim: the point estimate escapes the ±5pp tolerance AND the
    95% CI excludes 0 (a statistically significant claim of savings).

    NOT an OR. An exactly-calibrated two-sided 95% instrument excludes 0 on
    ~5% of nulls at EVERY k by construction (that is what "95%" means), so
    counting a bare CI exclusion as a false positive makes the rate contract
    (<= 5%) unpassable with margin at any sampling budget. The AND reading
    is the one the PM k-sweep reproduces (k=12 -> ~5.0%, k=20 -> ~1.1%,
    k=30 -> ~0.2-0.8%; measured at 400 seeds)."""
    return (abs(r["est"]) > NULL_TOL_PP
            and (r["lo"] > 0.0 or r["hi"] < 0.0))


def _control_is_success(r: dict) -> bool:
    """True 15% effect must be resolved inside the window with CI > 0."""
    in_window = CONTROL_WINDOW[0] <= r["est"] <= CONTROL_WINDOW[1]
    return in_window and r["lo"] > 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="AC-P1a empty-box calibration")
    ap.add_argument("--seeds", type=int, default=200,
                    help="seeded replications (rate contract needs ~200)")
    ap.add_argument("--k", type=int, default=HARNESS_K,
                    help=f"samples/arm/prompt to simulate (default: harness "
                         f"HARNESS_K={HARNESS_K})")
    # P4: the ratified carve headline is n=10 (non-code non-RAG), so the
    # gate must run at --n-prompts 10 with the run instrument's measured
    # CV (--cv 0.137 for the gemini instrument). Defaults preserve the
    # n=15 / archived-CV shape this gate historically cleared.
    ap.add_argument("--n-prompts", type=int, default=N_PROMPTS,
                    help=f"prompts per replication (default: {N_PROMPTS}, "
                         f"the PM subset-headline ruling; P4 uses 10)")
    ap.add_argument("--cv", type=float, default=MEASURED_CV,
                    help="measured output-length CV of the instrument "
                         f"(default: {MEASURED_CV:.4f}; gemini run uses "
                         "0.137)")
    ap.add_argument("--mean-tokens", type=float, default=MEASURED_MEAN_TOKENS,
                    help="measured mean completion tokens of the instrument")
    ap.add_argument("--cv-source", default=None,
                    help="label recorded in the artifact for where the CV "
                         "came from (e.g. the SD-gate artifact name)")
    ap.add_argument("--out", default=None,
                    help="write a JSON calibration artifact to this path "
                         "(P4: REQUIRED for the carve clearance evidence)")
    ap.add_argument("--show-seeds", action="store_true",
                    help="print per-seed estimates (debug)")
    args = ap.parse_args()

    if args.k < 1:
        print("--k must be >= 1", file=sys.stderr)
        return 2
    if args.n_prompts < 2:
        print("--n-prompts must be >= 2 (a single prompt carries no "
              "resampling information)", file=sys.stderr)
        return 2
    if args.cv <= 0:
        print("--cv must be > 0", file=sys.stderr)
        return 2

    null_fp, ctrl_ok = 0, 0
    worst_null_est, worst_ctrl_est = 0.0, 0.0
    for seed in range(args.seeds):
        nr = _experiment(seed, None, args.k,
                         n_prompts=args.n_prompts,
                         mean_tokens=args.mean_tokens, cv=args.cv)
        cr = _experiment(seed, EFFECT_15PCT, args.k,
                         n_prompts=args.n_prompts,
                         mean_tokens=args.mean_tokens, cv=args.cv)
        if _null_is_false_positive(nr):
            null_fp += 1
        if _control_is_success(cr):
            ctrl_ok += 1
        worst_null_est = min(worst_null_est, nr["est"])
        worst_ctrl_est = max(worst_ctrl_est, cr["est"])
        if args.show_seeds and seed % 25 == 0:
            print(f"  seed {seed:>3}: null {nr['est']:+6.2f}pp "
                  f"CI[{nr['lo']:+6.2f},{nr['hi']:+6.2f}] | "
                  f"ctrl {cr['est']:+6.2f}pp CI[{cr['lo']:+6.2f},{cr['hi']:+6.2f}]")

    null_fp_rate = null_fp / args.seeds
    ctrl_success_rate = ctrl_ok / args.seeds
    null_pass = null_fp_rate <= NULL_FP_MAX
    ctrl_pass = ctrl_success_rate >= CONTROL_MIN_SUCCESS
    green = null_pass and ctrl_pass

    print(f"\nAC-P1a empty-box calibration | estimator: run_benchmark.estimate "
          f"(shared, imported) | HARNESS_K={HARNESS_K} | sim k={args.k} | "
          f"n_prompts={args.n_prompts} | cv={args.cv:.4f} | "
          f"mean_tokens={args.mean_tokens:.1f} | seeds={args.seeds}")
    print(f"  null false-positive rate:  {null_fp_rate:6.1%}  "
          f"(contract <= {NULL_FP_MAX:.0%})  [worst estimate {worst_null_est:+.2f}pp]  "
          f"{'PASS' if null_pass else 'FAIL'}")
    print(f"  positive-control success:  {ctrl_success_rate:6.1%}  "
          f"(contract >= {CONTROL_MIN_SUCCESS:.0%})  [worst estimate {worst_ctrl_est:+.2f}pp]  "
          f"{'PASS' if ctrl_pass else 'FAIL'}")

    if args.out:
        from datetime import datetime, timezone
        artifact = {
            "schema": "empty_box_calibration_v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "estimator": "run_benchmark.estimate (shared, imported by "
                         "reference — the gate calibrates the shipped math)",
            "params": {
                "n_prompts": args.n_prompts,
                "cv": args.cv,
                "mean_tokens": args.mean_tokens,
                "k": args.k,
                "seeds": args.seeds,
                "null_tolerance_pp": NULL_TOL_PP,
                "control_window_pp": list(CONTROL_WINDOW),
                "null_fp_max": NULL_FP_MAX,
                "control_min_success": CONTROL_MIN_SUCCESS,
            },
            "cv_source": (args.cv_source
                          or "empty_box.py MEASURED_CV default (archived "
                             "SD gate)"),
            "results": {
                "null_false_positive_rate": null_fp_rate,
                "null_false_positives": null_fp,
                "control_success_rate": ctrl_success_rate,
                "control_successes": ctrl_ok,
                "worst_null_estimate_pp": worst_null_est,
                "worst_control_estimate_pp": worst_ctrl_est,
            },
            "verdicts": {
                "null_pass": null_pass,
                "control_pass": ctrl_pass,
                "calibrated": green,
            },
            "clearance": (
                f"{'CALIBRATED' if green else 'NOT CALIBRATED'} at "
                f"n_prompts={args.n_prompts}, cv={args.cv:.4f}, "
                f"k={args.k} over {args.seeds} seeds"),
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(artifact, indent=2))
        print(f"  artifact written: {out_path}")

    if green:
        print("\nCALIBRATED — estimator reads ~0 on a null effect and resolves "
              "a true 15% effect at the rates the contract demands. "
              "P1-1 re-run is cleared to spend.")
        return 0

    print("\nNOT CALIBRATED — no provider money on a P1-1 re-run.")
    if not green:
        print("The gate imports run_benchmark.estimate by reference, so a red "
              "result here means the SHIPPED math regressed (or the sampling "
              "regime diverged from HARNESS_K). Do NOT weaken thresholds to "
              "force green; fix the estimator or the harness loop.")
    return 1


if __name__ == "__main__":
    sys.exit(main())