"""Demo client: send real prompts through the token-saver proxy.

Usage:
    1. Start the proxy:   .venv\\Scripts\\uvicorn proxy.main:app --port 8000
    2. Set your key:      $env:OPENROUTER_API_KEY="sk-or-v1-..."
    3. Run:               .venv\\Scripts\\python test\\demo_client.py
                          .venv\\Scripts\\python test\\demo_client.py --model google/gemini-2.5-flash
                          .venv\\Scripts\\python demo_client.py --ask "your own question"

This mimics a real app: it just points its base_url at the proxy and uses the
same OpenRouter key it would use directly. The proxy compresses long prompts,
skips code prompts, and logs savings to SQLite (view with /stats).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROXY_URL = "http://localhost:8000/v1"

SAMPLES = [
    ("long-context QA", (
        "Read this text and answer in one sentence: what was Watt's key "
        "improvement?\n\n"
        "The history of the steam engine is a long and fascinating one. It began "
        "with early experiments by Hero of Alexandria, whose aeolipile demonstrated "
        "the principle of reactive steam power, though it was little more than a "
        "curiosity. Centuries later, Thomas Newcomen built the first practical "
        "atmospheric engine in 1712 to pump water out of mines. James Watt later "
        "improved the design dramatically with a separate condenser, which greatly "
        "improved efficiency and made steam power economically viable across "
        "industries. The subsequent development of high-pressure engines by Richard "
        "Trevithick and others enabled locomotives and steamships, transforming "
        "transportation and industry worldwide. The steam engine ultimately gave way "
        "to internal combustion and electric motors in the twentieth century, but its "
        "legacy as the workhorse of the Industrial Revolution remains unmatched. ") * 6),
    ("chat", "In two sentences: why is the sky blue?"),
    ("code (should pass through)", (
        "Briefly review this function for bugs:\n\n```python\n"
        "def binary_search(arr, target):\n"
        "    lo, hi = 0, len(arr)\n"
        "    while lo < hi:\n"
        "        mid = (lo + hi) // 2\n"
        "        if arr[mid] < target:\n"
        "            lo = mid + 1\n"
        "        else:\n"
        "            hi = mid\n"
        "    return lo if lo < len(arr) and arr[lo] == target else -1\n```")),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo client for token-saver proxy")
    parser.add_argument("--model", default="z-ai/glm-5.3-flash")
    parser.add_argument("--ask", default=None, help="Ask your own question instead of samples")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        sys.exit("Set OPENROUTER_API_KEY first:  $env:OPENROUTER_API_KEY=\"sk-or-v1-...\"")

    # This is the "one line of code" change: base_url points at the proxy.
    client = OpenAI(base_url=PROXY_URL, api_key=api_key)

    prompts = [("your question", args.ask)] if args.ask else SAMPLES

    for name, content in prompts:
        print(f"\n=== {name} ===")
        try:
            resp = client.chat.completions.create(
                model=args.model,
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e}")
            print("  Is the proxy running?  .venv\\Scripts\\uvicorn proxy.main:app --port 8000")
            sys.exit(1)
        usage = resp.usage
        print(f"  model: {resp.model}")
        print(f"  tokens: prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
        print(f"  answer: {resp.choices[0].message.content.strip()[:300]}")

    print("\nSavings so far:")
    import httpx
    print(httpx.get("http://localhost:8000/stats?format=text").text)


if __name__ == "__main__":
    main()
