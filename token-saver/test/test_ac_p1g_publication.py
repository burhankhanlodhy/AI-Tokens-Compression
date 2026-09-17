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

import json
import random
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


# ---------------------------------------------------------------------------
# C-7b: blended weights are per-call normalized (mode invariant)
# ---------------------------------------------------------------------------


def _arm(total, n_ok, k, sampled=True, ok=True, text="answer text"):
    """A run_arm()-shaped output: tokens_total is the RAW k-sample sum."""
    return {"ok": ok, "n_ok": n_ok, "k": k, "sampled": sampled,
            "tokens_total": total, "text": text,
            "tokens_source": "usage.completion_tokens", "error": None}


def _prompt(pid):
    return {"id": pid, "category": "qa",
            "messages": [{"role": "user", "content": f"question {pid}?"}]}


def test_c7b_blended_is_mode_invariant(monkeypatch):
    """THE invariant: eligible-only blended == --full-corpus blended on the
    same effect. Built through the shipped entry construction from the same
    per-call samples: 15 eligible prompts at a true 15% effect (k=30 both
    arms), 40 ineligible zero-effect prompts (byte-identical arms — full-k
    in full-corpus mode, single-pass baseline + derived treatment in
    eligible-only mode). Before C-7b the mixed sampling depths weighted the
    zero-effect prompts at 1/30th of their true traffic and over-reported
    the blended figure ~3.4x."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rng = random.Random(20260916)
    eligible_only, full_corpus = [], []
    for i in range(15):
        base_calls = [800.0 + rng.uniform(0, 400) for _ in range(30)]
        treat_calls = [b * 0.85 for b in base_calls]  # true 15% effect
        p = _prompt(f"elig{i}")
        eligible_only.append(run_benchmark.entry_from_arms(
            p, True,
            _arm(sum(base_calls), 30, 30), _arm(sum(treat_calls), 30, 30)))
        full_corpus.append(run_benchmark.entry_from_arms(
            p, True,
            _arm(sum(base_calls), 30, 30), _arm(sum(treat_calls), 30, 30)))
    for i in range(40):
        per_call = 500.0 + rng.uniform(0, 400)
        p = _prompt(f"in Elig{i}".replace(" ", "_"))
        # eligible-only: single-pass baseline, treatment NOT sampled
        eligible_only.append(run_benchmark.entry_from_arms(
            p, False, _arm(per_call, 1, 1),
            _arm(0, 0, 0, sampled=False)))
        # full-corpus: byte-identical arms sampled at full k
        full_corpus.append(run_benchmark.entry_from_arms(
            p, False, _arm(30 * per_call, 30, 30),
            _arm(30 * per_call, 30, 30)))

    s_elig = run_benchmark.summarize(eligible_only)
    s_full = run_benchmark.summarize(full_corpus)
    assert (s_elig["blended_corpus_wide"]["mean_output_reduction_pct"]
            == s_full["blended_corpus_wide"]["mean_output_reduction_pct"])
    assert (s_elig["headline"]["mean_output_reduction_pct"]
            == s_full["headline"]["mean_output_reduction_pct"])
    # and the mode-invariant figure is NOT the mixed-depth over-report:
    # rebuilding the same corpus with RAW sums (pre-C-7b shape) differs
    raw = [{"id": e["id"], "eligible": e["eligible"],
            "baseline_tokens": e["baseline_tokens_total"],
            "treatment_tokens": e["treatment_tokens_total"],
            "treatment_sampled": e["treatment_sampled"]}
           for e in eligible_only]
    buggy = run_benchmark.summarize(raw)["blended_corpus_wide"]
    correct = s_elig["blended_corpus_wide"]["mean_output_reduction_pct"]
    assert buggy["mean_output_reduction_pct"] > correct  # the 3.4x trap


def test_c7b_entry_tokens_are_percall_means_with_raw_totals_kept():
    """Each arm is normalized by its OWN n_ok; raw totals stay beside for
    audit; an unsampled arm (n_ok=0) does not divide by zero."""
    base = _arm(3000.0, 30, 30)
    treat = _arm(2550.0, 30, 30)
    e = run_benchmark.entry_from_arms(_prompt("x"), True, base, treat)
    assert e["baseline_tokens"] == 100.0
    assert e["treatment_tokens"] == 85.0
    assert e["baseline_tokens_total"] == 3000.0
    assert e["treatment_tokens_total"] == 2550.0
    unsampled = run_benchmark.entry_from_arms(
        _prompt("y"), False, _arm(750.0, 1, 1),
        _arm(0, 0, 0, sampled=False))
    assert unsampled["baseline_tokens"] == 750.0
    assert unsampled["treatment_tokens"] == 0.0
    assert unsampled["treatment_sampled"] is False


# --- C-7c: the CLI print surface routes through the PUBLISHED fields -----

def test_ac_p1g_cli_figure_suppresses_raw_percentage():
    """The figure renderer prints the publication contract, not the raw
    mean: a suppressed result renders status + note and never the
    percentage the JSON withheld."""
    hl = run_benchmark.summarize(_sub2pp_fixture())["headline"]
    assert hl["reported_reduction_pct"] is None
    line = run_benchmark._published_figure(hl)
    assert "no measurable effect" in line
    assert f"{hl['mean_output_reduction_pct']:.2f}%" not in line
    assert "2pp" in line  # the suppression reason ships with it


def test_ac_p1g_cli_figure_prints_published_pct_when_measurable():
    """Suppression guards noise, not signal: a measurable figure (>= 2pp,
    CI excluding 0) still renders the honest percent, taken verbatim from
    reported_reduction_pct."""
    entries = [
        {"id": "a", "eligible": True,
         "baseline_tokens": 10000.0, "treatment_tokens": 8300.0},  # 17%
        {"id": "b", "eligible": True,
         "baseline_tokens": 9000.0, "treatment_tokens": 7650.0},   # 15%
    ]
    hl = run_benchmark.summarize(entries)["headline"]
    assert hl["publication_status"] == "measurable_reduction"
    line = run_benchmark._published_figure(hl)
    assert f"{hl['reported_reduction_pct']:.2f}%" in line


def test_c7c_cli_print_routes_through_publication_fields(
        monkeypatch, tmp_path, capsys):
    """QA's C-7c probe, pinned end-to-end through main(): at a true 1%
    effect the results JSON publishes reported_reduction_pct=null for BOTH
    headline and blended — the CLI must print that same contract, never a
    bare 'HEADLINE ... 1.00%' / 'Blended ... 0.27%'."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(sys, "argv",
                        ["run_benchmark", "--out", str(tmp_path)])
    monkeypatch.setattr(run_benchmark, "fixture_checksum",
                        lambda: run_benchmark.EXPECTED_FIXTURE_SHA256)
    monkeypatch.setattr(run_benchmark.httpx, "get",
                        lambda *a, **k: type("R", (), {"status_code": 200})())
    monkeypatch.setattr(run_benchmark.httpx, "Client", lambda: object())

    def _arm(client, base, model, p, conciseness, k):
        per = 99.0 if conciseness else 100.0  # true 1% effect
        return {"ok": True, "n_ok": k, "k": k, "sampled": k > 0,
                "tokens_total": per * k, "text": "x",
                "tokens_source": "usage.completion_tokens", "error": None}

    monkeypatch.setattr(run_benchmark, "run_arm", _arm)
    monkeypatch.setattr(
        run_benchmark, "rubric_score",
        lambda *a, **k: {"mode": "model_judge", "score_a": 10,
                         "score_b": 10, "winner": "tie"})

    assert run_benchmark.main() == 0
    out = capsys.readouterr().out
    summary = json.loads(next(tmp_path.glob("benchmark_*.json")).read_text())

    for section, marker in (("headline", "HEADLINE"),
                            ("blended_corpus_wide", "Blended")):
        pub = summary[section]
        assert pub["reported_reduction_pct"] is None
        line = next(l for l in out.splitlines() if marker in l)
        # the printed line carries the suppression, never the raw mean
        assert "no measurable effect" in line
        assert f"{pub['mean_output_reduction_pct']:.2f}%" not in line
    # QA's exact observed leak, pinned as gone
    assert "1.00%" not in out
    assert "0.27%" not in out
