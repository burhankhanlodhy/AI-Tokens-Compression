"""P1-1 SD gate, schema v4 (PM ruling: bounded-and-visible, cap 1/3).

One prompt, N baseline-arm calls at pinned temperature 0.0, through the
LIVE proxy route. Gates spend on:

  qualifies = CV < 0.35
              AND reasoning_field_accepted     (control demonstrably applied)
              AND reasoning_field_present      (provider reports the count,
                                                every sample)
              AND reasoning_share <= 1/3       (ratio-of-sums:
                                               Σreasoning / Σcompletion)

"Zero" was a proxy for "the treatment can act on what we bill", not the
requirement itself (v1 rewarded loud failure: rt>0 = qualifies; v2 rewarded
quiet failure: field-absent zeros; v3's all-zeros is unsatisfiable on
instruments whose floor is MINIMAL, e.g. Gemini 3.5 Flash-Lite). v4 gates on
hidden reasoning being a BOUNDED MINORITY of billed output — measured in
this artifact, recomputable from the per-sample records, never asserted.

Differential probe (PM trap 1): MINIMAL is Flash-Lite's DEFAULT thinking
level, so field_accepted alone cannot distinguish "our control was applied"
from "our control was ignored, stock behavior". Before the gate's N calls,
the runner sends --probe-n calls at MINIMAL and --probe-n at HIGH on the
same prompt. If reasoning_tokens moves, the control is live; if not, the
record says "bounded by model default, control efficacy not demonstrated"
— which still passes a share-based cap honestly, but is a different claim
to publish, and the artifact says which one it is.

Denominator invariant (PM trap 2): the share assumes reasoning tokens are
INSIDE completion_tokens (glm's archived samples: completion − reasoning
tracks the visible text at 0.95–0.98 of chars/4). If a passthrough reports
them additively instead, the subtraction is wildly out of line with the
returned string and the share is computed off a wrong base. v4 checks the
invariant per sample (visible = completion − reasoning vs chars/4, band
0.5–1.5) and FAILS the gate on violation rather than publishing a share
nobody can trust.

CLI (superset of v3):
    --model      candidate model id
    --n          number of gate calls (default 5)
    --probe-n    calls per probe arm, MINIMAL and HIGH (default 2)
    --skip-probe skip the differential probe (control_efficacy: "skipped")
    --base-url   proxy base URL
    --out        directory for the per-model results JSON
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8010"
SHARE_CAP = 1 / 3          # PM ruling: bounded-and-visible, cap one third
DENOMINATOR_BAND = (0.5, 1.5)  # visible=(completion-reasoning) vs chars/4


def _client_key() -> str:
    """BYOK: client-side key. Prefer env; fall back to the live proxy
    process's env (dev-box convenience — the proxy holds it already)."""
    k = os.environ.get("OPENROUTER_API_KEY") or ""
    if k:
        return k
    try:
        out = subprocess.run(
            ["bash", "-lc",
             "for p in $(pgrep -f 'uvicorn proxy.main' ); do tr '\\0' '\\n' "
             "< /proc/$p/environ 2>/dev/null | grep '^OPENROUTER_API_KEY=' "
             "| head -1; done | head -1"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return out.split("=", 1)[1] if "=" in out else ""
    except Exception:
        return ""


KEY = _client_key()
MODEL = "google/gemini-3.5-flash-lite"
N = 5
PROBE_N = 2
# The control the PROXY injects for the google/ family (proxy/config.py,
# reasoning_control_for). Gate calls send NO explicit control, so
# field_accepted measures the proxy's control; the probe arms send their
# levels explicitly (client fields are always respected by the proxy).
EXPECTED_CONTROL = {"thinking_level": "MINIMAL"}
PROBE_HIGH_CONTROL = {"thinking_level": "HIGH"}

PROMPT = {
    "temperature": 0.0,  # pinned per audit
    "messages": [
        {"role": "user", "content": (
            "Explain what a REST API is to someone who has never programmed "
            "before. Cover the core concepts: resources, HTTP methods, status "
            "codes, and statelessness, with one concrete example of a request."
        )}
    ],
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Per-model SD gate (v4)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--n", type=int, default=N)
    ap.add_argument("--probe-n", type=int, default=PROBE_N)
    ap.add_argument("--skip-probe", action="store_true",
                    help="skip the differential control-efficacy probe")
    ap.add_argument("--base-url", default=BASE)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    return ap.parse_args(argv)


def run_sample(model: str, base_url: str, key: str,
               control: dict | None = None) -> dict | None:
    """One call. `control` (e.g. {"thinking_level": "HIGH"}) is sent as an
    explicit client field when given — the proxy always respects client
    fields; gate calls pass None so the PROXY's control (and its acceptance)
    is what gets measured. Records per-sample:
      - completion/reasoning counts and whether the provider REPORTS a
        reasoning_tokens field (absence is a distinct fact from a zero
        value — a silent model can be thinking at its default level)
      - the proxy's x-token-saver-reasoning evidence header:
          injected:<keys>              -> control was sent upstream
          rejected_retry_without_override -> 400'd, proxy retried bare
          rejected_400_relayed         -> 400'd, relayed raw
      - visible-token denominator evidence (chars/words) for the invariant
    """
    body = {**PROMPT, "model": model}
    if control:
        body.update(control)
    try:
        r = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "X-Token-Saver-Conciseness": "0"},
            json=body, timeout=120,
        )
    except httpx.HTTPError as e:
        print(f"request failed: {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:120]}")
        return None
    data = r.json()
    text = data["choices"][0]["message"]["content"] or ""
    u = data.get("usage") or {}
    completion = int(u.get("completion_tokens", 0))
    details = u.get("completion_tokens_details") or {}
    rt = int(details.get("reasoning_tokens", 0) or 0)
    rt_present = "reasoning_tokens" in details
    header = (r.headers.get("x-token-saver-reasoning") or "").strip().lower()
    print(f"provider completion_tokens={completion} (reasoning={rt}, "
          f"field_present={rt_present}) | words={len(text.split())} | "
          f"chars={len(text)} | reasoning-control-header={header or 'none'}"
          f"{' | explicit control: ' + json.dumps(control) if control else ''}")
    return {"completion_tokens": completion, "reasoning_tokens": rt,
            "reasoning_field_present": rt_present,
            "reasoning_evidence_header": header or None,
            "explicit_control": dict(control) if control else None,
            "words": len(text.split()), "chars": len(text)}


def denominator_check(sample: dict) -> dict:
    """Per-sample denominator invariant (PM trap 2): visible tokens implied
    by (completion − reasoning) must track the returned text (~chars/4).
    Out-of-band => the share's base is untrustworthy => gate fails."""
    visible = sample["completion_tokens"] - sample["reasoning_tokens"]
    est = sample["chars"] / 4.0
    ratio = (visible / est) if est else None
    ok = ratio is not None and DENOMINATOR_BAND[0] <= ratio <= DENOMINATOR_BAND[1]
    return {"visible_tokens": visible, "visible_est_tokens_chars_over_4": est,
            "denominator_ratio": ratio, "denominator_ok": ok}


def evaluate_probe(minimal_rts: list[int], high_rts: list[int]) -> dict:
    """Differential control-efficacy probe (PM trap 1). The control is
    'demonstrated' only if reasoning counts MOVE between the MINIMAL and
    HIGH arms — otherwise the parameter is decorative and the recorded
    boundedness claim must say so."""
    m = (sum(minimal_rts) / len(minimal_rts)) if minimal_rts else None
    h = (sum(high_rts) / len(high_rts)) if high_rts else None
    if m is None or h is None:
        efficacy = "skipped"
        claim = ("control efficacy unknown (probe skipped) — reasoning share "
                 "is measured but unattributed to the control")
    elif h > m:
        efficacy = "demonstrated"
        claim = ("control efficacy demonstrated: reasoning counts move with "
                 "the thinking level; MINIMAL is a live control, not stock "
                 "behavior")
    else:
        efficacy = "not demonstrated"
        claim = ("bounded by model default, control efficacy not "
                 "demonstrated — reasoning counts did not move between "
                 "MINIMAL and HIGH; the parameter may be decorative")
    return {"probe_minimal_reasoning_tokens": minimal_rts,
            "probe_high_reasoning_tokens": high_rts,
            "probe_minimal_mean": m, "probe_high_mean": h,
            "control_efficacy": efficacy, "boundedness_claim": claim}


def summarize(samples: list[int]) -> dict:
    mean = statistics.mean(samples)
    sd = statistics.stdev(samples)
    cv = sd / mean if mean else float("nan")
    out = {"mean": mean, "sd": sd, "cv": cv}
    for true_eff in (0.15, 0.22, 0.40):
        out[f"n_for_80pct_at_{true_eff}"] = 2.8 * cv / true_eff
    return out


def compute_gate(
    summary: dict,
    reasoning_tokens: list[int],
    completion_tokens: list[int],
    field_accepted: bool | None,
    field_present: bool = True,
    *,
    denominator_ok: bool = True,
    share_cap: float = SHARE_CAP,
) -> dict:
    """P1-1 SD gate, v4: bounded-and-visible (PM ruling).

    qualifies = CV < 0.35 AND field_accepted AND field_present (every
    sample) AND reasoning_share <= share_cap, where reasoning_share =
    Σreasoning / Σcompletion (ratio-of-sums, corrected-method lesson) and
    the denominator invariant must be green or the share is not published
    as a pass. The share is recomputable from the recorded per-sample
    counts; nothing here is asserted.
    """
    total_rt = sum(reasoning_tokens)
    total_ct = sum(completion_tokens)
    share = (total_rt / total_ct) if total_ct else None
    share_within_cap = bool(
        denominator_ok and share is not None and share <= share_cap
    )
    bounded = bool(
        field_accepted is True
        and field_present
        and reasoning_tokens
        and share_within_cap
    )
    return {
        "billed_cv": summary["cv"],
        "cv_lt_0.35": bool(summary["cv"] < 0.35),
        "mean_completion_tokens": summary["mean"],
        "reasoning_share_ratio_of_sums": share,
        "share_cap": share_cap,
        "share_within_cap": share_within_cap,
        "denominator_invariant_ok": bool(denominator_ok),
        "reasoning_field_accepted": field_accepted,
        "reasoning_field_present": bool(field_present),
        "reasoning_tokens_observed": reasoning_tokens,
        "reasoning_bounded_confirmed": bounded,
        "qualifies": bool(summary["cv"] < 0.35 and bounded),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # --- Differential control-efficacy probe (before any gate spend) ---
    if args.skip_probe:
        probe = evaluate_probe([], [])
    else:
        minimal_rts: list[int] = []
        high_rts: list[int] = []
        print(f"--- differential probe: {args.probe_n}x MINIMAL, "
              f"{args.probe_n}x HIGH (control-efficacy check) ---")
        for _ in range(args.probe_n):
            s = run_sample(args.model, args.base_url, KEY,
                           control=EXPECTED_CONTROL)
            if s is not None:
                minimal_rts.append(s["reasoning_tokens"])
        for _ in range(args.probe_n):
            s = run_sample(args.model, args.base_url, KEY,
                           control=PROBE_HIGH_CONTROL)
            if s is not None:
                high_rts.append(s["reasoning_tokens"])
        if len(minimal_rts) < 1 or len(high_rts) < 1:
            print("ERROR: probe arms failed — cannot attribute the control",
                  file=sys.stderr)
            return 1
        probe = evaluate_probe(minimal_rts, high_rts)
        print(f"probe: MINIMAL mean={probe['probe_minimal_mean']:.1f} | "
              f"HIGH mean={probe['probe_high_mean']:.1f} | "
              f"control_efficacy: {probe['control_efficacy']}")
        print(f"boundedness claim for the record: {probe['boundedness_claim']}")

    # --- Gate calls: no explicit control; the PROXY's control is measured ---
    samples_usage, reasoning_tokens, raw = [], [], []
    for _ in range(args.n):
        s = run_sample(args.model, args.base_url, KEY)
        if s is None:
            continue
        samples_usage.append(s["completion_tokens"])
        reasoning_tokens.append(s["reasoning_tokens"])
        s.update(denominator_check(s))
        raw.append(s)

    if len(samples_usage) < 3:
        print("ERROR: too few successful samples", file=sys.stderr)
        return 1

    summary = summarize(samples_usage)
    print(f"\n--- SD summary (model={args.model}, n={len(samples_usage)}) ---")
    print(f"provider usage.completion_tokens: mean={summary['mean']:.1f} "
          f"sd={summary['sd']:.1f} cv={summary['cv']:.3f}")
    for true_eff in (0.15, 0.22, 0.40):
        print(f"  if true effect {true_eff:.0%}: "
              f"~{summary[f'n_for_80pct_at_{true_eff}']:.0f} samples/arm for 80% power")

    reasoning_separately_reported = any(rt > 0 for rt in reasoning_tokens)
    accepted_flags = [s["reasoning_evidence_header"].startswith("injected")
                      for s in raw if s["reasoning_evidence_header"]]
    rejected_flags = [s["reasoning_evidence_header"].startswith("rejected")
                      for s in raw if s["reasoning_evidence_header"]]
    present_flags = [s["reasoning_field_present"] for s in raw]
    if any(rejected_flags):
        field_accepted: bool | None = False
    elif accepted_flags and all(accepted_flags):
        field_accepted = True
    else:
        field_accepted = None  # no control evidence on any sample
    denominator_ok = all(s["denominator_ok"] for s in raw)

    gate = compute_gate(summary, reasoning_tokens, samples_usage,
                        field_accepted, field_present=all(present_flags),
                        denominator_ok=denominator_ok)

    share = gate["reasoning_share_ratio_of_sums"]
    share_txt = f"{share:.3f}" if share is not None else "n/a"
    print(f"reasoning share (ratio-of-sums): {share_txt} "
          f"(cap {gate['share_cap']:.3f}) | denominator invariant: "
          f"{'OK' if denominator_ok else 'VIOLATED'}")
    if any(not s["denominator_ok"] for s in raw):
        print("DENOMINATOR INVARIANT VIOLATED: (completion − reasoning) is "
              "out of line with the returned text — reasoning tokens are "
              "likely reported OUTSIDE completion_tokens; the share's base "
              "is untrustworthy and the gate fails closed")
    if field_accepted is False:
        print("injected control REJECTED by upstream (proxy retried without "
              "it) — bounded control NOT applied")
    elif field_accepted is None:
        print("no control evidence on any sample — boundedness CANNOT be "
              "attributed to the control")
    elif not gate["reasoning_field_present"]:
        print("provider did NOT report a reasoning_tokens field on every "
              "sample — the share's numerator is unverifiable")
    print(f"C2 gate: CV={summary['cv']:.3f} "
          f"({'PASS' if gate['cv_lt_0.35'] else 'FAIL'} <0.35) | "
          f"share within cap: {gate['share_within_cap']} | "
          f"reasoning_bounded_confirmed: {gate['reasoning_bounded_confirmed']} | "
          f"QUALIFIES: {gate['qualifies']}")

    result = {
        "schema": "sd_gate_v4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "n_requested": args.n,
        "n_successful": len(samples_usage),
        "temperature": PROMPT["temperature"],
        "conciseness": "disabled",
        "base_url": args.base_url,
        "expected_proxy_control": EXPECTED_CONTROL,
        "probe": probe,
        "samples": raw,
        "summary": summary,
        "reasoning_tokens_samples": reasoning_tokens,
        "reasoning_separately_reported": reasoning_separately_reported,
        "reasoning_field_accepted": gate["reasoning_field_accepted"],
        "c2_gate": gate,
    }
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    safe_model = args.model.replace("/", "__")
    outfile = outdir / f"sd_gate_{safe_model}.json"
    outfile.write_text(json.dumps(result, indent=2) + "\n")
    print(f"results written: {outfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
