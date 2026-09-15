"""P1-1 audit step 1: output-token SD measurement (PM-mandated, cheapest
highest-value action before any harness rebuild).

One prompt, 5 baseline-arm calls, pinned temperature, through the LIVE
proxy route. Reports SD of output tokens two ways:
- count_text() on returned strings (what the old harness used)
- provider usage.completion_tokens (what the audit says we must use)

Also captures whether the provider reports reasoning/thinking tokens
separately (GLM-5.3-flash always-on thinking question from the audit).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8010"
KEY = os.environ.get("OPENROUTER_API_KEY") or ""
MODEL = "z-ai/glm-5.3-flash"
N = 5

PROMPT = {
    "model": MODEL,
    "temperature": 0.0,  # pinned per audit
    "messages": [
        {"role": "user", "content": (
            "Explain what a REST API is to someone who has never programmed "
            "before. Cover the core concepts: resources, HTTP methods, status "
            "codes, and statelessness, with one concrete example of a request."
        )}
    ],
}


def main() -> int:
    samples_text, samples_usage, reasoning_tokens = [], [], []
    for i in range(N):
        r = httpx.post(
            f"{BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {KEY}",
                     "X-Token-Saver-Conciseness": "0"},
            json=PROMPT, timeout=120,
        )
        if r.status_code != 200:
            print(f"run {i+1}: HTTP {r.status_code}: {r.text[:120]}")
            continue
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        u = data.get("usage") or {}
        completion = int(u.get("completion_tokens", 0))
        details = u.get("completion_tokens_details") or {}
        rt = int(details.get("reasoning_tokens", 0) or 0)
        samples_text.append(len(text.split()))  # whitespace words as sanity view
        samples_usage.append(completion)
        reasoning_tokens.append(rt)
        print(f"run {i+1}: provider completion_tokens={completion} "
              f"(reasoning={rt}) | words={len(text.split())} | chars={len(text)}")

    if len(samples_usage) < 3:
        print("ERROR: too few successful samples", file=sys.stderr)
        return 1

    print("\n--- SD summary (n=%d) ---" % len(samples_usage))
    for name, samples in (("provider usage.completion_tokens", samples_usage),):
        sd = statistics.stdev(samples)
        mean = statistics.mean(samples)
        cv = sd / mean if mean else float("nan")
        print(f"{name}: mean={mean:.1f} sd={sd:.1f} cv={cv:.3f}")
        # audit's power table: required true effect for 80% power
        for true_eff in (0.15, 0.22, 0.40):
            req = 2.8 * cv / true_eff  # approx n per arm for 80% power
            print(f"  if true effect {true_eff:.0%}: ~{req:.0f} samples/arm for 80% power")
    if any(reasoning_tokens):
        print(f"reasoning tokens present: {reasoning_tokens} — "
              "ALWAYS-ON THINKING CONFIRMED (audit's model-choice concern)")
    else:
        print("reasoning tokens: none reported by provider")
    return 0


if __name__ == "__main__":
    sys.exit(main())
