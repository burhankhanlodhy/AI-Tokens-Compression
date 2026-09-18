"""QA acceptance regressions for AC-P1b, AC-P1c, and AC-P1d.

These tests intentionally exercise the benchmark gate and the production
handler boundary. They pin the ratified publication semantics rather than
accepting an implementation that merely emits a percentage.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

TOKEN_SAVER = Path(__file__).resolve().parent.parent
BENCHMARK = TOKEN_SAVER / "benchmark"
sys.path.insert(0, str(TOKEN_SAVER))
sys.path.insert(0, str(BENCHMARK))

import empty_box  # noqa: E402
import run_benchmark  # noqa: E402
from proxy import caching, stats  # noqa: E402
from proxy.config import get_settings  # noqa: E402


# ---------------------------------------------------------------------------
# AC-P1b: the calibration gate must consume the exact interval it calibrates
# ---------------------------------------------------------------------------


def test_ac_p1b_empty_box_uses_estimator_ci95_interval(monkeypatch):
    """The gate must not rebuild a symmetric interval from the halfwidth.

    The runner publishes percentile bounds in ``ci95_interval``. An asymmetric
    fake makes accidental ``estimate +/- ci95`` reconstruction observable.
    """
    expected = {
        "mean_reduction_pct": 10.0,
        "ci95": 99.0,  # deliberately inconsistent with the exact bounds
        "ci95_interval": [8.0, 13.0],
    }
    monkeypatch.setattr(empty_box, "estimate", lambda _pairs: expected)

    result = empty_box._experiment(seed=0, multiplier=None, k=1)

    assert result["est"] == 10.0
    assert result["lo"] == 8.0
    assert result["hi"] == 13.0


class _FixedRng:
    def random(self):
        return 0.1  # force treatment into answer A


class _JudgeResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [{
                "message": {
                    "content": '{"score_a": 9, "score_b": 5, "winner": "a"}'
                }
            }]
        }


def test_ac_p1b_judge_runs_both_orders_and_averages(monkeypatch):
    """AC-P1b: the judge runs BOTH A/B orders per item and averages them.

    The fake judge awards ANSWER A 9 and ANSWER B 5 regardless of which
    answer occupies A — a maximal position bias. Single-order judging
    would return 5/9 or 9/5 depending on the seed; both-orders averaging
    must return 7/7 (tie), and the raw per-order scores stay in
    `judge_orders` for audit.
    """
    calls: list[str] = []

    def _fake_post(url, headers=None, json=None, timeout=None):
        assert json is not None
        content = json["messages"][0]["content"]
        which_a = "BASELINE" if "ANSWER A:\nbaseline answer" in content \
            else "TREATMENT"
        calls.append(which_a)

        class _R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content":
                        '{"score_a": 9, "score_b": 5, "winner": "a"}'}}]}

        return _R()

    monkeypatch.setattr(run_benchmark.httpx, "post", _fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    result = run_benchmark.rubric_score(
        baseline="baseline answer", treatment="treatment answer",
        question="What is the answer?", rng=_FixedRng(),
    )

    # Both orders ran, baseline-first then treatment-first.
    assert calls == ["BASELINE", "TREATMENT"]
    assert result["mode"] == "model_judge"
    assert [o["order"] for o in result["judge_orders"]] == \
        ["baseline_first", "treatment_first"]
    # Per-order raw scores are position-flipped (9/5 then 5/9 in the
    # baseline/treatment frame); the averages erase the bias.
    assert result["judge_orders"][0]["score_a"] == 9
    assert result["judge_orders"][0]["score_b"] == 5
    assert result["judge_orders"][1]["score_a"] == 5
    assert result["judge_orders"][1]["score_b"] == 9
    assert result["score_a"] == 7.0
    assert result["score_b"] == 7.0
    assert result["winner"] == "tie"


def test_ac_p1b_one_order_failure_excludes_item_from_parity(monkeypatch):
    """A failed mirror order must not silently ship one-order evidence."""
    state = {"calls": 0}

    def _fake_post(url, headers=None, json=None, timeout=None):
        state["calls"] += 1
        if state["calls"] == 2:  # the treatment_first order fails
            class _Bad:
                status_code = 429
            return _Bad()

        class _R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content":
                        '{"score_a": 8, "score_b": 8, "winner": "tie"}'}}]}

        return _R()

    monkeypatch.setattr(run_benchmark.httpx, "post", _fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    result = run_benchmark.rubric_score(
        baseline="baseline answer", treatment="treatment answer",
        question="What is the answer?", rng=None,
    )

    assert state["calls"] == 2
    assert result["mode"] == "judge_http_429"
    assert result["parity"] is None
    # mode != model_judge => summarize() excludes it from the parity
    # population (judged filter), so no one-order score reaches the gate.
    stats = run_benchmark.summarize([
        {"id": "p1", "eligible": True,
         "baseline_tokens": 100.0, "treatment_tokens": 50.0,
         "mode": result["mode"]},
    ])
    assert stats["quality_parity"]["n_judged"] == 0
    assert stats["quality_parity"]["parity_holds"] is False


def _judged_entry(score_a, score_b):
    return {"id": f"p{score_a}-{score_b}", "eligible": True,
            "baseline_tokens": 100.0, "treatment_tokens": 50.0,
            "mode": "model_judge",
            "score_a": score_a, "score_b": score_b, "winner": "a"}


def test_ac_p1b_parity_gate_is_mean_regression_le_1pt_not_zero_items():
    """AC-P1b ratified rule: parity uses the <=1pt MEAN regression.

    Deltas (score_a - score_b): five items at +4 (regress >1pt), five at
    0, five at -1. Mean regression = (20 - 5) / 15 = 1.00pt exactly — the
    spec's rule PASSES at exactly zero margin, while the stricter
    zero-items-over-1pt rule FAILS. The shipped zero-item rule failed
    P1-1 on precisely this shape (PM recompute 2026-09-18).
    """
    entries = (
        [_judged_entry(8, 4)] * 5    # each -4 regression over 1pt
        + [_judged_entry(9, 9)] * 5  # 0
        + [_judged_entry(9, 10)] * 5  # -1 (treatment better by 1pt)
    )
    stats = run_benchmark.summarize(entries)
    qp = stats["quality_parity"]
    assert qp["n_judged"] == 15
    assert qp["n_regressions_over_1pt"] == 5          # diagnostic, not gate
    assert qp["mean_regression_pt"] == 1.0
    assert qp["parity_rule"] == "mean_regression_le_1pt"
    assert qp["parity_holds"] is True


def test_ac_p1b_parity_gate_fails_when_mean_regression_exceeds_1pt():
    """Mean regression strictly above 1pt must fail the gate."""
    entries = [_judged_entry(9, 4)] * 8 + [_judged_entry(9, 9)] * 7
    stats = run_benchmark.summarize(entries)
    qp = stats["quality_parity"]
    # mean regression = (5*8 + 0*7)/15 = 2.67 > 1pt
    assert qp["mean_regression_pt"] == 2.67
    assert qp["parity_holds"] is False


# ---------------------------------------------------------------------------
# AC-P1c: at or below the 3pp publication floor is not a publishable savings
# percentage (floor = MEASURED calibrator blind-spot width, PM B4 2026-09-17)
# ---------------------------------------------------------------------------


def test_ac_p1c_sub_two_point_headline_is_no_measurable_effect():
    """A small headline must be explicitly withheld as a savings claim."""
    stats_result = run_benchmark.summarize([
        {"id": "eligible", "eligible": True,
         "baseline_tokens": 10000.0, "treatment_tokens": 9850.0},
        {"id": "ineligible", "eligible": False,
         "baseline_tokens": 100.0, "treatment_tokens": 100.0},
    ])

    headline = stats_result["headline"]
    assert headline["mean_output_reduction_pct"] == 1.5
    assert headline["publication_status"] == "no_measurable_effect"
    assert headline["reported_reduction_pct"] is None
    assert "3pp" in headline["publication_note"]


# ---------------------------------------------------------------------------
# AC-P1d: cache key remains stable when the production toggle changes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def p1d_client(tmp_path, monkeypatch):
    """ASGI client with SQLite ledger and no inherited Postgres redirection."""
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.delenv("TOKEN_SAVER_PG_BASE", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "p1d.db"))
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")
    monkeypatch.setenv("L1_ENABLED", "false")
    get_settings.cache_clear()
    stats.init_db()

    from proxy.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ac_p1d_cache_hit_survives_conciseness_toggle(
    p1d_client, monkeypatch
):
    """The real handler must keep one cache key across baseline/treatment."""
    from proxy import main as main_module

    raw_body = {
        "model": "openrouter/gpt-4o-mini",
        "messages": [{
            "role": "user",
            "content": "Please produce a detailed analysis of this request. "
                       + "Include the relevant reasoning and practical steps. " * 40,
        }],
    }
    upstream_bodies: list[dict] = []
    cache_keys: list[str] = []

    async def fake_forward(request, model, payload, stream):
        upstream_bodies.append(json.loads(payload))
        return httpx.Response(200, json={
            "id": "p1d", "model": model,
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                      "total_tokens": 7},
        }), "openrouter"

    def fake_lookup(provider, model, body):
        cache_keys.append(caching.cache_key(
            caching.canonical_prefix(body), model, provider))
        return "existing-entry" if len(cache_keys) == 2 else None

    monkeypatch.setattr(main_module, "compress_messages",
                        lambda messages: messages)
    monkeypatch.setattr(main_module, "_forward_routed", fake_forward)
    monkeypatch.setattr(main_module.caching, "lookup", fake_lookup)
    monkeypatch.setattr(main_module.caching, "record", lambda *args: None)

    common = {"Authorization": "Bearer test-key"}
    first = await p1d_client.post(
        "/v1/chat/completions",
        headers={**common, "X-Token-Saver-Conciseness": "0"},
        json=raw_body,
    )
    second = await p1d_client.post(
        "/v1/chat/completions",
        headers={**common, "X-Token-Saver-Conciseness": "1"},
        json=raw_body,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(cache_keys) == 2
    assert cache_keys[0] == cache_keys[1]
    assert len(upstream_bodies) == 2
    assert upstream_bodies[0]["messages"] != upstream_bodies[1]["messages"]
    assert upstream_bodies[0]["messages"][-1] == raw_body["messages"][0]
    assert upstream_bodies[1]["messages"][0]["role"] == "system"
    assert "concisely" in upstream_bodies[1]["messages"][0]["content"]
