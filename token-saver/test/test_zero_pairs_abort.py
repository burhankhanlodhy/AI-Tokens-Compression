"""Zero-pairs abort (PM ruling, P1-1 readiness): a benchmark run in which
NO pair survives must exit non-zero and write NO results file.

Why this exists: summarize([]) publishes publication_status =
no_measurable_effect with reported_reduction_pct = null — a total
connection failure (bad key, wrong UPSTREAM_BASE_URL, network down) would
serialize identically to a genuine null result. The first committed
benchmark run had 40/40 errors and still wrote a 0.0% results file. This
test pins the abort so that class of silent failure can never ship again.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(ROOT / "benchmark")):
    if p not in sys.path:
        sys.path.insert(0, p)

import run_benchmark as rb  # noqa: E402


def _failed_arm(client, base_url, model, prompt, conciseness, k=1,
                dose_pin=None):
    """Stub run_arm: every call fails the way a 401 from the wrong
    upstream does (run_one retries 3x then returns ok=False)."""
    return {"ok": False, "n_ok": 0, "k": k, "sampled": True,
            "tokens_total": 0, "text": "", "tokens_source": None,
            "error": "401 Unauthorized"}


def test_zero_valid_pairs_abort_no_file_nonzero_exit(tmp_path, monkeypatch,
                                                     capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    # Pin the checksum gate so the test exercises the abort, not the pin.
    monkeypatch.setattr(rb, "fixture_checksum",
                        lambda: rb.EXPECTED_FIXTURE_SHA256)
    # Health check passes (proxy alive) — the failure is downstream, at the
    # provider, which is exactly the silent case.
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: type("R", (), {"status_code": 200})())
    monkeypatch.setattr(rb, "run_arm", _failed_arm)

    out = tmp_path / "results"
    monkeypatch.setattr(sys, "argv",
                        ["run_benchmark.py", "--out", str(out)])
    rc = rb.main()

    assert rc != 0, "zero-surviving-pairs run must exit non-zero"
    # No results file may exist — not even a 0.0% one.
    assert not out.exists() or list(out.glob("*.json")) == []
    err = capsys.readouterr().err
    assert "0/" in err and "No results file written" in err


def test_some_valid_pairs_still_writes_results(tmp_path, monkeypatch,
                                               capsys):
    """Guard the guard: the abort must trigger ONLY when zero pairs
    survive, not suppress legitimate runs."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setattr(rb, "fixture_checksum",
                        lambda: rb.EXPECTED_FIXTURE_SHA256)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: type("R", (), {"status_code": 200})())

    def one_ok_arm(client, base_url, model, prompt, conciseness, k=1,
                   dose_pin=None):
        return {"ok": True, "n_ok": k, "k": k, "sampled": True,
                "tokens_total": 100 * k, "text": "x", "tokens_source": "tiktoken",
                "error": None}

    monkeypatch.setattr(rb, "run_arm", one_ok_arm)
    out = tmp_path / "results"
    monkeypatch.setattr(sys, "argv",
                        ["run_benchmark.py", "--out", str(out)])
    rc = rb.main()

    assert rc == 0
    files = list(out.glob("*.json"))
    assert len(files) == 1
    data = files[0].read_text()
    assert '"n_valid": ' in data  # a real summary, not the abort path
    capsys.readouterr()  # discard
