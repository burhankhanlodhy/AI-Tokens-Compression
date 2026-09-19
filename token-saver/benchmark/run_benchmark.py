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

Usage (P1-1 paid run — the exact ratified invocation, mirrored in
  product-spec-v2.md; the model default IS the K-3-pinned instrument):
  OPENROUTER_API_KEY=sk-... .venv/bin/python benchmark/run_benchmark.py \
      --base-url http://localhost:8000
  # -> default mode is --eligible-only (C-9), default model is
  #    google/gemini-3.5-flash-lite (B4 instrument swap). The GLM slug is
  #    WITHDRAWN for the paid run (78.1% reasoning share, null by
  #    construction) and is NOT a valid --model here.

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


def grounded_risk_of(prompt: dict) -> str:
    """P6-1 production grounded-answer risk on the full message list.

    C-4a rule (P6-1): the harness imports the production symbol — never
    re-implements it — so a patched detector flips the recorded verdicts
    on the next run. Since P6-4 (05be0f3) ``grounded_answer_risk`` scans
    system + EVERY user message (OpenAI ``role: system`` and Anthropic
    typed content blocks alike); a source block anywhere grounds the
    request at fidelity_critical. Recorded per entry as
    ``grounded_risk`` metadata; pre-AC-P6c this does NOT touch any
    published figure.
    """
    sys.path.insert(0, str(ROOT.parent))
    from proxy.grounded import grounded_answer_risk

    return grounded_answer_risk(prompt["messages"])["risk"]


def _reasoning_control(model: str) -> dict:
    """The reasoning control sent EXPLICITLY on both benchmark arms (PM v4
    ruling, consequence 2): the run must pin the same control the proxy
    would inject, so arm behavior never depends on proxy config and the
    headline carries the same audit evidence as the SD gate. One source of
    truth: proxy.config.reasoning_control_for."""
    sys.path.insert(0, str(ROOT.parent))
    from proxy.config import reasoning_control_for
    return reasoning_control_for(model)


def _reasoning_evidence(resp, payload: dict, model: str) -> dict:
    """Per-sample audit evidence for the headline (PM v4 ruling): raw
    reasoning count, whether the provider REPORTS the field (absence is a
    distinct fact from zero), the proxy's evidence header, and the control
    actually sent. Defensive about test-double responses (no headers attr)."""
    u = payload.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    headers = getattr(resp, "headers", None)
    header = headers.get("x-token-saver-reasoning") if headers else None
    return {
        "reasoning_control": _reasoning_control(model),
        "reasoning_tokens": int(details.get("reasoning_tokens", 0) or 0),
        "reasoning_field_present": "reasoning_tokens" in details,
        "evidence_header": header,
    }


def run_one(client: httpx.Client, base_url: str, model: str,
            prompt: dict, conciseness: bool,
            dose_pin: str | None = None) -> dict:
    body = {
        "model": model,
        "messages": prompt["messages"],
        "stream": False,
        "temperature": TEMPERATURE,
        # Same MINIMAL control on BOTH arms (PM v4): pin it client-side so
        # the run never depends on proxy config for its control.
        **_reasoning_control(model),
    }
    headers = {
        "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
        "X-Token-Saver-Conciseness": "1" if conciseness else "0",
    }
    if dose_pin is not None:
        # P6-3 tier pin (AC-P6c(2), PM ratification): the calibration
        # instrument forces the proxy's dose tier so `bounded` can be
        # measured on grounded fixtures the pre-calibration runtime cap
        # forbids. Consumed ONLY when the deployment sets allow_dose_pin
        # (production default False); the header is a PROXY_CONTROL_HEADER
        # and never forwards upstream. Sent on BOTH arms for symmetric
        # evidence — the baseline arm (conciseness 0) ignores it by design.
        headers["X-Token-Saver-Dose-Pin"] = dose_pin
    last_err = None
    for attempt in range(3):
        try:
            r = client.post(f"{base_url}/v1/chat/completions",
                            json=body, headers=headers, timeout=120)
            if r.status_code == 200:
                payload = r.json()
                text = extract_text(payload)
                usage = usage_completion_tokens(payload)
                evidence = _reasoning_evidence(r, payload, model)
                if usage is not None:
                    return {"ok": True, "text": text, "tokens": usage,
                            "tokens_source": "usage.completion_tokens",
                            "reasoning_evidence": evidence,
                            "latency_ms": r.elapsed.total_seconds() * 1000}
                # AC-P1a fallback, recorded so the honesty gate sees it.
                return {"ok": True, "text": text,
                        "tokens": count_output_tokens(text, model),
                        "tokens_source": "count_text_fallback",
                        "reasoning_evidence": evidence,
                        "latency_ms": r.elapsed.total_seconds() * 1000}
            last_err = f"status {r.status_code}: {r.text[:150]}"
        except httpx.HTTPError as exc:
            last_err = str(exc)
        time.sleep(2 ** attempt)
    return {"ok": False, "text": "", "tokens": 0, "error": last_err}


def run_arm(client: httpx.Client, base_url: str, model: str,
            prompt: dict, conciseness: bool, k: int = HARNESS_K,
            dose_pin: str | None = None) -> dict:
    """Take k samples of one arm; aggregate into the per-prompt token sum
    the estimator consumes. All k samples must succeed for the arm to
    count (a partial arm is a failed pair, never silently averaged).
    k=0 means the arm is NOT sampled (eligible-only spend ruling for the
    ineligible corpus: byte-identical arms buy noise, not signal)."""
    if k == 0:
        return {"ok": True, "n_ok": 0, "k": 0, "tokens_total": 0,
                "text": "", "tokens_source": None, "sampled": False,
                "error": None}
    samples = [run_one(client, base_url, model, prompt, conciseness,
                       dose_pin=dose_pin)
               for _ in range(k)]
    n_ok = sum(1 for s in samples if s["ok"])
    first_ok = next((s for s in samples if s["ok"]), None)
    err = next((s.get("error") for s in samples if not s["ok"]), None)
    # PM v4 (consequence 2): per-sample reasoning evidence travels with the
    # arm into the results artifact — the headline's composition must be
    # verifiable, not just its total.
    return {"ok": n_ok == k, "n_ok": n_ok, "k": k, "sampled": True,
            "tokens_total": sum(s.get("tokens", 0) for s in samples),
            "text": first_ok["text"] if first_ok else "",
            "tokens_source": (first_ok or {}).get("tokens_source"),
            "samples_evidence": [s["reasoning_evidence"]
                                 for s in samples if s["ok"]],
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


def _judge_once(answer_a: str, answer_b: str, question: str) -> dict:
    """One judge call in the raw A/B frame (no mapping back).

    Returns the parsed scores/winner exactly as the model saw them, or a
    dict with truthy `error` on any failure. Kept separate from
    rubric_score so the both-orders wrapper (AC-P1b) can run the mirror
    order through the identical call path.
    """
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
    judge_key = os.environ.get("OPENROUTER_API_KEY")
    judge_model = os.environ.get("BENCHMARK_JUDGE_MODEL", "openai/gpt-4o")
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {judge_key}"},
            json={"model": judge_model,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.status_code != 200:
            return {"error": f"judge_http_{r.status_code}"}
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content[content.index("{"):content.rindex("}") + 1])
        return {"score_a": int(data["score_a"]),
                "score_b": int(data["score_b"]),
                "winner": data.get("winner", "tie")}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"judge_error: {exc}"}


def rubric_score(baseline: str, treatment: str, question: str,
                 rng: random.Random | None = None) -> dict:
    """Model-based pairwise judge (AC-P1) — BOTH A/B orders, averaged.

    Position bias is decided by averaging the mirror orders, not by hoping
    one randomised order cancels it: every item is judged once with the
    baseline as ANSWER A and once with the treatment as ANSWER A, each
    order's raw scores are mapped back to the (baseline, treatment)
    reference frame, and `score_a`/`score_b` carry the mean across both
    orders (raw per-order scores are kept in `judge_orders` for audit).
    `parity_holds` therefore consumes position-debiased evidence. The
    `rng` parameter is vestigial from the single-order design and is
    accepted for call-site compatibility only — the order pair is fixed,
    so results are deterministic by construction.

    Falls back to a length-blind heuristic ONLY if no judge key is
    available — and records that fact so the honesty gate (AC-P1b) catches
    it. If EITHER order's judge call fails, the item is excluded from the
    parity population entirely (mode carries the failure): a one-order
    score would silently re-import the position bias this fix removes.
    """
    judge_key = os.environ.get("OPENROUTER_API_KEY")
    judge_model = os.environ.get("BENCHMARK_JUDGE_MODEL", "openai/gpt-4o")
    if not judge_key:
        return {"mode": "no_judge_key", "parity": None, "winner": "unknown"}

    orders = [("baseline_first", baseline, treatment),
              ("treatment_first", treatment, baseline)]
    mapped: list[dict] = []
    for name, answer_a, answer_b in orders:
        raw = _judge_once(answer_a, answer_b, question)
        if "error" in raw:
            return {"mode": raw["error"], "parity": None,
                    "winner": "unknown"}
        score_a, score_b = raw["score_a"], raw["score_b"]
        winner = raw["winner"]
        if name == "treatment_first":  # map back to the reference frame
            score_a, score_b = score_b, score_a
            winner = {"a": "b", "b": "a", "tie": "tie"}.get(winner, "tie")
        mapped.append({"order": name, "score_a": score_a,
                       "score_b": score_b, "winner": winner})
    mean_a = sum(o["score_a"] for o in mapped) / len(mapped)
    mean_b = sum(o["score_b"] for o in mapped) / len(mapped)
    if mean_a > mean_b:
        winner = "a"
    elif mean_b > mean_a:
        winner = "b"
    else:
        winner = "tie"
    return {"mode": "model_judge", "judge_orders": mapped,
            "score_a": mean_a, "score_b": mean_b, "winner": winner}


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
        "grounded_risk": grounded_risk_of(p),
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


# AC-P1c publication floor (C-10 ratified; amended PM B4 2026-09-17): the
# MEASURED sabotage-sweep blind-spot width, not an assumption. Graded
# sabotage: injected savings of 20/10/4.5pp were caught; 3.0pp and below
# passed through undetected, so estimates at or below 3.0pp cannot be told
# from noise — never publishable.
PUBLICATION_FLOOR_PP = 3.0


def _publication(e: dict, n_valid: int,
                 population_incomplete: bool = False,
                 parity_holds: bool | None = None) -> dict:
    """AC-P1g publication contract (C-10, ratified; floor amended B4
    2026-09-17 to the MEASURED blind-spot width) — applies to EVERY
    published figure, headline AND blended alike (PM amendment: a suppressed
    headline sitting next to a bare blended percentage is the same
    noise-dressed-as-signal publication one field over).

    A figure carries a percentage ONLY when it is a measured effect:
      - the parity population the run planned is COMPLETE (AC-P1g clause,
        PM 1b: an 80%-lost run publishes no percentage — the artifact is
        what gets quoted, not the boolean beside it),
      - >= 2 valid pairs with a non-degenerate (hi > lo) 95% interval,
      - the interval excludes 0 on the reduction side (the AC-P1a-gate
        null-FP AND contract — a CI including 0 is no measured effect
        regardless of the point estimate),
      - the point estimate clears the 3pp publication floor (at or below
        the measured blind spot is suppressed).
    Otherwise publication_status = "no_measurable_effect" (the literal is
    pinned by the committed CI-blocking test, so it is contract, not style)
    and reported_reduction_pct is null.

    P2 (P3 ratification, option (a), 2026-09-18): `parity_holds` is the
    subset's OWN parity verdict — None (no parity evidence / vacuous)
    changes nothing, but an explicit False suppresses the figure even when
    the interval is healthy: a sellable percentage beside a red parity
    gate is the exact noise-dressed-as-signal publication AC-P1g exists to
    forbid, one field over. Precedence: population incompleteness (0) >
    parity failure (1) > 3pp floor (2) — a population/parity failure
    invalidates the MEASUREMENT, the floor only its publishability.

    Branch order encodes the QA-pinned precedence: population
    incompleteness is precedence-0 — above the 3pp check — because a
    collapsed population invalidates the measurement itself, not just its
    floor clearance; a sub-floor estimate still reports the 3pp
    blind-spot reason even when the interval is ALSO degenerate (the
    committed test feeds a single pair at 1.5pp and asserts "3pp" in the
    note); a healthy estimate with a degenerate interval still ships no
    percentage — that is the n=1-at-3.1pp zero-width-CI case.
    """
    est = e["mean_reduction_pct"]
    lo, hi = e["ci95_interval"]
    degenerate = n_valid < 2 or not hi > lo
    if population_incomplete:
        return {"publication_status": "no_measurable_effect",
                "reported_reduction_pct": None,
                "publication_note": ("parity population incomplete: the run "
                                     "planned more judged items than "
                                     "completed both orders (see "
                                     "quality_parity.upstream_lost_ids / "
                                     "excluded_judge_ids) — a percentage "
                                     "off a partial population is never "
                                     "publishable, regardless of the "
                                     "estimate")}
    if parity_holds is False:
        return {"publication_status": "no_measurable_effect",
                "reported_reduction_pct": None,
                "publication_note": ("quality parity gate FAILED on this "
                                     "population (mean regression above "
                                     "the 1pt spec gate — see "
                                     "quality_parity beside this figure): "
                                     "the effect is measured but "
                                     "suppressed-pending-fix, never "
                                     "folded into a sellable claim")}
    if est <= PUBLICATION_FLOOR_PP:
        note = (f"estimated {round(est, 2)}pp is at or below the 3pp "
                "publication floor — inside the measured blind-spot width "
                "(graded sabotage: up to 3.0pp passed through undetected) "
                "where signal cannot be told from noise")
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
                                     "clears the 3pp publication floor")}
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
                                 "(3pp publication floor applies)")}


def _published_figure(e: dict) -> str:
    """AC-P1g CLI rendering of a published figure (C-7c): the percentage
    comes from reported_reduction_pct VERBATIM — never from the raw mean —
    so a result the publication contract suppresses cannot reach stdout
    dressed as a number."""
    if e["reported_reduction_pct"] is not None:
        return (f"{e['reported_reduction_pct']:.2f}% "
                f"(95% CI ±{e['ci95_halfwidth']:.2f}) "
                f"[{e['publication_status']}]")
    return (f"no measurable effect "
            f"({e['publication_status']}: {e['publication_note']})")


# ---------------------------------------------------------------------------
# P2: per-category + ratified headline-carve emission (option (a), user
# 2026-09-17, @product-manager in-file). Before this block the published
# 57.71pp carve was a hand computation that existed nowhere in
# machine-emitted output — the artifact carried only headline / blended /
# quality_parity. These blocks close that audit hole: every published
# subset figure is emitted by the same summarize() that produces the
# headline, under the same AC-P1g contract.
# ---------------------------------------------------------------------------

# Option (a) carve: the published headline claim is the eligible
# NON-CODE NON-RAG subset. RAG is published beside it, never hidden —
# its by_category block carries the measured figure with its own
# AC-P1g triplet (suppressed-pending-fix when its parity gate fails).
CARVE_EXCLUDED_CATEGORIES = ("code", "rag")

# Calibration caveat that MUST ship with the carve (ratified 09fc87c):
# the n=10 clearance rests on the measured gemini CV, and the committed
# empty-box artifact at N_PROMPTS=10 is the evidence (P4).
CARVE_CALIBRATION_CAVEAT = (
    "n=10 gate clearance rests on the MEASURED instrument CV=0.137 "
    "(sd_gate_google__gemini-3.5-flash-lite.json): at n=10/k=30, null FP "
    "0.00%, control 99.50%. The archived CV=0.2415 FAILS at n=10/k=30 "
    "(control 89.0% < 90% contract); a noisier instrument requires "
    "k>=40. Evidence: the committed empty-box artifact run at "
    "N_PROMPTS=10 with this CV pinned (P4).")


def _subset_parity(sub_entries: list[dict],
                   planned_ids_for_sub: list[str] | None) -> dict:
    """Parity/population accounting for ONE publication subset.

    Mirrors the top-level quality_parity rules exactly (planned-population
    anchor when the subset's planned ids are supplied, attempted-only
    otherwise, fails closed), but scoped to the subset: a category whose
    items were lost upstream must not inherit a green from the run-level
    population. `planned_ids_for_sub` is the subset's PLANNED judged
    population, derived by the caller from planned_judge_ids + the
    planned items' categories — NEVER by intersecting the planned list
    with the surviving rows (a lost item never reaches `valid`, so an
    intersection is the survivor anchor again).

    Sign conventions: `mean_regression_pt` is the gate convention
    (score_a - score_b, POSITIVE = treatment scored lower); the ratio
    `signed_mean_regression_pt` (score_b - score_a, NEGATIVE = treatment
    scored lower) matches the ratified carve arithmetic as quoted by the
    PM (qa-049 = -3.0) — both ship so the artifact can never be misread.

    parity_holds is None when the subset judged nothing (vacuous — a
    zero-eligible category carries no parity evidence and must not be
    suppressed by a gate that never ran), True/False otherwise.
    """
    attempted = [r for r in sub_entries if "mode" in r]
    judged = [r for r in attempted if r.get("mode") == "model_judge"]
    excluded_judge_ids = [r.get("id") for r in attempted
                          if r.get("mode") != "model_judge"]
    judged_ids = {r.get("id") for r in judged}
    if planned_ids_for_sub is not None:
        upstream_lost_ids = [pid for pid in planned_ids_for_sub
                             if pid not in judged_ids
                             and pid not in set(excluded_judge_ids)]
        population_complete = (len(judged) == len(planned_ids_for_sub)
                               and not excluded_judge_ids
                               and not upstream_lost_ids
                               and len(judged_ids) == len(planned_ids_for_sub))
        population_incomplete = not population_complete
    else:
        upstream_lost_ids = []
        population_complete = (bool(attempted)
                               and len(judged) == len(attempted))
        # No planned denominator: keep the pre-1b stats-helper semantics —
        # incompleteness suppression only activates on the run path.
        population_incomplete = False
    gate_regression = (round(sum(r.get("score_a", 10) - r.get("score_b", 10)
                                 for r in judged) / len(judged), 2)
                       if judged else None)
    signed_regression = (round(sum(r.get("score_b", 10) - r.get("score_a", 10)
                                   for r in judged) / len(judged), 2)
                         if judged else None)
    n_over_1pt = sum(1 for r in judged
                     if r.get("score_b", 10) < r.get("score_a", 10) - 1)
    parity_holds = None
    if judged:
        parity_holds = (population_complete
                        and gate_regression is not None
                        and gate_regression <= 1.0)
    return {
        "n_judged": len(judged),
        "n_attempted": len(attempted),
        "n_judged_planned": (len(planned_ids_for_sub)
                             if planned_ids_for_sub is not None else None),
        "population_complete": population_complete,
        "excluded_judge_ids": excluded_judge_ids,
        "upstream_lost_ids": upstream_lost_ids,
        "n_regressions_over_1pt": n_over_1pt,
        "mean_regression_pt": gate_regression,
        "signed_mean_regression_pt": signed_regression,
        "signed_regression_sign_convention": (
            "score_b - score_a: NEGATIVE = treatment scored lower "
            "(regression) — matches the ratified carve arithmetic"),
        "parity_rule": "mean_regression_le_1pt_full_population",
        "parity_holds": parity_holds,
        "_population_incomplete": population_incomplete,
    }


def _subset_block(label: str, population: str, sub_entries: list[dict],
                  planned_ids_for_sub: list[str] | None = None,
                  calibration_caveat: str | None = None) -> dict:
    """One publication-shaped subset figure (P2): estimate + CI + the full
    AC-P1g triplet + the subset's own quality_parity, all computed by the
    shared estimator and the same _publication() contract as the headline.

    Zero-eligible subsets (code: all 10 fixtures are gate-negative, K-6)
    emit the triplet with a construction note rather than a floor note —
    there is nothing measured, and "estimated 0.0pp" would misstate why.
    """
    pairs = [(r["baseline_tokens"], r["treatment_tokens"])
             for r in sub_entries]
    n_valid = sum(1 for b, _ in pairs if b > 0)
    e = estimate(pairs)
    parity = _subset_parity(sub_entries, planned_ids_for_sub)
    if not sub_entries:
        publication = {
            "publication_status": "no_measurable_effect",
            "reported_reduction_pct": None,
            "publication_note": ("no eligible prompts in this population — "
                                 "the production gate does not fire on any "
                                 "of its fixtures, so there is nothing to "
                                 "measure (0pp by construction, not by "
                                 "estimate)"),
        }
    else:
        publication = _publication(
            e, n_valid,
            population_incomplete=parity.pop("_population_incomplete"),
            parity_holds=parity["parity_holds"])
    parity.pop("_population_incomplete", None)
    block = {
        "label": label,
        "population": population,
        "n_eligible": len(sub_entries),
        "n_valid": n_valid,
        "mean_output_reduction_pct": round(e["mean_reduction_pct"], 2),
        "ci95_halfwidth": round(e["ci95"], 2),
        "ci95_interval": [round(v, 2) for v in e["ci95_interval"]],
        "meets_15pct": bool(e["mean_reduction_pct"] >= 15
                            and e["mean_reduction_pct"] - e["ci95"] >= 15),
        **publication,
        "quality_parity": parity,
    }
    if calibration_caveat is not None:
        block["calibration_caveat"] = calibration_caveat
    return block


def summarize(valid_entries: list[dict],
              n_eligible_planned: int | None = None,
              planned_judge_ids: list[str] | None = None,
              planned_categories: dict[str, str] | None = None) -> dict:
    """Headline (eligible subset) + labelled blended (corpus-wide) stats.

    AC-P1b population anchor (PM ratification, option (a)): the parity
    denominator is the PLANNED judged population from sampling_plan()
    (n=15 in the ruled eligible-only shape; n=55 under the --full-corpus
    owner override), NOT the survivors. main() passes it as
    n_eligible_planned (with planned_judge_ids for the audit trail); a
    call without the denominator keeps the older attempted-only anchor so
    the stats helpers stay callable, but the paid run path always anchors
    on the planned population.
    """
    eligible = [r for r in valid_entries if r.get("eligible")]
    blended_pairs = [(r["baseline_tokens"],
                      r["treatment_tokens"] if r.get("treatment_sampled", True)
                      else r["baseline_tokens"])
                     for r in valid_entries]
    eligible_pairs = [(r["baseline_tokens"], r["treatment_tokens"])
                      for r in eligible]
    headline = estimate(eligible_pairs)
    blended = estimate(blended_pairs)
    # Parity population = every entry that ATTEMPTED judging (both arms ok
    # and treatment sampled; entry_from_arms sets "mode" exactly then).
    # A failed judge call keeps "mode" (judge_http_429 / judge_error /
    # no_judge_key) but is not "model_judge", so without a population floor
    # a flaky judge API could shrink the parity population to a single
    # surviving prompt and still emit a green gate (PM synthetic: 14/15
    # excluded -> n_judged 1, parity_holds true). AC-P1b gate therefore
    # FAILS CLOSED: every attempted item must complete both orders.
    attempted = [r for r in valid_entries if "mode" in r]
    judged = [r for r in attempted if r.get("mode") == "model_judge"]
    excluded_judge_ids = [r.get("id") for r in attempted
                          if r.get("mode") != "model_judge"]
    # AC-P1b planned-eligible anchor: items whose ARMS failed never reach
    # judging, so they carry no "mode", are filtered out of `valid` before
    # this function sees them, and are invisible to the attempted-only
    # anchor — a run losing 12 of 15 to upstream 429s could publish a
    # green gate and a headline off the 3 survivors. When main() supplies
    # the planned population, the gate requires EVERY planned judged item
    # to have completed both orders, and upstream-lost IDs are recorded
    # beside (not inside) excluded_judge_ids: judge flakiness and
    # upstream flakiness are distinct failure classes.
    judged_ids = {r.get("id") for r in judged}
    excluded_set = set(excluded_judge_ids)
    if planned_judge_ids is not None and n_eligible_planned is None:
        n_eligible_planned = len(planned_judge_ids)
    upstream_lost_ids = ([pid for pid in planned_judge_ids
                          if pid not in judged_ids
                          and pid not in excluded_set]
                         if planned_judge_ids is not None else [])
    # AC-P1b parity gate is the spec's "≤1pt mean regression", NOT "zero
    # items >1pt": the zero-item reading is stricter than the spec and
    # won't survive judge noise (PM recompute 2026-09-18: the shipped
    # zero-item rule failed P1-1 at mean regression exactly 1.00pt — the
    # threshold itself). n_regressions_over_1pt is kept as a diagnostic.
    regressions = [r for r in judged
                   if r.get("score_b", 10) < r.get("score_a", 10) - 1]
    # The gate compares the PUBLISHED (2dp-rounded) mean, not the raw one —
    # otherwise a 1.004pt mean publishes as "1.0" next to parity_holds:
    # false and the artifact reads as a self-contradiction (PM audit nit).
    # The 0.005pt tolerance this admits is far below judge resolution.
    mean_regression_pt = (round(sum(r.get("score_a", 10) - r.get("score_b", 10)
                                    for r in judged) / len(judged), 2)
                          if judged else None)
    if n_eligible_planned is not None:
        # Planned-population gate: every planned judged item must complete
        # both orders AND nothing unplanned may sneak into the judged set
        # (n_judged == planned with a swapped-in item would still be a
        # population hole).
        population_complete = (len(judged) == n_eligible_planned
                               and not excluded_judge_ids
                               and not upstream_lost_ids
                               and len(judged_ids) == n_eligible_planned)
    else:
        population_complete = bool(attempted) and len(judged) == len(attempted)
    # AC-P1a "valid pair" = baseline > 0 (the estimator's own filter): the
    # n >= 2 arm of the publication guard counts the SAME rows the estimate
    # was computed from, not raw entries.
    n_valid_headline = sum(1 for b, _ in eligible_pairs if b > 0)
    n_valid_blended = sum(1 for b, _ in blended_pairs if b > 0)
    # AC-P1g suppression on incomplete population (PM 1b): ONLY active when
    # the caller supplied the planned denominator (the run path) — a
    # population that lost items before judging publishes NO percentage on
    # either field, so a `parity_holds: false` artifact can never sit next
    # to a sellable headline. Stats-helper callers (no denominator) keep
    # the pre-1b semantics.
    population_incomplete = (n_eligible_planned is not None
                             and not population_complete)
    # P2: per-category + ratified headline-carve emission. Each block is
    # computed over the ELIGIBLE subset of its population (the ineligible
    # corpus is byte-identical arms by gate design — folding it in would
    # dilute, not measure). Every block carries the full AC-P1g triplet
    # and its own quality_parity; the carve also ships the ratified
    # calibration caveat. RAG lands here with its parity failure attached:
    # 26.18pp measured, suppressed-pending-fix by the contract — visible,
    # never sellable.
    #
    # Subset planned populations: with planned_categories supplied (the
    # run path — main() passes {id: category} for every planned item),
    # each subset's planned ids come from the PLANNED items' categories,
    # never from intersecting with the surviving rows (a lost item never
    # reaches `valid`, so an intersection would re-anchor on survivors —
    # the exact false-green AC-P1b's planned anchor forbids). Stats-helper
    # callers without the map keep the survivor-derived fallback.
    def _planned_for(pred) -> list[str] | None:
        if planned_judge_ids is None:
            return None
        if planned_categories is None:
            # No category map: the planned population cannot be scoped to
            # a subset (a lost item's category is unknowable from rows) —
            # fall back to the attempted-only anchor, exactly the pre-1b
            # stats-helper semantics the top-level gate keeps.
            return None
        return [pid for pid in planned_judge_ids
                if pred(planned_categories.get(pid) or "unknown")]

    categories = sorted({(r.get("category") or "unknown")
                         for r in valid_entries})
    by_category = {
        "label": ("per-category figures over the ELIGIBLE subset of each "
                  "category (AC-P1 'report per-category'; the ineligible "
                  "corpus is byte-identical arms by gate design). Each "
                  "block carries the AC-P1g publication contract and its "
                  "own quality_parity."),
        "categories": {
            cat: _subset_block(
                f"eligible {cat} subset",
                f"eligible {cat} prompts (gate-fired)",
                [r for r in eligible
                 if (r.get("category") or "unknown") == cat],
                planned_ids_for_sub=_planned_for(
                    lambda c, _cat=cat: c == _cat))
            for cat in categories
        },
    }
    carve_sub = [r for r in eligible
                 if (r.get("category") or "unknown")
                 not in CARVE_EXCLUDED_CATEGORIES]
    headline_carve = _subset_block(
        "RATIFIED headline (option (a), user 2026-09-17): eligible "
        "non-code non-RAG — the published claim",
        "eligible non-code non-RAG prompts (gate-fired)",
        carve_sub,
        planned_ids_for_sub=_planned_for(
            lambda c: c not in CARVE_EXCLUDED_CATEGORIES),
        calibration_caveat=CARVE_CALIBRATION_CAVEAT)
    return {
        "headline_population": "eligible_subset",
        "n_eligible": len(eligible),
        "headline": {
            "mean_output_reduction_pct": round(headline["mean_reduction_pct"], 2),
            "ci95_halfwidth": round(headline["ci95"], 2),
            "ci95_interval": [round(v, 2) for v in headline["ci95_interval"]],
            "meets_15pct": bool(headline["mean_reduction_pct"] >= 15
                                and headline["mean_reduction_pct"] - headline["ci95"] >= 15),
            **_publication(headline, n_valid_headline,
                           population_incomplete=population_incomplete),
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
            **_publication(blended, n_valid_blended,
                           population_incomplete=population_incomplete),
        },
        "headline_carve": headline_carve,
        "by_category": by_category,
        "quality_parity": {
            "n_judged": len(judged),
            "n_attempted": len(attempted),
            # AC-P1b planned-eligible anchor (PM option (a) ratification):
            # the denominator is the population the run PLANNED to judge
            # via sampling_plan() — n=15 in the ruled eligible-only shape,
            # n=55 under the --full-corpus owner override — never the
            # survivor count. None only when the caller omits it (stats
            # helpers); main() always supplies it.
            "n_eligible_planned": n_eligible_planned,
            "upstream_lost_ids": upstream_lost_ids,
            "population_complete": population_complete,
            "excluded_judge_ids": excluded_judge_ids,
            "n_regressions_over_1pt": len(regressions),
            "mean_regression_pt": mean_regression_pt,
            # AC-P1b ratified rule: mean regression <= 1pt across the judged
            # population (both-orders averaged judge evidence), not zero
            # individual items over 1pt. Fails closed unless EVERY item in
            # the PLANNED judged population completed both orders — no
            # partial-population greens, and no anchoring on survivors.
            "parity_rule": "mean_regression_le_1pt_full_population",
            "parity_holds": (population_complete
                             and mean_regression_pt is not None
                             and mean_regression_pt <= 1.0),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    # B4 instrument swap (RATIFIED): the default --model must be the
    # K-3-pinned ratifed slug. The archived GLM instrument is withdrawn
    # for the paid run (78.1% reasoning share => null-by-construction);
    # a bare model NAME misses the pricing table — the exact SLUG is
    # required. Guarded by
    # test_c9_p1_model_default_is_the_ratified_instrument_slug.
    ap.add_argument("--model", default="google/gemini-3.5-flash-lite")
    ap.add_argument("--out", default=str(ROOT / "results"))
    # AC-P1b supersession: default = the new artifact DEFERS to the current
    # publication authority (a reproduction run never demotes it — PM
    # ruling 0c96677). Only a run ratified as the replacement publication
    # passes this flag, which stamps the previous authority and archives
    # older generations.
    ap.add_argument("--supersede-authority", dest="supersede_authority",
                    action="store_true",
                    help="RATIFIED REPLACEMENT ONLY: make this run the "
                         "publication authority (stamps the previous "
                         "authority, archives older generations). Without "
                         "it the run defers to the current authority.")
    # AC-P6f artifact contract (PM blocker ruling, 2026-09-18): the tripwire's
    # load_calibration_band() consumes calibration_<model>_<stamp>.json with
    # the OUTPUT-token band; the benchmark artifact alone never arms it. This
    # flag makes the SAME run emit that artifact from the same measured pairs
    # — the gate and the tripwire read one source of truth, and the unit is
    # stamped ("metric": "output_tokens") so the input/output mismatch class
    # cannot recur silently.
    ap.add_argument("--emit-calibration", dest="emit_calibration",
                    action="store_true",
                    help="Also write calibration_<model>_<stamp>.json — the "
                         "AC-P6c band artifact the /api/tripwire dose-drift "
                         "rule consumes (bounded-arm OUTPUT-token band).")
    ap.add_argument("--calibration-tier", dest="calibration_tier",
                    default="bounded",
                    help="Dose tier the treatment arm was pinned to for the "
                         "calibration run (default: bounded).")
    # P6-3 tier pin, harness side (PM blocker ruling, 2026-09-18): the pin
    # previously existed only on run_one/run_arm signatures and was NEVER
    # passed by main() — a calibration run would silently measure
    # treatment == baseline (band of ~0pp consumed as calibrated truth).
    # This flag is the ONLY way the pin leaves the harness; the proxy
    # honors it solely behind ALLOW_DOSE_PIN=true (benchmark-only), so
    # production traffic can never self-raise a tier. --emit-calibration
    # additionally REFUSES to run unless the pin is set AND equals the
    # tier the artifact would claim (enforced in main()).
    ap.add_argument("--dose-pin", dest="dose_pin", default=None,
                    help="Force the proxy's dose tier to this value on "
                         "every request (x-token-saver-dose-pin header). "
                         "REQUIRED for calibration runs; honored by the "
                         "proxy only when that deployment sets "
                         "ALLOW_DOSE_PIN=true. Never point a pinned run "
                         "at a production deployment.")
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


# --- AC-P6f calibration artifact (PM blocker ruling, 2026-09-18) ------------

def _bootstrap_mean_ci(values: list[float]) -> list[float]:
    """95% CI of the MEAN via paired-bootstrap conventions (same seed and
    resample count as estimator.estimate, so both intervals in the artifact
    come from one deterministic instrument)."""
    from estimator import BOOTSTRAP_RESAMPLES, _BOOTSTRAP_SEED

    n = len(values)
    if n < 2:
        m = sum(values) / n if values else 0.0
        return [m, m]
    rng = random.Random(_BOOTSTRAP_SEED)
    means = sorted(
        sum(rng.choices(values, k=n)) / n for _ in range(BOOTSTRAP_RESAMPLES))
    return [means[int(0.025 * (BOOTSTRAP_RESAMPLES - 1))],
            means[int(0.975 * (BOOTSTRAP_RESAMPLES - 1))]]


def emit_calibration_artifact(
    results: list[dict],
    out_dir: Path,
    model: str,
    tier: str,
    sampling_mode: str,
    source_artifact: str,
    population_ids: list[str] | None = None,
) -> Path:
    """Write calibration_<model>_<stamp>.json — the AC-P6f band artifact.

    Built from the SAME measured pairs the benchmark artifact carries
    (treatment arm = the pinned dose tier), restricted to the grounded
    population when ``population_ids`` is given. The unit is stamped
    explicitly: the band is on realized OUTPUT-token reduction — the
    quantity AC-P6c calibrates — never input compression.
    """
    paired = [
        r for r in results
        if r.get("baseline_ok") and r.get("treatment_ok")
        and (r.get("baseline_tokens") or 0) > 0
        # AC-P6f blocker 4 (PM, 2026-09-18): a gate-ineligible row has
        # treatment_k=0 — its treatment arm was NEVER sampled, yet
        # entry_from_arms records treatment_tokens=0.0 with
        # treatment_ok=True. Without this check such rows enter the band
        # as 100% cuts (measured: a real ~30% band poisoned to 73% mean,
        # and a bounded_output_tokens floor of ~47 against a real ~700 —
        # which would silently disarm the dose-drift tripwire forever).
        # An unsampled treatment arm is not evidence; it is absence of
        # evidence.
        and r.get("treatment_sampled", True)
        and (population_ids is None or r.get("id") in population_ids)
    ]
    if not paired:
        raise ValueError(
            "emit_calibration: no valid paired rows in the calibration "
            "population — refusing to write an empty band artifact")
    pairs = [(r["baseline_tokens"], r["treatment_tokens"]) for r in paired]
    reduction = estimate(pairs)
    tokens = [float(r["treatment_tokens"]) for r in paired]
    artifact = {
        "artifact_kind": "ac_p6c_calibration",
        "tier": tier,
        "metric": "output_tokens",
        "model": model,
        "sampling_mode": sampling_mode,
        "source_artifact": source_artifact,
        # AC-P6k uses the same explicit authority protocol as benchmark
        # artifacts. A fresh calibration is authoritative only until a
        # ratified successor points it elsewhere.
        "superseded_by": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "population": {"n": len(paired),
                       "ids": sorted(r["id"] for r in paired)},
        # OUTPUT-token reduction (treatment vs baseline, per-prompt, k=30
        # aggregated) — the band the dose-drift tripwire compares against.
        "output_reduction_pct": {
            "mean": round(reduction["mean_reduction_pct"], 2),
            "ci95_interval": [round(v, 2) for v in reduction["ci95_interval"]],
        },
        "cut_pct_band": [round(v, 2) for v in reduction["ci95_interval"]],
        # Absolute bounded-arm output-token distribution for the live
        # distributional drift compare (per-request counterfactual output
        # does not exist in production; drift is measured on the mean).
        "bounded_output_tokens": {
            "mean": round(sum(tokens) / len(tokens), 2),
            "ci95_interval": [round(v, 2) for v in _bootstrap_mean_ci(tokens)],
            "n": len(tokens),
        },
    }
    # P6-3 parity limb (PM blocker, 2026-09-19): the calibration artifact
    # publishes the band for a population whose quality it must also
    # account for — an artifact that omits the parity limb it fails is
    # exactly the measurement defect the AC-P1g brand exists to prevent.
    # The block reuses _subset_parity verbatim (same sign conventions,
    # same <=1pt gate) so it cannot drift from the benchmark artifact, and
    # is emitted UNCONDITIONALLY: a red verdict ships beside the band, it
    # never silently drops the block. parity_holds None = no judge
    # evidence in the population rows (vacuous, reported as such).
    # P6-3 hole #2 (PM re-verification of a7fe4e0): `paired` is filtered
    # BEFORE parity runs, so anchoring on it alone greens a gutted
    # population — rows lost upstream (arm failure, 429) vanish without a
    # trace and a 3-of-5 population publishes "PARITY: PASS". That is the
    # exact AC-P1b false-green the benchmark path bans. `population_ids`
    # is the PLANNED grounded population: passing it as the planned anchor
    # makes upstream_lost_ids visible and fails the limb closed. When the
    # caller supplies no planned population (None), the pre-1b
    # attempted-only semantics stay — an unplanned ad-hoc emit cannot
    # manufacture a denominator it never had.
    cal_parity = _subset_parity(paired, population_ids)
    cal_parity.pop("_population_incomplete", None)
    cal_parity["parity_rule"] = "mean_regression_le_1pt_calibration_population"
    cal_parity["scope"] = ("parity of the calibration population itself "
                           "(the same paired rows the band above is built "
                           "from) at the pinned tier")
    artifact["quality_parity"] = cal_parity
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"calibration_{model.replace('/', '_')}_{stamp}.json"
    path.write_text(json.dumps(artifact, indent=2))
    qp = artifact["quality_parity"]
    if qp["parity_holds"] is False:
        print(f"Calibration PARITY: FAIL (mean_regression_pt="
              f"{qp['mean_regression_pt']}, regressions over 1pt: "
              f"{qp['n_regressions_over_1pt']}/{qp['n_judged']}) — band "
              f"published with its red parity limb attached")
    elif qp["parity_holds"] is None:
        print("Calibration PARITY: NO JUDGE EVIDENCE in population rows "
              "(parity_holds=null)")
    else:
        print("Calibration PARITY: PASS")
    return path


def planned_judged_ids(plans: list[tuple[dict, bool]],
                       eligible_only: bool) -> list[str]:
    """AC-P1b planned judged population (PM derivation, option (a)): the
    IDs sampling_plan() PLANS to judge — n=15 in the ruled eligible-only
    shape, n=55 under the --full-corpus owner override. Derived from the
    plan, never hardcoded, so the parity gate holds verbatim at n=15 in
    the ruled shape without breaking the owner-override path (a literal
    `n_judged == 15` would fail a fully-clean 55-item override run, and a
    `>= 15` reading would green a full-corpus run that lost 40 items)."""
    return [p["id"] for p, e in plans
            if sampling_plan(e, eligible_only)["judge"]]


# AC-P1b extension (P1 artifact supersession, PM 2026-09-18): every
# committed artifact carries `superseded_by` — the publication authority
# carries null, every other artifact points at it. Supersession is a
# RATIFICATION act, not a newness side effect (PM ruling on the P7
# reproduction, commit 0c96677: a corroborating run does NOT replace the
# published artifact). Default on every run: the new artifact DEFERS to
# the current authority (its superseded_by names the authority). Only
# `--supersede-authority` (a run ratified as the replacement publication,
# like the P1-1 005625Z -> 040421Z swap) stamps the previous authority,
# archives older generations to results/archive/, and makes the new
# artifact authoritative. Either way results/ holds at most the
# authoritative pair and no consumer needs commit messages.
ARCHIVE_SUBDIR = "archive"


def _artifact_authoritative_names(results_dir: Path) -> list[str]:
    """Names of benchmark artifacts with no supersession pointer yet."""
    names = []
    for p in sorted(results_dir.glob("benchmark_*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("superseded_by") is None:
            names.append(p.name)
    return names


def stamp_supersession(results_dir: Path, new_name: str) -> dict:
    """Apply the supersession lifecycle for a freshly written artifact.

    Returns {"supersedes": [...], "archived": [...]} for the run summary.
    """
    archive_dir = results_dir / ARCHIVE_SUBDIR
    archived: list[str] = []
    supersedes: list[str] = []
    for p in sorted(results_dir.glob("benchmark_*.json")):
        if p.name == new_name:
            continue
        data = json.loads(p.read_text())
        target = data.get("superseded_by")
        if target is not None:
            # An earlier generation: it already names a successor that is
            # itself now superseded — keep it as history, out of the
            # live results pair.
            archive_dir.mkdir(parents=True, exist_ok=True)
            p.rename(archive_dir / p.name)
            archived.append(p.name)
        else:
            data["superseded_by"] = new_name
            p.write_text(json.dumps(data, indent=2))
            supersedes.append(p.name)
    return {"supersedes": supersedes, "archived": archived}


def main() -> int:
    args = build_parser().parse_args()
    eligible_only = args.mode == "eligible_only"

    # P6-3 tier pin, harness-side guard (PM blocker ruling, 2026-09-18):
    # --emit-calibration writes a band artifact /api/tripwire consumes as
    # calibrated truth, so it may only run when the treatment arm was
    # ACTUALLY pinned to the tier the artifact claims. Without this, a run
    # with no --dose-pin resolves every grounded fidelity-critical fixture
    # to tier "none" (grounded_calibration_green=False), treatment ==
    # baseline, and the artifact publishes a ~0pp band as the calibration.
    # Refusal happens BEFORE any spend: no health check, no provider call,
    # no artifact.
    if args.emit_calibration:
        if args.dose_pin is None:
            print("ERROR: --emit-calibration requires --dose-pin: the "
                  "calibration band is measured on the PINNED dose tier, "
                  "and an unpinned run resolves grounded fidelity-critical "
                  "fixtures to tier 'none' (treatment == baseline).",
                  file=sys.stderr)
            return 1
        if args.dose_pin != args.calibration_tier:
            print(f"ERROR: --emit-calibration refuses a tier mismatch: "
                  f"the run pins the treatment arm to '{args.dose_pin}' "
                  f"but the artifact would claim tier "
                  f"'{args.calibration_tier}'. The tripwire would calibrate "
                  f"drift against a band the arm never measured.",
                  file=sys.stderr)
            return 1

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
    # AC-P6f blocker 4 (PM, 2026-09-18): the calibration population is
    # grounded AND gate-ELIGIBLE — ineligible grounded fixtures get
    # treatment_k=0 under sampling_plan, so their treatment arm is never
    # sampled and can contribute only fabricated 100%-cut rows to the
    # band. This check runs BEFORE any provider call (zero spend on
    # refusal); the treatment_sampled filter inside
    # emit_calibration_artifact is the second layer behind it.
    if args.emit_calibration:
        cal_ids = [p["id"] for p, e in plans
                   if e and grounded_risk_of(p) != "none"]
        if not cal_ids:
            print("ERROR: --emit-calibration refuses to run: the pinned "
                  "population (grounded AND gate-eligible prompts) is "
                  "empty — no row in this run can carry a real "
                  "treatment-arm measurement for the band.",
                  file=sys.stderr)
            return 1
    n_eligible = sum(1 for _, e in plans if e)
    # AC-P1b planned judged population (PM derivation, option (a)): the
    # parity denominator is what sampling_plan() PLANNED to judge — n=15
    # in the ruled eligible-only shape, n=55 under the --full-corpus
    # owner override — derived, never hardcoded, so the gate holds
    # verbatim in the ruled shape without breaking the owner-override
    # path. planned_judge_ids goes into summarize() so upstream-lost IDs
    # (arms failed before judging) are recorded beside excluded_judge_ids.
    planned_judge_ids = planned_judged_ids(plans, eligible_only)
    n_judge_planned = len(planned_judge_ids)
    # P2: the planned items' categories — the only way a subset (carve,
    # per-category) can anchor its planned population without intersecting
    # with survivors (a lost item never reaches `valid`).
    planned_categories = {p["id"]: (p.get("category") or "unknown")
                          for p, _ in plans}
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
        # P6-3 tier pin: passed to BOTH arms for symmetric evidence — the
        # baseline arm (conciseness 0) ignores it by design; the treatment
        # arm's tier is forced to the pin behind the proxy's
        # ALLOW_DOSE_PIN gate. Never set here unless --dose-pin was given.
        base = run_arm(client, args.base_url, args.model, p,
                       conciseness=False, k=plan["baseline_k"],
                       dose_pin=args.dose_pin)
        treat = run_arm(client, args.base_url, args.model, p,
                        conciseness=True, k=plan["treatment_k"],
                        dose_pin=args.dose_pin)
        entry = entry_from_arms(p, eligible, base, treat)
        # PM v4 (consequence 2): per-sample reasoning evidence is published
        # with the entry, so the headline's composition is auditable.
        entry["reasoning_evidence"] = {
            "arm_control": _reasoning_control(args.model),
            "baseline_samples": base.get("samples_evidence", []),
            "treatment_samples": treat.get("samples_evidence", []),
        }
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

    # Zero-pairs abort: if NO pair survived, the harness never reached the
    # provider (bad key, wrong upstream, network down). summarize([]) would
    # publish a clean-looking no_measurable_effect / null figure — a total
    # connection failure serializing identically to a real null result.
    # Abort instead: non-zero exit, NO results file.
    if not valid:
        print(f"FATAL: 0/{len(results)} pairs survived — the harness never "
              f"measured anything (auth/upstream/network failure?). No "
              f"results file written.", file=sys.stderr)
        return 1

    # Production estimator — SHARED with the calibration gate (estimator.py).
    # The gate and the re-run must measure the same math; a divergence here is
    # the defect class the AC-P1a-gate exists to catch.
    stats = summarize(valid, n_eligible_planned=n_judge_planned,
                      planned_judge_ids=planned_judge_ids,
                      planned_categories=planned_categories)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fixture_checksum": checksum,
        "fixture_checksum_pinned": EXPECTED_FIXTURE_SHA256,
        "temperature": TEMPERATURE,
        "harness_k": HARNESS_K,
        "sampling_mode": args.mode,
        # P6-3 audit stamp: records whether (and to what tier) this run
        # pinned the treatment arm. Null on unpinned runs — a calibration
        # artifact whose source shows dose_pin=null must never exist (see
        # the main() emit-calibration guard).
        "dose_pin": args.dose_pin,
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
    # AC-P1b supersession extension: DEFAULT = defer to the current
    # publication authority (reproduction runs never demote it — PM
    # ruling 0c96677). Only --supersede-authority (a run ratified as the
    # replacement publication) stamps the previous authority and archives
    # older generations via stamp_supersession().
    prior_authorities = _artifact_authoritative_names(out)
    if args.supersede_authority:
        summary["superseded_by"] = None
        summary["supersedes"] = prior_authorities
        path.write_text(json.dumps(summary, indent=2))
        supersession = stamp_supersession(out, path.name)
        if supersession["archived"]:
            print(f"Supersession: archived {len(supersession['archived'])} "
                  f"older generation(s) to results/{ARCHIVE_SUBDIR}/")
    else:
        # Defer: point at the current authority; with zero authorities on
        # disk (fresh results dir) this run IS the authority (null).
        summary["superseded_by"] = (prior_authorities[0]
                                    if len(prior_authorities) == 1 else None)
        summary["supersedes"] = []
        path.write_text(json.dumps(summary, indent=2))
    if args.emit_calibration:
        # AC-P6f artifact contract: same run, same measured pairs, OUTPUT
        # unit stamped. Restricted to the grounded population (the corpus
        # AC-P6c calibrates); a run with zero grounded rows refuses here.
        # Blocker 4: SAME population as the pre-spend check above —
        # grounded AND gate-eligible (unsampled treatment arms excluded
        # again inside emit_calibration_artifact via treatment_sampled).
        cal_ids = [p["id"] for p, e in plans
                   if e and grounded_risk_of(p) != "none"]
        cal_path = emit_calibration_artifact(
            results, out, args.model, args.calibration_tier, args.mode,
            path.name, population_ids=cal_ids or None)
        print(f"Calibration artifact (AC-P6f band, output-token unit) "
              f"written to {cal_path}")
    hl = stats["headline"]
    bl = stats["blended_corpus_wide"]
    # AC-P1g: the CLI prints the PUBLISHED fields and nothing else. Reading
    # mean_output_reduction_pct here (pre-C-7c) printed a bare "1.00%" for a
    # result the JSON itself suppressed as no_measurable_effect — the same
    # noise-dressed-as-signal defect one surface over. The figure printed is
    # reported_reduction_pct verbatim, so stdout and results JSON cannot
    # diverge by construction.
    print(f"\nHEADLINE (eligible subset, n={stats['n_eligible']}): "
          f"{_published_figure(hl)}")
    # P2: the ratified carve is a first-class published figure — it goes to
    # stdout through the same _published_figure() contract (the printed
    # figure is reported_reduction_pct verbatim, never the raw mean).
    carve = stats["headline_carve"]
    print(f"HEADLINE CARVE ({carve['population']}, n={carve['n_eligible']}): "
          f"{_published_figure(carve)}")
    for cat, blk in stats["by_category"]["categories"].items():
        print(f"  [{cat}] eligible n={blk['n_eligible']}: "
              f"{_published_figure(blk)}")
    print(f"Blended (corpus-wide, n={bl['n']}, labelled, not the headline): "
          f"{_published_figure(bl)}")
    qp = stats["quality_parity"]
    print(f"Quality parity: {qp['n_judged']}/{qp['n_attempted']} judged "
          f"of {qp['n_eligible_planned']} planned (both orders averaged), "
          f"{len(qp['upstream_lost_ids'])} upstream-lost, "
          f"{qp['n_regressions_over_1pt']} >1pt regressions (diagnostic), "
          f"mean regression {qp['mean_regression_pt']}pt "
          f"-> {'PASS' if qp['parity_holds'] else 'FAIL'} "
          f"(rule: {qp['parity_rule']})")
    print(f"AC-P1 target on HEADLINE (>=15% mean, CI lower bound >=15): "
          f"{'MET' if hl['meets_15pct'] else 'NOT MET'}")
    print(f"Results written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
