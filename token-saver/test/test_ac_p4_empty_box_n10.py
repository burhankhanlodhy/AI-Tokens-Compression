"""P4: empty-box gate at the carve population (N_PROMPTS=10, gemini CV).

The ratified option-(a) carve published the headline at n=10
(non-code non-RAG), but `benchmark/empty_box.py` still hardcoded
``N_PROMPTS = 15`` and the archived z-ai CV=0.2415 — which the spec's own
calibration caveat says FAILS at n=10/k=30 (control 89.0% < 90%). Until
the gate cleared at the carve population with the RUN instrument's
measured CV (gemini 0.137), the 57.71pp carve was uncalibrated and
unpublishable (PM board ruling, 2026-09-18).

These tests pin:
1. N_PROMPTS / CV / mean tokens are CLI parameters (constants are only
   defaults) — `_experiment` honors them;
2. the committed n=10 / CV=0.137 / k=30 calibration artifact is GREEN
   (null FP <= 5%, control >= 90%) and self-describes its params;
3. the archived CV at n=10 genuinely FAILS the control contract — the
   parameterization is load-bearing, not cosmetic;
4. the artifact validator rejects degenerate configs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

import empty_box  # noqa: E402
from estimator import HARNESS_K  # noqa: E402

ARTIFACT = (BENCHMARK / "results"
            / "empty_box_calibration_n10_gemini_cv0137_k30.json")


def test_ac_p4_n_prompts_is_a_parameter_not_a_constant():
    """_experiment honors n_prompts: the across-prompt spread is drawn
    per n, and the estimator sees exactly that many pairs."""
    r15 = empty_box._experiment(seed=0, multiplier=None, k=1,
                                n_prompts=15)
    r10 = empty_box._experiment(seed=0, multiplier=None, k=1,
                                n_prompts=10)
    assert r15["est"] != r10["est"] or r15["lo"] != r10["lo"]
    # and the module constant is unchanged for the ruled n=15 shape
    assert empty_box.N_PROMPTS == 15


def test_ac_p4_cv_is_a_parameter_not_a_constant():
    """A different CV changes the simulated sampling noise: the gate
    calibrates the RUN instrument, not the archived one."""
    r_arch = empty_box._experiment(seed=0, multiplier=None, k=1,
                                   n_prompts=10, cv=0.2415,
                                   mean_tokens=1050.6)
    r_gem = empty_box._experiment(seed=0, multiplier=None, k=1,
                                  n_prompts=10, cv=0.137,
                                  mean_tokens=1050.6)
    # identical seeds, different noise regime -> different resamples
    assert (r_arch["est"], r_arch["lo"], r_arch["hi"]) != \
           (r_gem["est"], r_gem["lo"], r_gem["hi"])


def test_ac_p4_n10_gemini_cv_artifact_is_committed_and_green():
    """THE P4 clearance evidence: the committed artifact must be the
    carve-population run (n=10, CV=0.137, k=30, 2000 seeds) and GREEN —
    until it exists, the 57.71pp carve is uncalibrated and must not be
    published (PM board ruling)."""
    assert ARTIFACT.exists(), (
        "the n=10 empty-box calibration artifact is missing — the carve "
        "is uncalibrated; re-run benchmark/empty_box.py --seeds 2000 "
        "--n-prompts 10 --cv 0.137 --k 30 --out ...")
    a = json.loads(ARTIFACT.read_text())
    assert a["schema"] == "empty_box_calibration_v1"
    p = a["params"]
    assert p["n_prompts"] == 10
    assert abs(p["cv"] - 0.137) < 1e-9
    assert p["k"] == HARNESS_K == 30
    assert p["seeds"] == 2000
    assert p["null_fp_max"] == 0.05
    assert p["control_min_success"] == 0.90
    v = a["verdicts"]
    assert v["null_pass"] is True
    assert v["control_pass"] is True
    assert v["calibrated"] is True
    r = a["results"]
    assert r["null_false_positive_rate"] <= 0.05
    assert r["control_success_rate"] >= 0.90
    # the estimator under calibration is the SHARED symbol, by reference
    assert "run_benchmark.estimate" in a["estimator"]


def test_ac_p4_archived_cv_genuinely_fails_at_n10_k30():
    """The caveat's load-bearing claim, verified: CV=0.2415 at n=10/k=30
    drops the positive-control success BELOW the 90% contract (89.0% over
    2000 seeds — the number the ratified caveat quotes) while the gemini
    CV=0.137 clears at 99.5%. This is WHY the gate had to be re-run with
    the run instrument's CV, and why a noisier instrument requires k>=40
    (spec, ratified 09fc87c). At short seed counts the rate sits ON the
    90% edge — the rate, not a lucky seed, is the contract."""
    seeds = 2000
    ctrl_ok = 0
    for seed in range(seeds):
        r = empty_box._experiment(seed, empty_box.EFFECT_15PCT,
                                  empty_box.HARNESS_K,
                                  n_prompts=10, cv=0.2415,
                                  mean_tokens=3937.4)
        if empty_box._control_is_success(r):
            ctrl_ok += 1
    rate = ctrl_ok / seeds
    assert rate < empty_box.CONTROL_MIN_SUCCESS, (
        "CV=0.2415 now clears at n=10/k=30 — the spec caveat is stale, "
        "re-measure before editing this test")


def test_ac_p4_degenerate_configs_are_rejected(monkeypatch, tmp_path):
    """The parameterized gate fails closed on nonsense configs instead of
    silently simulating them (exit 2, no artifact)."""
    for argv in (["empty_box.py", "--seeds", "2", "--n-prompts", "1"],
                 ["empty_box.py", "--seeds", "2", "--cv", "0"],
                 ["empty_box.py", "--seeds", "2", "--k", "0"]):
        out = tmp_path / "should_not_exist.json"
        monkeypatch.setattr(sys, "argv",
                            argv + (["--out", str(out)]
                                    if len(argv) > 2 else []))
        assert empty_box.main() == 2
        assert not out.exists()


def test_ac_p4_artifact_write_is_wired(monkeypatch, tmp_path):
    """A short parameterized run emits a self-describing artifact with the
    exact params it ran — the committed clearance artifact is produced by
    this same path, not hand-authored."""
    out = tmp_path / "cal.json"
    monkeypatch.setattr(sys, "argv", [
        "empty_box.py", "--seeds", "5", "--n-prompts", "4", "--cv", "0.137",
        "--mean-tokens", "1050.6", "--k", "5", "--out", str(out),
        "--cv-source", "unit-test"])
    assert empty_box.main() in (0, 1)   # verdict whatever the rates give
    a = json.loads(out.read_text())
    assert a["params"]["n_prompts"] == 4
    assert a["params"]["seeds"] == 5
    assert a["cv_source"] == "unit-test"
    assert a["verdicts"]["calibrated"] == (
        a["results"]["null_false_positive_rate"] <= 0.05
        and a["results"]["control_success_rate"] >= 0.90)
