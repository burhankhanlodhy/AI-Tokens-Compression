"""V1.2.1 integration E2E corpus.

Single mixed corpus covering all four v1.2.1 optimization phases plus a
no-op segment that must remain untouched. Segment weighting mirrors the
release-gate targets (tool-heavy, codebase-heavy, schema-heavy,
result-heavy, mixed, and a control segment).

Scenario shape (each entry):
  id, segment, upstream_savings_expected: bool,
  messages: OpenAI chat messages array,
  tools: optional tools array (schema-heavy weight)

Checksum pinned in e2e_corpus.json.sha256; the A/B runner refuses to run
against a mismatched corpus (pinned-fixture integrity, mirroring
run_l1_benchmark.py / run_schema_benchmark.py).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "e2e_corpus.json"
CHECKSUM_FILE = CORPUS.with_suffix(".json.sha256")


def _tool(name, desc, params):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": params,
                            "required": [p for p in params][:1]},
        },
    }


def _verbose_schema_tools(n: int, prefix: str = "api") -> list:
    """Schema-heavy segment: redundant descriptions the minifier removes."""
    tools = []
    for i in range(n):
        params = {
            "id": {"type": "string", "description": "The unique identifier string for this particular resource entity in the system."},
            "email": {"type": "string", "format": "email", "description": "Email address"},
            "limit": {"type": "integer", "description": "The maximum number of items to return in a single page of results."},
            "cursor": {"type": "string", "description": "The pagination cursor string returned by the previous page of results."},
            "verbose": {"type": "boolean", "description": "Whether or not to include verbose extended detailed output."},
        }
        tools.append(_tool(
            f"{prefix}_action_{i}",
            "Perform an API action on a resource. This tool performs the requested action "
            "on the specified resource using the provided parameters and returns the result.",
            params,
        ))
    return tools


def _large_code_file(lines: int, seed: str) -> str:
    body = []
    for i in range(lines):
        body.append(f"def handler_{i}(payload, config):")
        body.append(f'    """Process step {i} for {seed}."""')
        body.append("    result = transform(payload, config)")
        body.append("    if result is None:")
        body.append("        raise ValueError('transform failed')")
        body.append("    return result")
    text = "import os\nimport json\nimport logging\n\n" + "\n".join(body)
    text += "\n\ndef main():\n    print('entrypoint for " + seed + "')\n"
    return text


def _tool_result_bloat() -> str:
    """Result-heavy segment: oversized tool output with noise + signal."""
    rows = "\n".join(
        f'{{"id": {i}, "name": "record_{i}", "status": "active", '
        f'"description": "A very long description of record {i} that repeats verbose '
        f'marketing copy nobody needs in context, padding the payload massively.", '
        f'"metadata": {{"created": "2026-01-01T00:00:{i:02d}Z", "score": {i % 100}}}}}'
        for i in range(60)
    )
    return (
        "Query results (60 of 60 records shown):\n" + rows +
        "\n\nNOTE: 59 of these records are stale duplicates; the relevant record "
        "is id=7 (record_7, status=active, updated today)."
    )


def _shell_noise() -> str:
    lines = ["$ npm run build"]
    for i in range(40):
        lines.append(f"webpack compiled module {i} chunk assets emitted")
    lines.append("npm ERR! code ELIFECYCLE")
    lines.append("npm ERR! errno 1")
    lines.append("$ pytest -q")
    for i in range(40):
        lines.append(f"test_case_{i} PASSED")
    lines.append("FAILED tests/test_billing.py::test_invoice_total - AssertionError: assert 100 == 90")
    lines.append("1 failed, 40 passed in 2.31s")
    return "\n".join(lines)


def _import_block() -> str:
    return ("import os\nimport json\nimport logging\nimport asyncio\n"
            "from decimal import Decimal\nfrom typing import Any\n")


def build_corpus() -> list:
    scenarios = []

    # --- codebase-heavy: 6 scenarios (truncation + import dedup + shell filter)
    for i in range(3):
        scenarios.append({
            "id": f"e2e-codebase-large-{i:03d}",
            "segment": "codebase",
            "upstream_savings_expected": True,
            "messages": [
                {"role": "user", "content":
                    f"File: src/module_{i}.py\n```python\n"
                    f"{_large_code_file(600 + i * 200, f'module_{i}')}\n```\n"
                    "Explain what this module does and list its entrypoints."},
            ],
        })
    shared_imports = _import_block()
    for i in range(2):
        files = "\n\n".join(
            f"File: pkg{i}/file{j}.py\n```python\n{shared_imports}\n\ndef file_{j}_main():\n"
            f"    print('unique body for file {j} seed {i}')\n    return {i * 10 + j}\n```"
            for j in range(12)
        )
        scenarios.append({
            "id": f"e2e-codebase-dedup-{i:03d}",
            "segment": "codebase",
            "upstream_savings_expected": True,
            "messages": [
                {"role": "user", "content":
                    files + "\n\nSummarize the shared structure across these files."},
            ],
        })
    scenarios.append({
        "id": "e2e-codebase-shell-001",
        "segment": "codebase",
        "upstream_savings_expected": True,
        "messages": [
            {"role": "user", "content":
                "My build broke. Here is the terminal output:\n```\n" + _shell_noise() +
                "\n```\nWhy did the build fail?"},
        ],
    })

    # --- schema-heavy: 4 scenarios (20-40 tools, verbose descriptions)
    for n, tag in ((40, "a"), (30, "b"), (24, "c"), (20, "d")):
        scenarios.append({
            "id": f"e2e-schema-{tag}",
            "segment": "schema",
            "upstream_savings_expected": True,
            "messages": [
                {"role": "user", "content":
                    "Use the appropriate API tool to list the first 5 records for "
                    "resource id 'res-42'."},
            ],
            "tools": _verbose_schema_tools(n),
        })

    # --- tool-result-heavy: 5 scenarios
    for i in range(5):
        scenarios.append({
            "id": f"e2e-result-{i:03d}",
            "segment": "result",
            "upstream_savings_expected": True,
            "messages": [
                {"role": "user", "content": "List the active records."},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": f"call_{i}", "type": "function",
                                 "function": {"name": "list_records",
                                              "arguments": "{\"status\":\"active\"}"}}]},
                {"role": "tool", "tool_call_id": f"call_{i}",
                 "content": _tool_result_bloat()},
                {"role": "user", "content": "Which single record actually matters here?"},
            ],
            "tools": [_tool("list_records", "List records with optional status filter.",
                            {"status": {"type": "string", "description": "Filter by record status."}})],
        })

    # --- tool-heavy conversation: 3 scenarios
    for i in range(3):
        scenarios.append({
            "id": f"e2e-tool-{i:03d}",
            "segment": "tool",
            "upstream_savings_expected": True,
            "messages": [
                {"role": "system", "content": "You are a coding agent operating in a repository."},
                {"role": "user", "content": "Run the test suite and summarize failures."},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": f"tc_{i}", "type": "function",
                                 "function": {"name": "run_tests",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"tc_{i}",
                 "content": _shell_noise()},
                {"role": "user", "content": "Now propose the minimal fix."},
            ],
            "tools": _verbose_schema_tools(6, "agent") + [
                _tool("run_tests", "Run the project test suite and return output.", {}),
            ],
        })

    # --- control segment: small talk, must stay ~untouched
    for i in range(2):
        scenarios.append({
            "id": f"e2e-control-{i:03d}",
            "segment": "control",
            "upstream_savings_expected": False,
            "messages": [
                {"role": "user", "content":
                    f"Say hello and tell me a one-sentence fun fact about the number {i + 7}."},
            ],
        })

    return scenarios


def main() -> None:
    import hashlib
    scenarios = build_corpus()
    CORPUS.write_text(json.dumps(scenarios, indent=1) + "\n")
    digest = hashlib.sha256(CORPUS.read_bytes()).hexdigest()
    CHECKSUM_FILE.write_text(digest + "\n")
    segments = {}
    for s in scenarios:
        segments[s["segment"]] = segments.get(s["segment"], 0) + 1
    print(f"wrote {len(scenarios)} scenarios; segments={segments}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
