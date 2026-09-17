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


def test_ac_p1b_randomized_judge_order_maps_back_to_baseline_treatment(monkeypatch):
    """A randomized A/B presentation must preserve the reference frame."""
    monkeypatch.setattr(run_benchmark.httpx, "post",
                        lambda *args, **kwargs: _JudgeResponse())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    result = run_benchmark.rubric_score(
        baseline="baseline answer", treatment="treatment answer",
        question="What is the answer?", rng=_FixedRng(),
    )

    # The fake judged treatment as A (9) and baseline as B (5); mapping back
    # must report baseline=5, treatment=9, winner=treatment ("b").
    assert result["judge_order_swapped"] is True
    assert result["score_a"] == 5
    assert result["score_b"] == 9
    assert result["winner"] == "b"


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
