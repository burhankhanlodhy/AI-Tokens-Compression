"""P1-1: output-conciseness benchmark runner (AC-P1/P1a/P1b/P1c).

Design (per product-spec-v2.md):
- Fixture set: benchmark/prompts.json (pinned corpus v2, 55 prompts,
  committed pre-run, mix >= 60% conversational/QA/RAG, <= 40% code) —
  AC-P1a anti-cherry-picking. The corpus checksum is PINNED below; the
  runner refuses to spend a single token against a corpus that does not
  match the pin (AC-P1b).
- Each ELIGIBLE prompt is sent 2 x HARNESS_K times through the live proxy
  route: HARNESS_K samples with OUTPUT_CONCISENESS_ENABLED=false (baseline)
  and HARNESS_K samples =true (treatment), aggregated into per-prompt token
  sums (the aggregation the shared estimator and the calibration gate both
  simulate). Ineligible prompts are NOT sampled at full k (spend ruling:
  byte-identical arms by gate design, 0pp expected by construction) — under
  the default --eligible-only mode they get ONE baseline pass so the
  blended corpus-wide figure has honest denominator weights; their
  treatment side is derived (:= baseline). --full-corpus restores the
  every-prompt full-k shape (owner override, ~3,300 completions).
- Output tokens counted from the provider's usage.completion_tokens
  (AC-P1a); the proxy-side counter is only a recorded fallback.
- Temperature is pinned (TEMPERATURE) on every request and recorded in
  the results JSON — an unpinned temperature is a confound the honesty
  gate would not see.
- Headline population (PM subset-headline ruling, 2026-09-16): the
  headline is measured over the ELIGIBLE subset — prompts whose last user
  message clears the production gate (should_inject_conciseness), i.e.
  the requests where the feature actually fires. The corpus-wide blended
  figure is published BESIDE it, explicitly labelled, never alone. Both
  name the population they cover.
- Quality parity: model-based side-by-side pairwise judge (AC-P1) with
  randomised A/B order per prompt (both-orders randomisation).
- Statistical claim: paired test at 95% CI (AC-P1a) via the shared
  corrected estimator (ratio-of-sums + bootstrap).

Usage:
  OPENROUTER_API_KEY=sk-... .venv/bin/python benchmark/run_benchmark.py \
      --base-url http://localhost:8000 --model z-ai/glm-5.3-flash

Requires a running proxy (docker compose up) and a real API key in the env
of the CLIENT calls (BYOK passthrough). No results are fabricated: if the
proxy is unreachable or the corpus checksum mismatches the pin, the script
exits non-zero with no results file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from estimator import estimate, HARNESS_K  # noqa: E402 — shared with the gate

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "prompts.json"

# AC-P1b pin: the immutable corpus v2 this harness is allowed to spend
# against (commit 7cae1b1). A mismatch aborts BEFORE any provider call.
EXPECTED_FIXTURE_SHA256 = "e3fcde4d6862b97ec828bfb1e977fe12ff321d76b1f19a0c3a608c3f8cd154cd"

# Pinned decoding temperature (SD-gate artifact was measured at temp=0.0).
TEMPERATURE = 0.0


def fixture_checksum() -> str:
    return hashlib.sha256(FIXTURES.read_bytes()).hexdigest()


def count_output_tokens(text: str, model: str) -> int:
    """Fallback counter (proxy's own) — only used if usage is missing."""
    sys.path.insert(0, str(ROOT.parent))
    from proxy.counting import count_text

    return count_text(text, model)


def extract_text(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def usage_completion_tokens(payload: dict) -> int | None:
    try:
        val = payload["usage"]["completion_tokens"]
        return int(val) if val is not None else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def is_eligible(prompt: dict) -> bool:
    """Production gate predicate on the prompt's LAST user message.

    The same code path the live proxy uses to decide injection — the
    headline population is "requests where the feature fires", measured
    with the exact predicate that makes that decision.
    """
    sys.path.insert(0, str(ROOT.parent))
    from proxy.counting import should_inject_conciseness

    return should_inject_conciseness(prompt["messages"])


def run_one(client: httpx.Client, base_url: str, model: str,
            prompt: dict, conciseness: bool) -> dict:
    body = {
        "model": model,
        "messages": prompt["messages"],
        "stream": False,
        "temperature": TEMPERATURE,
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
                payload = r.json()
                text = extract_text(payload)
                usage = usage_completion_tokens(payload)
                if usage is not None:
                    return {"ok": True, "text": text, "tokens": usage,
                            "tokens_source": "usage.completion_tokens",
                            "latency_ms": r.elapsed.total_seconds() * 1000}
                # AC-P1a fallback, recorded so the honesty gate sees it.
                return {"ok": True, "text": text,
                        "tokens": count_output_tokens(text, model),
                        "tokens_source": "count_text_fallback",
                        "latency_ms": r.elapsed.total_seconds() * 1000}
            last_err = f"status {r.status_code}: {r.text[:150]}"
        except httpx.HTTPError as exc:
            last_err = str(exc)
        time.sleep(2 ** attempt)
    return {"ok": False, "text": "", "tokens": 0, "error": last_err}


def run_arm(client: httpx.Client, base_url: str, model: str,
            prompt: dict, conciseness: bool, k: int = HARNESS_K) -> dict:
    """Take k samples of one arm; aggregate into the per-prompt token sum
    the estimator consumes. All k samples must succeed for the arm to
    count (a partial arm is a failed pair, never silently averaged).
    k=0 means the arm is NOT sampled (eligible-only spend ruling for the
    ineligible corpus: byte-identical arms buy noise, not signal)."""
    if k == 0:
        return {"ok": True, "n_ok": 0, "k": 0, "tokens_total": 0,
                "text": "", "tokens_source": None, "sampled": False,
                "error": None}
    samples = [run_one(client, base_url, model, prompt, conciseness)
               for _ in range(k)]
    n_ok = sum(1 for s in samples if s["ok"])
    first_ok = next((s for s in samples if s["ok"]), None)
    err = next((s.get("error") for s in samples if not s["ok"]), None)
    return {"ok": n_ok == k, "n_ok": n_ok, "k": k, "sampled": True,
            "tokens_total": sum(s.get("tokens", 0) for s in samples),
            "text": first_ok["text"] if first_ok else "",
            "tokens_source": (first_ok or {}).get("tokens_source"),
            "error": err}


def sampling_plan(eligible: bool, eligible_only: bool) -> dict:
    """Spend ruling (PM, 2026-09-16): the ineligible prompts send
    byte-identical arms by gate design with temperature pinned — their
    expected contribution is 0pp by construction, so full-k sampling of
    them buys noise, not real money. Shipping shape: full-k both arms on
    the eligible subset, single-pass baseline on the ineligible corpus
    (for honest derived blended weights), judge restricted to the
    headline population."""
    if eligible:
        return {"baseline_k": HARNESS_K, "treatment_k": HARNESS_K,
                "judge": True}
    if eligible_only:
        return {"baseline_k": 1, "treatment_k": 0, "judge": False}
    return {"baseline_k": HARNESS_K, "treatment_k": HARNESS_K, "judge": True}


def rubric_score(baseline: str, treatment: str, question: str,
                 rng: random.Random) -> dict:
    """Model-based pairwise judge (AC-P1) with randomised A/B order.

    Uses the same upstream model in judge mode via a direct (unproxied)
    call; falls back to a length-blind heuristic ONLY if no judge key is
    available — and records that fact so the honesty gate (AC-P1b) catches
    it. The winner is mapped back so `winner` is always reported relative
    to (baseline, treatment), whichever slot the model saw first.
    """
    judge_key = os.environ.get("OPENROUTER_API_KEY")
    judge_model = os.environ.get("BENCHMARK_JUDGE_MODEL", "openai/gpt-4o")
    if not judge_key:
        return {"mode": "no_judge_key", "parity": None, "winner": "unknown"}

    swap = rng.random() < 0.5
    answer_a, answer_b = ((treatment, baseline) if swap
                          else (baseline, treatment))
    prompt = (
        "You are a strict evaluator. Compare two AI answers to the same "
        "question. Score each 1-10 on: structural correctness and answer "
        "fidelity (does it answer what was asked, accurately, without "
        "hallucination). Brevity is NOT rewarded; only correctness and "
        "completeness of the actual answer.\n\n"
        f"QUESTION: {question}\n\n"
        f"ANSWER A:\n{answer_a[:4000]}\n\n"
        f"ANSWER B:\n{answer_b[:4000]}\n\n"
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
        score_a, score_b = int(data["score_a"]), int(data["score_b"])
        winner = data.get("winner", "tie")
        if swap:  # map back to baseline/treatment reference frame
            score_a, score_b = score_b, score_a
            winner = {"a": "b", "b": "a", "tie": "tie"}.get(winner, "tie")
        return {"mode": "model_judge", "judge_order_swapped": swap,
                "score_a": score_a, "score_b": score_b, "winner": winner}
    except Exception as exc:  # noqa: BLE001
        return {"mode": f"judge_error: {exc}", "parity": None,
                "winner": "unknown"}


def last_user_message(prompt: dict) -> str:
    user_msgs = [m for m in prompt["messages"] if m.get("role") == "user"]
    return user_msgs[-1].get("content", "") if user_msgs else ""


def entry_from_arms(p: dict, eligible: bool, base: dict, treat: dict) -> dict:
    """Build one results entry from its two run_arm() outputs.

    C-7b (PM ruling, measured): run_arm() returns RAW k-sample SUMS. In the
    default eligible-only mode the eligible arms accumulate k=30 calls while
    the ineligible corpus is a single call, so weighting the blended
    corpus-wide figure by raw sums enters the 40 zero-effect prompts at
    1/30th of their true traffic weight — a ~3.4x over-report of the only
    number claiming to describe a customer's whole bill (13.78pp vs the
    correct 4.09pp at a true 15%), and the two modes disagreed by
    construction (--full-corpus was right). Every arm is therefore
    normalized by its OWN n_ok to a per-call mean before it reaches
    summarize(); raw totals are kept beside for audit. The headline is
    unaffected (ratio-of-sums within one arm depth) and eligible-only
    blended == full-corpus blended on the same effect — the pinned
    invariant.
    """
    base_calls = base.get("n_ok") or 0
    treat_calls = treat.get("n_ok") or 0
    entry = {
        "id": p["id"], "category": p["category"], "eligible": eligible,
        "baseline_tokens": (base.get("tokens_total", 0) / base_calls
                            if base_calls else 0.0),
        "treatment_tokens": (treat.get("tokens_total", 0) / treat_calls
                             if treat_calls else 0.0),
        "treatment_sampled": treat["sampled"],
        "baseline_ok": base["ok"], "treatment_ok": treat["ok"],
        "baseline_n_ok": base["n_ok"], "treatment_n_ok": treat["n_ok"],
        "baseline_tokens_total": base.get("tokens_total", 0),
        "treatment_tokens_total": treat.get("tokens_total", 0),
        "tokens_source": base.get("tokens_source"),
    }
    question = last_user_message(p)
    if base["ok"] and treat["sampled"] and treat["ok"]:
        # deterministic per-prompt judge order (str seeds are stable
        # across processes, unlike hash())
        entry.update(rubric_score(base["text"], treat["text"],
                                  question,
                                  random.Random(f"judge-order:{p['id']}")))
        entry["baseline_text"] = base["text"][:800]
        entry["treatment_text"] = treat["text"][:800]
    elif not base["ok"] or not treat["ok"]:
        entry["error"] = base.get("error") or treat.get("error")
    return entry


# AC-P1c publication floor (C-10, ratified): the sabotage-sweep blind-spot
# width. Below this the instrument cannot resolve signal from noise, so a
# figure there is noise dressed as signal — never publishable.
PUBLICATION_FLOOR_PP = 2.0


def _publication(e: dict, n_valid: int) -> dict:
    """AC-P1g publication contract (C-10, ratified) — applies to EVERY
    published figure, headline AND blended alike (PM amendment: a suppressed
    headline sitting next to a bare blended percentage is the same
    noise-dressed-as-signal publication one field over).

    A figure carries a percentage ONLY when it is a measured effect:
      - >= 2 valid pairs with a non-degenerate (hi > lo) 95% interval,
      - the interval excludes 0 on the reduction side (the AC-P1a-gate
        null-FP AND contract — a CI including 0 is no measured effect
        regardless of the point estimate),
      - the point estimate clears the 2pp publication floor.
    Otherwise publication_status = "no_measurable_effect" (the literal is
    pinned by the committed CI-blocking test, so it is contract, not style)
    and reported_reduction_pct is null.

    Branch order encodes the QA-pinned precedence: a sub-floor estimate
    reports the 2pp blind-spot reason even when the interval is ALSO
    degenerate (the committed test feeds a single pair at 1.5pp and asserts
    "2pp" in the note); a healthy estimate with a degenerate interval still
    ships no percentage — that is the n=1-at-3.1pp zero-width-CI case.
    """
    est = e["mean_reduction_pct"]
    lo, hi = e["ci95_interval"]
    degenerate = n_valid < 2 or not hi > lo
    if est < PUBLICATION_FLOOR_PP:
        note = (f"estimated {round(est, 2)}pp is below the 2pp publication "
                "floor — inside the measured blind-spot width where signal "
                "cannot be told from noise")
        if degenerate:
            note += (" (interval is also degenerate: fewer than 2 valid "
                     "pairs / zero-width CI)")
        return {"publication_status": "no_measurable_effect",
                "reported_reduction_pct": None,
                "publication_note": note}
    if degenerate:
        return {"publication_status": "no_measurable_effect",
                "reported_reduction_pct": None,
                "publication_note": ("degenerate inference: fewer than 2 "
                                     "valid pairs or a zero-width 95% CI — "
                                     "no variance information, no "
                                     "percentage shipped")}
    if lo > 0.0:
        return {"publication_status": "measurable_reduction",
                "reported_reduction_pct": round(est, 2),
                "publication_note": (f"measured {round(est, 2)}pp reduction: "
                                     "95% CI excludes 0 and the estimate "
                                     "clears the 2pp publication floor")}
    if hi < 0.0:
        return {"publication_status": "no_measurable_effect",
                "reported_reduction_pct": None,
                "publication_note": ("95% CI excludes 0 on the INCREASE "
                                     "side — a measured regression, not a "
                                     "reduction; no reduction percentage "
                                     "is publishable")}
    return {"publication_status": "no_measurable_effect",
            "reported_reduction_pct": None,
            "publication_note": ("95% CI includes 0 — no measured effect "
                                 "regardless of the point estimate "
                                 "(2pp publication floor applies)")}


def summarize(valid_entries: list[dict]) -> dict:
    """Headline (eligible subset) + labelled blended (corpus-wide) stats."""
    eligible = [r for r in valid_entries if r.get("eligible")]
    blended_pairs = [(r["baseline_tokens"],
                      r["treatment_tokens"] if r.get("treatment_sampled", True)
                      else r["baseline_tokens"])
                     for r in valid_entries]
    eligible_pairs = [(r["baseline_tokens"], r["treatment_tokens"])
                      for r in eligible]
    headline = estimate(eligible_pairs)
    blended = estimate(blended_pairs)
    judged = [r for r in valid_entries if r.get("mode") == "model_judge"]
    regressions = [r for r in judged
                   if r.get("score_b", 10) < r.get("score_a", 10) - 1]
    # AC-P1a "valid pair" = baseline > 0 (the estimator's own filter): the
    # n >= 2 arm of the publication guard counts the SAME rows the estimate
    # was computed from, not raw entries.
    n_valid_headline = sum(1 for b, _ in eligible_pairs if b > 0)
    n_valid_blended = sum(1 for b, _ in blended_pairs if b > 0)
    return {
        "headline_population": "eligible_subset",
        "n_eligible": len(eligible),
        "headline": {
            "mean_output_reduction_pct": round(headline["mean_reduction_pct"], 2),
            "ci95_halfwidth": round(headline["ci95"], 2),
            "ci95_interval": [round(v, 2) for v in headline["ci95_interval"]],
            "meets_15pct": bool(headline["mean_reduction_pct"] >= 15
                                and headline["mean_reduction_pct"] - headline["ci95"] >= 15),
            **_publication(headline, n_valid_headline),
        },
        "blended_corpus_wide": {
            "label": ("corpus-wide blended over ALL valid prompts — NOT the "
                      "headline; dilutes the eligible-subset effect with "
                      "byte-identical arms. DERIVED when ineligible arms "
                      "were not sampled: their arms are byte-identical by "
                      "gate design (treatment := baseline, 0pp contribution) "
                      "and only single-pass baseline weights were measured."),
            "derived": any(not r.get("treatment_sampled", True)
                           for r in valid_entries),
            "n": len(blended_pairs),
            "mean_output_reduction_pct": round(blended["mean_reduction_pct"], 2),
            "ci95_halfwidth": round(blended["ci95"], 2),
            "ci95_interval": [round(v, 2) for v in blended["ci95_interval"]],
            **_publication(blended, n_valid_blended),
        },
        "quality_parity": {
            "n_judged": len(judged),
            "n_regressions_over_1pt": len(regressions),
            "parity_holds": bool(judged) and len(regressions) == 0,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--model", default="z-ai/glm-5.3-flash")
    ap.add_argument("--out", default=str(ROOT / "results"))
    # C-9 spend ruling: eligible-only is the DEFAULT shipping shape
    # (~970 calls). The full-55 measured shape (~3,300 + judges) requires
    # an explicit --full-corpus owner override — it can never happen
    # silently.
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--eligible-only", dest="mode", default="eligible_only",
                      action="store_const", const="eligible_only",
                      help="(default) full-k both arms on the eligible "
                           "subset; single-pass baseline only on the "
                           "ineligible corpus; judge restricted to the "
                           "headline population")
    mode.add_argument("--full-corpus", dest="mode",
                      action="store_const", const="full_corpus",
                      help="OWNER OVERRIDE: sample every prompt at full k "
                           "both arms (~3,300 completions + judges)")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    eligible_only = args.mode == "eligible_only"

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set. No results will be fabricated.",
              file=sys.stderr)
        return 1

    # AC-P1b checksum pin: verify BEFORE anything else, before even the
    # health check — a wrong corpus never reaches the spend path.
    checksum = fixture_checksum()
    if checksum != EXPECTED_FIXTURE_SHA256:
        print(f"ERROR: pinned corpus checksum mismatch.\n"
              f"  expected {EXPECTED_FIXTURE_SHA256}\n"
              f"  found    {checksum}\n"
              f"The benchmark refuses to run against an unpinned corpus.",
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
    plans = [(p, is_eligible(p)) for p in prompts]
    n_eligible = sum(1 for _, e in plans if e)
    # spend estimate BEFORE the first provider call — never a silent budget
    est_calls = sum(
        sampling_plan(e, eligible_only)["baseline_k"]
        + sampling_plan(e, eligible_only)["treatment_k"]
        for _, e in plans)
    est_judges = sum(1 for _, e in plans
                     if e and sampling_plan(e, eligible_only)["judge"])
    mode_label = ("eligible-only (ruled shipping shape)" if eligible_only
                  else "FULL CORPUS (owner override)")
    print(f"Mode: {mode_label} | HARNESS_K={HARNESS_K} | "
          f"eligible subset: {n_eligible}/{len(prompts)}")
    print(f"Spend estimate: ~{est_calls} completions + ~{est_judges} judge "
          f"calls. Ctl-C now if this is not the authorized budget.")
    for p, eligible in plans:
        plan = sampling_plan(eligible, eligible_only)
        base = run_arm(client, args.base_url, args.model, p,
                       conciseness=False, k=plan["baseline_k"])
        treat = run_arm(client, args.base_url, args.model, p,
                        conciseness=True, k=plan["treatment_k"])
        entry = entry_from_arms(p, eligible, base, treat)
        results.append(entry)
        pct = (100 * (entry["baseline_tokens"] - entry["treatment_tokens"])
               / entry["baseline_tokens"]
               if entry["baseline_tokens"] and treat["sampled"] and treat["ok"]
               else float("nan"))
        tag = "*" if eligible else ("b" if eligible_only else "*")
        print(f"  {p['id']}{tag}: "
              f"{entry['baseline_tokens']} -> "
              f"{entry['treatment_tokens'] if treat['sampled'] else '(n/a)'} "
              f"({pct:.1f}%) "
              f"{'OK' if base['ok'] and treat['ok'] else 'ERR'}")

    valid = [r for r in results if r.get("baseline_ok") and r.get("treatment_ok")]

    # Production estimator — SHARED with the calibration gate (estimator.py).
    # The gate and the re-run must measure the same math; a divergence here is
    # the defect class the AC-P1a-gate exists to catch.
    stats = summarize(valid)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fixture_checksum": checksum,
        "fixture_checksum_pinned": EXPECTED_FIXTURE_SHA256,
        "temperature": TEMPERATURE,
        "harness_k": HARNESS_K,
        "sampling_mode": args.mode,
        "n_fixture": len(prompts),
        "n_valid": len(valid),
        **stats,
        "ac_p1c_floor_note": ("If >= 15% at parity is unachievable, the honest "
                              "achieved number >= 10% ships instead (AC-P1c)."),
        "results": results,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"benchmark_{args.model.replace('/', '_')}_{stamp}.json"
    path.write_text(json.dumps(summary, indent=2))
    hl = stats["headline"]
    bl = stats["blended_corpus_wide"]
    print(f"\nHEADLINE (eligible subset, n={stats['n_eligible']}): "
          f"{hl['mean_output_reduction_pct']:.2f}% "
          f"(95% CI ±{hl['ci95_halfwidth']:.2f})")
    print(f"Blended (corpus-wide, n={bl['n']}, labelled, not the headline): "
          f"{bl['mean_output_reduction_pct']:.2f}% "
          f"(95% CI ±{bl['ci95_halfwidth']:.2f})")
    print(f"Quality parity: {stats['quality_parity']['n_judged']} judged, "
          f"{stats['quality_parity']['n_regressions_over_1pt']} >1pt regressions")
    print(f"AC-P1 target on HEADLINE (>=15% mean, CI lower bound >=15): "
          f"{'MET' if hl['meets_15pct'] else 'NOT MET'}")
    print(f"Results written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
