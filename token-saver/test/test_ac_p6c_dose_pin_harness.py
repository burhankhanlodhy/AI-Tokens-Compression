"""AC-P6c(2)/AC-P6f harness-side dose-pin wiring (PM blocker ruling,
2026-09-18): the pin must leave the harness or a calibration run silently
measures treatment == baseline.

Three defects pinned here, all found in pre-run re-verification of HEAD
fcde96d:
1. run_one/run_arm accepted dose_pin= but main()'s run_arm call sites
   passed nothing and no --dose-pin flag existed — the pin never left the
   harness.
2. --emit-calibration would happily write a calibration band artifact
   from an unpinned run (grounded fidelity-critical fixtures resolve to
   tier "none", band ~0pp) that /api/tripwire would consume as calibrated
   truth.
3. The artifact's claimed tier (--calibration-tier) had no enforced
   relationship to the tier actually pinned (--dose-pin).

The refusal tests assert refusal BEFORE any spend: no results file, no
calibration artifact, non-zero exit.
"""
from __future__ import annotations

import json
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


def _ok_arm_factory(calls: list):
    """Stub run_arm that records every call's kwargs and returns a healthy
    arm (k samples, 100 tokens each)."""
    def stub(client, base_url, model, prompt, conciseness, k=1,
             dose_pin=None):
        calls.append({"conciseness": conciseness, "k": k,
                      "dose_pin": dose_pin})
        return {"ok": True, "n_ok": k, "k": k, "sampled": True,
                "tokens_total": 100 * k, "text": "x",
                "tokens_source": "usage.completion_tokens", "error": None}
    return stub


@pytest.fixture()
def offline_main(monkeypatch, tmp_path):
    """Make main() runnable offline: pinned checksum, alive proxy, stubbed
    arms and judge, tmp results dir. Returns (argv_setter, calls, out)."""
    calls: list = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(rb, "fixture_checksum",
                        lambda: rb.EXPECTED_FIXTURE_SHA256)
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: type("R", (), {"status_code": 200})())
    monkeypatch.setattr(rb, "run_arm", _ok_arm_factory(calls))
    monkeypatch.setattr(rb, "_judge_once",
                        lambda *a, **k: {"score_a": 7, "score_b": 7,
                                         "winner": "tie"})
    out = tmp_path / "results"

    def set_argv(*extra):
        monkeypatch.setattr(sys, "argv",
                            ["run_benchmark.py", "--out", str(out), *extra])

    return set_argv, calls, out


def test_parser_exposes_dose_pin_flag_default_none():
    args = rb.build_parser().parse_args([])
    assert args.dose_pin is None, ("--dose-pin must exist and default to "
                                   "None: an unpinned run stays unpinned")
    args = rb.build_parser().parse_args(["--dose-pin", "bounded"])
    assert args.dose_pin == "bounded"


def test_main_passes_dose_pin_to_both_arms(offline_main):
    """The blocker itself: with --dose-pin given, EVERY run_arm call —
    baseline and treatment — must carry the pin."""
    set_argv, calls, out = offline_main
    set_argv("--dose-pin", "bounded")
    rc = rb.main()
    assert rc == 0
    assert calls, "stub run_arm was never called"
    assert all(c["dose_pin"] == "bounded" for c in calls), (
        f"pin did not reach every arm: {calls}")
    by_side = {c["conciseness"] for c in calls}
    assert by_side == {False, True}, "both arms must be exercised"
    # The artifact records the pin for audit.
    artifacts = list(out.glob("benchmark_*.json"))
    assert len(artifacts) == 1
    data = json.loads(artifacts[0].read_text())
    assert data.get("dose_pin") == "bounded"


def test_main_leaves_pin_unset_by_default(offline_main):
    """AC-P6c(2) harness side: without the flag, no pin header is sent
    (the proxy-side ignore-when-ALLOW_DOSE_PIN-off rule is pinned in
    test_ac_p6c_dose_pin.py)."""
    set_argv, calls, _ = offline_main
    set_argv()
    assert rb.main() == 0
    assert all(c["dose_pin"] is None for c in calls)


def test_emit_calibration_refuses_without_pin(offline_main, capsys):
    """--emit-calibration on an unpinned run would publish a ~0pp band as
    calibrated truth. Refuse before any spend."""
    set_argv, calls, out = offline_main
    set_argv("--emit-calibration")
    rc = rb.main()
    assert rc != 0
    assert calls == [], "refusal must happen BEFORE any provider call"
    assert not out.exists() or list(out.glob("*.json")) == []
    err = capsys.readouterr().err
    assert "requires --dose-pin" in err


def test_emit_calibration_refuses_pin_tier_mismatch(offline_main, capsys):
    """The artifact claims tier X; the run pinned tier Y. The tripwire
    would calibrate drift against a band the arm never measured."""
    set_argv, calls, out = offline_main
    set_argv("--emit-calibration", "--dose-pin", "full",
             "--calibration-tier", "bounded")
    rc = rb.main()
    assert rc != 0
    assert calls == [], "refusal must happen BEFORE any provider call"
    assert not out.exists() or list(out.glob("*.json")) == []
    err = capsys.readouterr().err
    assert "mismatch" in err and "full" in err and "bounded" in err


def test_emit_calibration_runs_with_matching_pin(offline_main):
    """Guard the guard: pin == claimed tier is the sanctioned calibration
    shape — it must proceed and write the AC-P6f artifact stamped with the
    pinned tier and the output-token metric."""
    set_argv, calls, out = offline_main
    set_argv("--emit-calibration", "--dose-pin", "bounded")
    rc = rb.main()
    assert rc == 0
    assert calls and all(c["dose_pin"] == "bounded" for c in calls)
    cal = list(out.glob("calibration_*.json"))
    assert len(cal) == 1, "matching pin must still emit the artifact"
    data = json.loads(cal[0].read_text())
    assert data["artifact_kind"] == "ac_p6c_calibration"
    assert data["tier"] == "bounded"
    assert data["metric"] == "output_tokens"
    src = json.loads((out / data["source_artifact"]).read_text())
    assert src["dose_pin"] == "bounded", (
        "calibration artifact must trace to a pinned source run")
