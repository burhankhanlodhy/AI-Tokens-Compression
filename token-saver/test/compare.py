"""Side-by-side quality/savings check: direct vs. through the proxy.

Usage:
    python test/compare.py [--proxy http://localhost:8000] [--prompts prompts.json]

Requires a real API key in the environment (OPENROUTER_API_KEY) — the proxy
forwards it, and the direct call uses it too.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.counting import count_messages, count_text  # noqa: E402

UPSTREAM = "https://openrouter.ai/api"  # run_once appends /v1/chat/completions

DEFAULT_PROMPTS = [
    # Conversational / compressible
    {"name": "summarize", "messages": [{"role": "user", "content":
        "Summarize the following article in three bullet points.\n\n" + (
            "The history of the steam engine is a long and fascinating one. "
            "It began with early experiments by Hero of Alexandria, whose aeolipile "
            "demonstrated the principle of reactive steam power, though it was little "
            "more than a curiosity. Centuries later, Thomas Newcomen built the first "
            "practical atmospheric engine in 1712 to pump water out of mines. James "
            "Watt later improved the design dramatically with a separate condenser, "
            "which greatly improved efficiency and made steam power economically "
            "viable across industries. The subsequent development of high-pressure "
            "engines by Richard Trevithick and others enabled locomotives and "
            "steamships, transforming transportation and industry worldwide. "
            "The steam engine ultimately gave way to internal combustion and "
            "electric motors in the twentieth century, but its legacy as the "
            "workhorse of the Industrial Revolution remains unmatched. ") * 3}]},
    {"name": "qa", "messages": [{"role": "user", "content":
        "Based on this context, answer the question briefly.\n\nContext: "
        "Photosynthesis in plants converts light energy into chemical energy, "
        "producing glucose from carbon dioxide and water while releasing oxygen "
        "as a byproduct. It occurs primarily in the chloroplasts, using the "
        "pigment chlorophyll. The light-dependent reactions occur in the "
        "thylakoid membranes, while the Calvin cycle occurs in the stroma. "
        "Question: Where does the Calvin cycle take place?"}]},
    {"name": "brainstorm", "messages": [{"role": "user", "content":
        "Give me five concise name ideas for a developer tool that reduces "
        "LLM API costs. One line each, no explanations."}]},
    # Code — should be routed to passthrough
    {"name": "code-review", "messages": [{"role": "user", "content":
        "Review this function for bugs:\n\n```python\ndef binary_search(arr, target):\n"
        "    lo, hi = 0, len(arr)\n    while lo < hi:\n        mid = (lo + hi) // 2\n"
        "        if arr[mid] < target:\n            lo = mid + 1\n        else:\n"
        "            hi = mid\n    return lo if lo < len(arr) and arr[lo] == target else -1\n```"}]},
    {"name": "code-explain", "messages": [{"role": "user", "content":
        "What does this code do? Be brief.\n\nimport asyncio\n\nasync def main():\n"
        "    await asyncio.gather(*(fetch(i) for i in range(10)))\n\n"
        "async def fetch(i):\n    await asyncio.sleep(0.1)\n    print(i)"}]},
]


def run_once(client: httpx.Client, base_url: str, api_key: str,
             model: str, messages: list[dict]) -> tuple[dict, float]:
    started = time.perf_counter()
    resp = client.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages},
        timeout=120,
    )
    elapsed = time.perf_counter() - started
    resp.raise_for_status()
    return resp.json(), elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://localhost:8000")
    parser.add_argument("--model", default="openai/gpt-4.1-mini")
    parser.add_argument("--prompts", default=None, help="JSON file with a list of {name, messages}")
    parser.add_argument("--out", default=str(Path(__file__).parent / "compare_report.md"),
                        help="Where to write the full (untruncated) answers for review")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        sys.exit("Set OPENROUTER_API_KEY (get one at https://openrouter.ai/keys).")

    prompts = DEFAULT_PROMPTS
    if args.prompts:
        prompts = json.loads(Path(args.prompts).read_text(encoding="utf-8"))

    report = [
        f"# compare.py report\n",
        f"model: {args.model}  \n",
        f"generated: {datetime.now().isoformat(timespec='seconds')}\n\n",
    ]

    with httpx.Client() as client:
        print(f"{'prompt':<14} {'direct in/out':>14} {'proxy in/out':>14} "
              f"{'saved':>7} {'latency':>9}")
        print("-" * 62)
        for p in prompts:
            try:
                direct, t_direct = run_once(client, UPSTREAM,
                                            api_key, args.model, p["messages"])
                proxied, t_proxy = run_once(client, args.proxy,
                                            api_key, args.model, p["messages"])
            except httpx.HTTPError as e:
                print(f"{p['name']:<14} ERROR: {e}")
                report.append(f"## {p['name']}\n\nERROR: {e}\n\n---\n\n")
                continue

            direct_text = direct["choices"][0]["message"]["content"] or ""
            proxied_text = proxied["choices"][0]["message"]["content"] or ""

            # Prefer each call's own real usage from its provider; only fall
            # back to a local tiktoken estimate if usage is missing, so the
            # "saved" delta compares like-for-like real accounting.
            d_in = direct.get("usage", {}).get(
                "prompt_tokens", count_messages(p["messages"], args.model))
            d_out = direct.get("usage", {}).get(
                "completion_tokens", count_text(direct_text, args.model))
            p_in = proxied.get("usage", {}).get("prompt_tokens", d_in)
            p_out = proxied.get("usage", {}).get("completion_tokens", d_out)
            d_usage = json.dumps(direct.get("usage", {}))
            p_usage = json.dumps(proxied.get("usage", {}))
            saved = d_in - p_in
            print(f"{p['name']:<14} {d_in:>6}/{d_out:<7} {p_in:>6}/{p_out:<7} "
                  f"{saved:>6} {t_proxy - t_direct:>+8.2f}s")
            print(f"  direct : {direct_text[:160]!r}")
            print(f"  proxied: {proxied_text[:160]!r}")

            report.append(
                f"## {p['name']}\n\n"
                f"- direct:  in={d_in} out={d_out} latency={t_direct:.2f}s\n"
                f"- proxied: in={p_in} out={p_out} latency={t_proxy:.2f}s "
                f"(input saved: {saved})\n"
                f"- direct usage: `{d_usage}`\n"
                f"- proxied usage: `{p_usage}`\n\n"
                f"**Direct answer:**\n\n{direct_text}\n\n"
                f"**Proxied answer:**\n\n{proxied_text}\n\n---\n\n"
            )

    Path(args.out).write_text("".join(report), encoding="utf-8")
    print(f"\nFull untruncated answers written to {args.out}")
    print("Review the answers side by side — compression should not change "
          "meaning on conversational prompts, and code prompts should be "
          "passed through (little/no savings).")


if __name__ == "__main__":
    main()
