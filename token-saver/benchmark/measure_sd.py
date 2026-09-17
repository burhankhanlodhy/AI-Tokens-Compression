"""P1-1 audit step 1: output-token SD measurement (PM-mandated, cheapest
highest-value action before any harness rebuild).

One prompt, N baseline-arm calls, pinned temperature, through the LIVE
proxy route. Reports SD/CV of output tokens via provider
usage.completion_tokens (what the audit says we must use).

Also captures whether the provider reports reasoning/thinking tokens
separately (GLM-5.3-flash always-on thinking question from the audit).

C0: parameterized for per-model SD gating (Track C2):
    --model   candidate model id (default: z-ai/glm-5.3-flash)
    --n       number of calls (default: 5)
    --base-url proxy base URL
    --out     directory for the per-model results JSON
The results file records model, n, fixture-free config, raw samples and
CV so C2's billed-token CV < 0.35 gate can be evaluated from the artifact.
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
MODEL = "z-ai/glm-5.3-flash"
N = 5

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
    ap = argparse.ArgumentParser(description="Per-model SD gate (C0/C2)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--n", type=int, default=N)
    ap.add_argument("--base-url", default=BASE)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    return ap.parse_args(argv)


def run_sample(model: str, base_url: str, key: str) -> dict | None:
    """One baseline-arm call. Returns provider-usage sample or None on failure.

    Also records the proxy's x-token-saver-reasoning evidence header:
      injected                      -> the `reasoning:{enabled:false}`
                                       override was actually sent upstream
      rejected_retry_without_override -> upstream 400'd the override and the
                                       proxy retried WITHOUT it (the override
                                       never took effect for this sample)
    """
    try:
        r = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "X-Token-Saver-Conciseness": "0"},
            json={**PROMPT, "model": model}, timeout=120,
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
    override = (r.headers.get("x-token-saver-reasoning") or "").strip().lower()
    injected = override == "injected"
    rejected = override.startswith("rejected")
    print(f"provider completion_tokens={completion} (reasoning={rt}) | "
          f"words={len(text.split())} | chars={len(text)} | "
          f"reasoning-override={override or 'none'}")
    return {"completion_tokens": completion, "reasoning_tokens": rt,
            "words": len(text.split()), "chars": len(text),
            "reasoning_override_injected": injected,
            "reasoning_override_rejected": rejected}


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
    field_accepted: bool | None,
) -> dict:
    """P1-1 SD gate, post-swap semantics (PM ruling at 9b1ac2d).

    Two recorded facts, kept separate:
      - reasoning_field_accepted: did the upstream ACCEPT our
        `reasoning:{enabled:false}` override (True), REJECT it via the
        proxy's 400-retry path (False), or was no override sent at all
        (None — e.g. disable_reasoning_by_default off or a client-supplied
        reasoning field)?
      - reasoning_tokens_observed: the raw per-sample counts. ZERO is the
        PASS value: a working suppression shows all zeros; a SILENT mapping
        failure (override ignored, model thinks anyway) shows nonzero.
    Suppression is CONFIRMED only when the field was accepted AND every
    observed reasoning count is zero. `qualifies` keys off confirmation,
    never off "reasoning tokens were seen" (the old v1 predicate, which
    rewarded the silent-failure mode and punished the working one).
    """
    suppression_confirmed = bool(field_accepted is True and reasoning_tokens
                                 and all(rt == 0 for rt in reasoning_tokens))
    return {
        "billed_cv": summary["cv"],
        "cv_lt_0.35": bool(summary["cv"] < 0.35),
        "reasoning_field_accepted": field_accepted,
        "reasoning_tokens_observed": reasoning_tokens,
        "suppression_confirmed": suppression_confirmed,
        "qualifies": bool(summary["cv"] < 0.35 and suppression_confirmed),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    samples_text, samples_usage, reasoning_tokens, raw = [], [], [], []
    injected_flags, rejected_flags = [], []
    for _ in range(args.n):
        s = run_sample(args.model, args.base_url, KEY)
        if s is None:
            continue
        samples_text.append(s["words"])
        samples_usage.append(s["completion_tokens"])
        reasoning_tokens.append(s["reasoning_tokens"])
        injected_flags.append(s["reasoning_override_injected"])
        rejected_flags.append(s["reasoning_override_rejected"])
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
    if all(rejected_flags):
        field_accepted: bool | None = False
    elif any(rejected_flags):
        field_accepted = False
    elif any(injected_flags):
        field_accepted = True
    else:
        field_accepted = None  # no override was sent on any sample

    gate = compute_gate(summary, reasoning_tokens, field_accepted)
    if field_accepted is False:
        print("reasoning override REJECTED by upstream (proxy retried without "
              "it) — suppression NOT confirmed")
    elif field_accepted is None:
        print("no reasoning override was sent on any sample — "
              "suppression CANNOT be attributed")
    elif gate["suppression_confirmed"]:
        print("reasoning override accepted AND all reasoning-token counts are "
              "zero — SUPPRESSION CONFIRMED")
    else:
        print("reasoning override accepted but reasoning tokens observed "
              f"({reasoning_tokens}) — suppression FAILED (silent mapping loss)")

    # C2 gate fields (schema v2): billed-token CV < 0.35 AND suppression
    # CONFIRMED (field accepted + zero observed reasoning). Failing gate is
    # a recorded negative result. reasoning_separately_reported is kept as a
    # diagnostic (v1 field) but no longer gates qualification.
    result = {
        "schema": "sd_gate_v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "n_requested": args.n,
        "n_successful": len(samples_usage),
        "temperature": PROMPT["temperature"],
        "conciseness": "disabled",
        "base_url": args.base_url,
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
    print(f"C2 gate: CV={summary['cv']:.3f} "
          f"({'PASS' if gate['cv_lt_0.35'] else 'FAIL'} <0.35) | "
          f"suppression confirmed: {gate['suppression_confirmed']} | "
          f"QUALIFIES: {gate['qualifies']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
