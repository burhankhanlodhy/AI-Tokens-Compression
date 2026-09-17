"""P1-1: output-conciseness benchmark runner (AC-P1/P1a/P1b).

Design (per product-spec-v2.md):
- Fixture set: benchmark/prompts.json (>= 40 prompts, committed pre-run,
  mix >= 60% conversational/QA/RAG, <= 40% code) — AC-P1a anti-cherry-picking.
- Each prompt is sent TWICE through the live proxy route: once with
  OUTPUT_CONCISENESS_ENABLED=false (baseline), once =true (treatment).
- Output tokens counted identically on both sides via proxy.counting.
- Quality parity: model-based side-by-side pairwise judge (AC-P1) with a
  rubric score (structural correctness + answer fidelity, 1-10).
- Statistical claim: paired test at 95% CI (AC-P1a).
- Honesty gate (AC-P1b): results JSON records the exact fixture checksum,
  model, and config so QA can reproduce. The published headline must match
  the committed run.

Usage:
  OPENROUTER_API_KEY=sk-... .venv/bin/python benchmark/run_benchmark.py \
      --base-url http://localhost:8000 --model z-ai/glm-5.3-flash

Requires a running proxy (docker compose up) and a real API key in the env
of the CLIENT calls (BYOK passthrough). No results are fabricated: if the
proxy is unreachable the script exits non-zero with no results file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from estimator import estimate  # noqa: E402 — shared with the calibration gate

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "prompts.json"


def fixture_checksum() -> str:
    return hashlib.sha256(FIXTURES.read_bytes()).hexdigest()


def count_output_tokens(text: str, model: str) -> int:
    """Count via the proxy's own counter for identical treatment both sides."""
    sys.path.insert(0, str(ROOT.parent))
    from proxy.counting import count_text

    return count_text(text, model)


def extract_text(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def run_one(client: httpx.Client, base_url: str, model: str,
            prompt: dict, conciseness: bool) -> dict:
    body = {
        "model": model,
        "messages": prompt["messages"],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
        "X-Token-Saver-Conciseness": "1" if conciseness else "0",
    }
    last_err = None
    for attempt in range(3):
        try:
            r = client.post(f"{base_url}/v1/chat/completions",
                            json=body, headers=headers, timeout=120)
            if r.status_code == 200:
                text = extract_text(r.json())
                return {"ok": True, "text": text,
                        "tokens": count_output_tokens(text, model),
                        "latency_ms": r.elapsed.total_seconds() * 1000}
            last_err = f"status {r.status_code}: {r.text[:150]}"
        except httpx.HTTPError as exc:
            last_err = str(exc)
        time.sleep(2 ** attempt)
    return {"ok": False, "text": "", "tokens": 0, "error": last_err}


def rubric_score(baseline: str, treatment: str, question: str) -> dict:
    """Model-based pairwise judge (AC-P1). Returns parity + preference.

    Uses the same upstream model in judge mode via a direct (unproxied) call;
    falls back to a length-blind heuristic ONLY if no judge key is available —
    and records that fact so the honesty gate (AC-P1b) catches it.
    """
    judge_key = os.environ.get("OPENROUTER_API_KEY")
    judge_model = os.environ.get("BENCHMARK_JUDGE_MODEL", "openai/gpt-4o")
    if not judge_key:
        return {"mode": "no_judge_key", "parity": None, "winner": "unknown"}

    prompt = (
        "You are a strict evaluator. Compare two AI answers to the same "
        "question. Score each 1-10 on: structural correctness and answer "
        "fidelity (does it answer what was asked, accurately, without "
        "hallucination). Brevity is NOT rewarded; only correctness and "
        "completeness of the actual answer.\n\n"
        f"QUESTION: {question}\n\n"
        f"ANSWER A:\n{baseline[:4000]}\n\n"
        f"ANSWER B:\n{treatment[:4000]}\n\n"
        'Respond ONLY with JSON: {"score_a": <int>, "score_b": <int>, '
        '"winner": "a"|"b"|"tie"}'
    )
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {judge_key}"},
            json={"model": judge_model,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.status_code != 200:
            return {"mode": f"judge_http_{r.status_code}", "parity": None,
                    "winner": "unknown"}
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content[content.index("{"):content.rindex("}") + 1])
        return {"mode": "model_judge",
                "score_a": int(data["score_a"]), "score_b": int(data["score_b"]),
                "winner": data.get("winner", "tie")}
    except Exception as exc:  # noqa: BLE001
        return {"mode": f"judge_error: {exc}", "parity": None, "winner": "unknown"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--model", default="z-ai/glm-5.3-flash")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set. No results will be fabricated.",
              file=sys.stderr)
        return 1

    fixtures = json.loads(FIXTURES.read_text())
    prompts = fixtures["prompts"]
    assert len(prompts) >= 40, "AC-P1 requires >= 40 prompts"

    # sanity: proxy must be alive before we spend tokens
    try:
        health = httpx.get(f"{args.base_url}/health", timeout=5)
        assert health.status_code == 200
    except Exception as exc:
        print(f"ERROR: proxy not reachable at {args.base_url}: {exc}",
              file=sys.stderr)
        return 1

    client = httpx.Client()
    results = []
    print(f"Running {len(prompts)} prompts x 2 arms (baseline/treatment)...")
    for p in prompts:
        question = next((m["content"] for m in p["messages"]
                         if m["role"] == "user"), "")
        base = run_one(client, args.base_url, args.model, p, conciseness=False)
        treat = run_one(client, args.base_url, args.model, p, conciseness=True)
        entry = {
            "id": p["id"], "category": p["category"],
            "baseline_tokens": base.get("tokens", 0),
            "treatment_tokens": treat.get("tokens", 0),
            "baseline_ok": base["ok"], "treatment_ok": treat["ok"],
        }
        if base["ok"] and treat["ok"]:
            entry.update(rubric_score(base["text"], treat["text"], question))
            entry["baseline_text"] = base["text"][:800]
            entry["treatment_text"] = treat["text"][:800]
        else:
            entry["error"] = base.get("error") or treat.get("error")
        results.append(entry)
        pct = (100 * (base["tokens"] - treat["tokens"]) / base["tokens"]
               if base.get("tokens") and treat["ok"] else float("nan"))
        print(f"  {p['id']}: {base.get('tokens', 0)} -> {treat.get('tokens', 0)} "
              f"({pct:.1f}%) {'OK' if entry.get('ok', True) else 'ERR'}")

    valid = [r for r in results if r.get("baseline_ok") and r.get("treatment_ok")]
    judged = [r for r in valid if r.get("mode") == "model_judge"]
    regressions = [r for r in judged if r.get("score_b", 10) < r.get("score_a", 10) - 1]

    # Production estimator — SHARED with the calibration gate (estimator.py).
    # The gate and the re-run must measure the same math; a divergence here is
    # the defect class the AC-P1a-gate exists to catch.
    pairs = [(r["baseline_tokens"], r["treatment_tokens"]) for r in valid]
    est = estimate(pairs)
    mean_reduction = est["mean_reduction_pct"]
    ci95 = est["ci95"]

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fixture_checksum": fixture_checksum(),
        "n_fixture": len(prompts),
        "n_valid": len(valid),
        "model": args.model,
        "mean_output_reduction_pct": round(mean_reduction, 2),
        "ci95_halfwidth": round(ci95, 2),
        "ci95_interval": [round(mean_reduction - ci95, 2), round(mean_reduction + ci95, 2)],
        "meets_15pct": bool(mean_reduction >= 15 and mean_reduction - ci95 >= 15),
        "quality_parity": {
            "n_judged": len(judged),
            "n_regressions_over_1pt": len(regressions),
            "parity_holds": bool(judged) and len(regressions) == 0,
        },
        "ac_p1c_floor_note": ("If >= 15% at parity is unachievable, the honest "
                              "achieved number >= 10% ships instead (AC-P1c)."),
        "results": results,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"benchmark_{args.model.replace('/', '_')}_{stamp}.json"
    path.write_text(json.dumps(summary, indent=2))
    print(f"\nMean output reduction: {mean_reduction:.2f}% (95% CI ±{ci95:.2f})")
    print(f"Quality parity: {len(judged)} judged, {len(regressions)} >1pt regressions")
    print(f"AC-P1 target (>=15% mean, CI lower bound >=15): "
          f"{'MET' if summary['meets_15pct'] else 'NOT MET'}")
    print(f"Results written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
