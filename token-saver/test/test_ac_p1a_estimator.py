"""C-4b acceptance tests: corrected estimator + wired sampling loop.

Covers the C-4b contract items as automated regressions:
  - AC-P1a: ratio-of-sums headline with bootstrap CI (estimator.py), shared
    by reference with the calibration gate (empty_box.py).
  - AC-P1b: pinned fixture checksum with fail-on-mismatch BEFORE any spend;
    temperature pinned on every request; output tokens from
    usage.completion_tokens (fallback recorded).
  - HARNESS_K wired: the runner demonstrably issues k calls per arm and
    aggregates them the way the gate simulates.
  - PM subset-headline ruling: headline over the eligible subset only,
    blended corpus-wide figure labelled beside it, never alone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

import estimator  # noqa: E402
import empty_box  # noqa: E402
import run_benchmark  # noqa: E402
from estimator import estimate  # noqa: E402


# ---------------------------------------------------------------------------
# Shared-by-reference guarantee: the gate calibrates exactly the shipped math
# ---------------------------------------------------------------------------

def test_ac_p1a_gate_imports_harness_estimator_by_reference():
    """empty_box must run run_benchmark.estimate, not a local re-implementation."""
    assert empty_box.estimate is run_benchmark.estimate
    assert run_benchmark.estimate is estimator.estimate


def test_ac_p1a_harness_k_wired_into_gate_and_harness():
    """HARNESS_K is a real sampling parameter: the gate defaults to it and
    the harness loop defaults to it (same symbol, value >= 30 per ruling)."""
    assert estimator.HARNESS_K >= 30
    assert run_benchmark.HARNESS_K is estimator.HARNESS_K


# ---------------------------------------------------------------------------
# Estimator mathematics (AC-P1a)
# ---------------------------------------------------------------------------

def test_ac_p1a_headline_is_ratio_of_sums_not_mean_of_ratios():
    # One huge prompt at 50% + one tiny prompt at 10%: ratio-of-sums = 46.36,
    # the condemned mean-of-ratios would read 30.0.
    pairs = [(1000.0, 500.0), (100.0, 90.0)]
    est = estimate(pairs)
    assert abs(est["mean_reduction_pct"] - 100 * 510 / 1100) < 1e-9


def test_ac_p1a_null_reads_zero_with_ci_containing_zero():
    est = estimate([(100.0 + i, 100.0 + i) for i in range(15)])
    assert est["mean_reduction_pct"] == 0.0
    lo, hi = est["ci95_interval"]
    assert lo <= 0.0 <= hi


def test_ac_p1a_resolves_true_effect_with_ci_excluding_zero():
    # Deterministic 15% effect on every prompt: headline must read 15 with
    # the CI excluding 0 (no noise, so the interval must be tight).
    pairs = [(1000.0 + 10 * i, 0.85 * (1000.0 + 10 * i)) for i in range(15)]
    est = estimate(pairs)
    assert abs(est["mean_reduction_pct"] - 15.0) < 0.5
    lo, hi = est["ci95_interval"]
    assert lo > 0.0


def test_ac_p1a_estimate_is_deterministic_for_reproducible_results():
    pairs = [(100.0 * (1 + 0.01 * i), 90.0 * (1 + 0.01 * i)) for i in range(15)]
    first, second = estimate(pairs), estimate(pairs)
    assert first == second


def test_ac_p1a_empty_and_degenerate_inputs_do_not_crash():
    assert estimate([])["mean_reduction_pct"] == 0.0
    single = estimate([(100.0, 80.0)])
    assert single["mean_reduction_pct"] == 20.0
    all_zero = estimate([(0.0, 0.0), (0.0, 0.0)])
    assert all_zero["mean_reduction_pct"] == 0.0


# ---------------------------------------------------------------------------
# Harness sampling loop: HARNESS_K demonstrably wired (runner issues k/arm)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, completion_tokens: int | None = 42):
        self.status_code = 200
        self._tokens = completion_tokens
        payload = {"choices": [{"message": {"content": "answer"}}]}
        if completion_tokens is not None:
            payload["usage"] = {"completion_tokens": completion_tokens}
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    @property
    def elapsed(self):
        class _E:
            def total_seconds(self):
                return 0.01
        return _E()


class _FakeClient:
    """Counts requests; hands each caller a fresh 200 with usage tokens."""

    def __init__(self, completion_tokens: int | None = 42):
        self.calls: list[dict] = []
        self.completion_tokens = completion_tokens

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json, "headers": headers})
        return _FakeResponse(self.completion_tokens)


PROMPT = {"id": "t-001", "category": "qa",
          "messages": [{"role": "user", "content": "x" * 500}]}


def test_ac_p1a_runner_issues_harness_k_calls_per_arm():
    client = _FakeClient()
    run_benchmark.run_arm(client, "http://x", "m", PROMPT, conciseness=False)
    assert len(client.calls) == estimator.HARNESS_K


def test_ac_p1a_runner_aggregates_k_samples_into_per_prompt_sums():
    client = _FakeClient(completion_tokens=7)
    arm = run_benchmark.run_arm(client, "http://x", "m", PROMPT,
                               conciseness=True, k=5)
    assert arm["ok"] is True
    assert arm["tokens_total"] == 5 * 7          # sum, not mean, not last
    assert arm["n_ok"] == 5 and arm["k"] == 5


def test_ac_p1a_partial_arm_is_a_failed_pair_not_silently_averaged(monkeypatch):
    monkeypatch.setattr(run_benchmark.time, "sleep", lambda _s: None)
    responses = [_FakeResponse(10), _FakeResponse(10), None, _FakeResponse(10)]
    calls = []

    def flaky_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        r = responses.pop(0)
        if r is None:  # every retry of the 3rd request keeps failing
            responses.insert(0, None)
            r = _FakeResponse(1)
            r.status_code = 500
            r.text = "boom"
        return r

    class _FlakyClient:
        post = staticmethod(flaky_post)

    arm = run_benchmark.run_arm(_FlakyClient(), "http://x", "m", PROMPT,
                               conciseness=True, k=3)
    assert len(calls) > 3            # the failed sample was retried
    assert arm["ok"] is False
    assert arm["n_ok"] < 3


# ---------------------------------------------------------------------------
# AC-P1b: usage.completion_tokens, pinned temperature
# ---------------------------------------------------------------------------

def test_ac_p1b_tokens_counted_from_usage_completion_tokens():
    client = _FakeClient(completion_tokens=321)
    sample = run_benchmark.run_one(client, "http://x", "m", PROMPT,
                                   conciseness=False)
    assert sample["tokens"] == 321
    assert sample["tokens_source"] == "usage.completion_tokens"


def test_ac_p1b_missing_usage_falls_back_and_records_the_fallback(monkeypatch):
    monkeypatch.setattr(run_benchmark, "count_output_tokens",
                        lambda text, model: 77)
    client = _FakeClient(completion_tokens=None)
    sample = run_benchmark.run_one(client, "http://x", "m", PROMPT,
                                   conciseness=True)
    assert sample["tokens"] == 77
    assert sample["tokens_source"] == "count_text_fallback"


def test_ac_p1b_temperature_is_pinned_on_every_request():
    client = _FakeClient()
    run_benchmark.run_one(client, "http://x", "m", PROMPT, conciseness=False)
    assert client.calls[0]["body"]["temperature"] == run_benchmark.TEMPERATURE
    assert run_benchmark.TEMPERATURE == 0.0


# ---------------------------------------------------------------------------
# AC-P1b: pinned corpus checksum, fail-on-mismatch BEFORE any spend
# ---------------------------------------------------------------------------

def test_ac_p1b_checksum_pin_matches_committed_corpus_v2():
    assert run_benchmark.fixture_checksum() == \
        run_benchmark.EXPECTED_FIXTURE_SHA256
    assert run_benchmark.EXPECTED_FIXTURE_SHA256 == (
        "e3fcde4d6862b97ec828bfb1e977fe12ff321d76b1f19a0c3a608c3f8cd154cd")


def test_ac_p1b_mismatched_corpus_aborts_before_spend(monkeypatch, tmp_path,
                                                      capsys):
    bad = tmp_path / "prompts.json"
    bad.write_text(json.dumps({"prompts": [{"id": "rogue"}]}))
    monkeypatch.setattr(run_benchmark, "FIXTURES", bad)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py"])  # keep argparse off pytest's argv
    rc = run_benchmark.main()
    assert rc == 1
    assert "checksum mismatch" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# PM subset-headline ruling: eligible-subset headline, labelled blended
# ---------------------------------------------------------------------------

def _entry(pid, eligible, b, t, judged=False):
    e = {"id": pid, "eligible": eligible, "baseline_tokens": b,
         "treatment_tokens": t}
    if judged:
        e.update({"mode": "model_judge", "score_a": 8, "score_b": 8})
    return e


def test_p1_headline_population_is_eligible_subset_only():
    stats = run_benchmark.summarize(
        [_entry("a", True, 1000.0, 850.0),    # eligible, exactly 15%
         _entry("b", False, 100.0, 100.0),    # ineligible, zero effect
         _entry("c", False, 100.0, 100.0)])
    hl = stats["headline"]
    bl = stats["blended_corpus_wide"]
    assert stats["headline_population"] == "eligible_subset"
    assert stats["n_eligible"] == 1
    assert abs(hl["mean_output_reduction_pct"] - 15.0) < 0.01  # NOT diluted
    assert hl["meets_15pct"] is True
    # blended is over ALL prompts and labelled as never-headline
    assert bl["n"] == 3
    assert abs(bl["mean_output_reduction_pct"] -
               100 * (1200.0 - 1050.0) / 1200.0) < 0.01
    assert "NOT the headline" in bl["label"]


def test_p1_corpus_v2_eligible_count_is_15_of_55():
    fixtures = json.loads((BENCHMARK / "prompts.json").read_text())
    prompts = fixtures["prompts"]
    assert len(prompts) == 55
    eligible = [p for p in prompts if run_benchmark.is_eligible(p)]
    assert len(eligible) == 15


# ---------------------------------------------------------------------------
# C-9: eligible-only spend ruling
# ---------------------------------------------------------------------------

def test_c9_sampling_plan_full_k_on_eligible_single_pass_on_ineligible():
    full = run_benchmark.sampling_plan(True, eligible_only=True)
    assert full == {"baseline_k": estimator.HARNESS_K,
                    "treatment_k": estimator.HARNESS_K, "judge": True}
    single = run_benchmark.sampling_plan(False, eligible_only=True)
    assert single == {"baseline_k": 1, "treatment_k": 0, "judge": False}
    # --full-corpus override restores every-prompt full-k
    override = run_benchmark.sampling_plan(False, eligible_only=False)
    assert override["baseline_k"] == estimator.HARNESS_K
    assert override["treatment_k"] == estimator.HARNESS_K


def test_c9_zero_k_arm_is_not_sampled():
    arm = run_benchmark.run_arm(_FakeClient(), "http://x", "m", PROMPT,
                               conciseness=True, k=0)
    assert arm["sampled"] is False and arm["ok"] is True
    assert arm["tokens_total"] == 0


def test_c9_eligible_only_is_the_default_mode_full_corpus_needs_override():
    ap = run_benchmark.build_parser()
    assert ap.parse_args([]).mode == "eligible_only"
    assert ap.parse_args(["--eligible-only"]).mode == "eligible_only"
    assert ap.parse_args(["--full-corpus"]).mode == "full_corpus"
    with pytest.raises(SystemExit):
        ap.parse_args(["--eligible-only", "--full-corpus"])


def test_c9_derived_blended_weights_zero_contribution_for_unsampled_arms():
    stats = run_benchmark.summarize(
        [{"id": "a", "eligible": True, "baseline_tokens": 1000.0,
          "treatment_tokens": 800.0, "treatment_sampled": True},
         {"id": "b", "eligible": False, "baseline_tokens": 100.0,
          "treatment_tokens": 0.0, "treatment_sampled": False},
         {"id": "c", "eligible": False, "baseline_tokens": 300.0,
          "treatment_tokens": 0.0, "treatment_sampled": False}])
    b = stats["blended_corpus_wide"]
    assert b["derived"] is True
    assert "DERIVED" in b["label"]
    # blended denominator weights are real baselines (1000+100+300);
    # unsampled arms contribute t := b, i.e. exactly 0pp
    assert abs(b["mean_output_reduction_pct"] - 100 * 200 / 1400) < 0.01
    # headline is untouched by the derived weights
    assert abs(stats["headline"]["mean_output_reduction_pct"] - 20.0) < 0.01


def test_c9_end_to_end_eligible_only_matches_ruled_budget(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """Integration: default mode on the real pinned corpus issues exactly
    15 x 2 x 30 + 40 x 1 = 940 completion calls and 15 judge calls — the
    ruled ~970 shape, not the 3,300 full-corpus shape."""
    calls = {"completions": 0, "treatment": 0, "judges": 0}

    class _CountingClient:
        def post(self, url, json=None, headers=None, timeout=None):
            calls["completions"] += 1
            if (headers or {}).get("X-Token-Saver-Conciseness") == "1":
                calls["treatment"] += 1
            return _FakeResponse(10)

    class _FakeHealth:
        status_code = 200

    monkeypatch.setattr(run_benchmark.httpx, "Client", _CountingClient)
    monkeypatch.setattr(run_benchmark.httpx, "get",
                        lambda *a, **k: _FakeHealth())

    def _fake_judge(baseline, treatment, question, rng):
        calls["judges"] += 1
        return {"mode": "model_judge", "score_a": 8, "score_b": 8,
                "winner": "tie"}

    monkeypatch.setattr(run_benchmark, "rubric_score", _fake_judge)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py",
                                      "--out", str(tmp_path)])

    assert run_benchmark.main() == 0
    assert calls["completions"] == 15 * 2 * estimator.HARNESS_K + 40
    assert calls["treatment"] == 15 * estimator.HARNESS_K
    assert calls["judges"] == 15
