"""Generate checksum-pinned V1.2.1 G1 and G3 benchmark corpora.

Scenarios are chosen from representative MCP/CRM schemas and common shell/API
outputs; no observed savings percentage is used to size or select them.
Run: python3 benchmark/gen_v121_g1_g3_corpora.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _tool(name: str, params: dict, description: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": params,
                       "required": list(params)[:1]},
    }}


def build_g1_corpus() -> list[dict]:
    scenarios = []
    crm_fields = {
        "email": {"type": "string", "format": "email",
                  "description": "The user email address"},
        "account_email": {"type": "string", "format": "email",
                           "description": "Email address"},
        "contact_email": {"type": "string", "format": "email",
                           "description": "User email address"},
        "customer_id": {"type": "string", "description":
                         "The customer_id is the unique customer identifier used to look up a customer record."},
        "include_archived": {"type": "boolean", "description":
                              "Whether include_archived should include archived records in the returned results."},
        "page_size": {"type": "integer", "description":
                      "The page_size is the maximum number of customer records to return in each page."},
        "page_cursor": {"type": "string", "description":
                        "The page_cursor is the pagination cursor from the prior response, if continuing a search."},
    }
    for n, tag in ((24, "crm"), (32, "mcp"), (40, "sales")):
        tools = []
        for i in range(n):
            # CRM/MCP-style parameter blocks are deliberately pretty-printed
            # by the corpus serializer. The email entries exercise only the
            # existing explicit redundant-description allow-list.
            params = json.loads(json.dumps(crm_fields, sort_keys=True))
            tools.append(_tool(
                f"{tag}_contacts_search_{i:02d}", params,
                "Search CRM contacts using the supplied customer and email "
                "criteria, pagination controls, and archive visibility options. "
                "Returns matching contact records with account relationships."))
        scenarios.append({
            "id": f"v121-g1-{tag}-{n}-tools", "segment": "tool",
            "upstream_savings_expected": True,
            "messages": [
                {"role": "system", "content":
                 "You are an account operations assistant. Use the CRM search "
                 "tool to find the requested contact without changing records."},
                {"role": "user", "content":
                 "Find the account contact for customer_id C-1042 and return "
                 "the email address. Include the first page only."},
            ],
            "tools": tools,
        })
    return scenarios


def _pad_lines(lines: list[str], target_chars: int, prefix: str) -> str:
    """Expand natural output rows to a fixed size class, without truncation."""
    result = "\n".join(lines)
    index = 0
    while len(result) < target_chars:
        row = f"{prefix} {index:05d} details=ordinary-output status=ok owner=service-{index % 17}"
        result += "\n" + row
        index += 1
    return result


def _ls_result(target: int = 24_000, entries: int = 220) -> str:
    lines = ["$ ls -la /srv/customer-exports", "total 892",
             "drwxr-x---  4 app    ops       4096 Sep 22 17:40 .",
             "drwxr-xr-x 18 root   root      4096 Sep 22 17:39 .."]
    for i in range(entries):
        if i % 11 == 0:
            filename = f".venv/lib/python/site-packages/module-{i:04d}.py"
        elif i % 13 == 0:
            filename = f"node_modules/package-{i:04d}/index.js"
        elif i % 17 == 0:
            filename = f"build/cache/artifact-{i:04d}.o"
        else:
            filename = f"customer-export-{i:04d}.csv"
        noise = " [temporary upload artifact; safe to ignore]" if i % 3 == 0 else ""
        lines.append(f"-rw-r----- 1 app ops {1024 + i * 13:8d} Sep 22 17:{i % 60:02d} "
                     f"{filename}{noise}")
    lines.append("-rw-r----- 1 app ops     834 Sep 22 17:40 customer-export-0042.csv  <-- requested file")
    return _pad_lines(lines, target, "-rw-r----- 1 app ops 2048 Sep 22 17:41 archived-export")


def _json_result(target: int = 42_000) -> str:
    records = []
    for i in range(60):
        records.append({
            "id": f"acct-{i:05d}", "name": f"Regional account {i}",
            "status": "active" if i % 9 else "archived",
            "email": f"contact{i}@example.test", "owner": f"team-{i % 23}",
            "metadata": {"source": "crm-sync", "description":
                         "Imported account profile with audit fields and integration metadata." * 2,
                         "labels": ["customer", "synced", f"region-{i % 12}"]},
            "links": {"self": f"/v3/accounts/acct-{i:05d}", "contacts":
                      f"/v3/accounts/acct-{i:05d}/contacts"},
        })
    base = json.dumps({"request_id": "req-82941", "page": 1,
                       "total": len(records), "data": records}, indent=2)
    return _pad_lines([base], target, '  "diagnostic_note": "API response metadata retained for pagination"')


def _log_result(target: int = 70_000) -> str:
    lines = ["2026-09-22T17:42:01.211Z INFO worker=sync-4 starting nightly CRM sync"]
    frames = [
        "Traceback (most recent call last):",
        '  File "sync/worker.py", line 188, in process_batch',
        '  File "sync/client.py", line 94, in fetch_page',
        "    response = await self.transport.send(request)",
        "TimeoutError: upstream CRM request exceeded 30 seconds",
        "During handling of the above exception, another exception occurred:",
    ]
    for i in range(90):
        lines.extend([f"2026-09-22T17:{42 + i // 60:02d}:{i % 60:02d}.000Z WARN retry batch={i:03d} backoff=2s",
                      *frames,
                      f"2026-09-22T17:{43 + i // 60:02d}:{i % 60:02d}.100Z INFO retry complete batch={i:03d}"])
    lines.append("ERROR batch=042 account=acct-01042 exhausted retries; preserve this failure detail")
    return _pad_lines(lines, target, "2026-09-22T17:44:00.000Z INFO worker=sync-4 heartbeat")


def _scenario(scenario_id: str, result: str, *, size_class: str,
              tool_name: str, repeated_result_of: str | None = None) -> dict:
    # Stable exact payload (including call id) for repeated-result pair so the
    # harness can exercise an identical-request cache path across both arms.
    scenario = {
        "id": scenario_id, "segment": "result", "size_class": size_class,
        "upstream_savings_expected": True,
        "messages": [
            {"role": "user", "content": "Inspect the tool output and identify the requested account artifact."},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_result_fixture", "type": "function",
                             "function": {"name": tool_name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_result_fixture", "content": result},
            {"role": "user", "content": "Which artifact or failure is relevant?"},
        ],
    }
    if repeated_result_of:
        scenario["repeated_result_of"] = repeated_result_of
    return scenario


def build_g3_corpus() -> list[dict]:
    listing = _ls_result(24_000)
    api = _json_result(42_000)
    logs = _log_result(70_000)
    return [
        _scenario("v121-g3-ls-noisy-24000", listing,
                  size_class="over-cap-file-listing", tool_name="list_files"),
        _scenario("v121-g3-json-api-42000", api,
                  size_class="over-cap-json-api", tool_name="get_api_response"),
        _scenario("v121-g3-repeated-listing-24000", listing,
                  size_class="over-cap-identical-cache-repeat", tool_name="list_files",
                  repeated_result_of="v121-g3-ls-noisy-24000"),
        _scenario("v121-g3-repeated-logs-70000", logs,
                  size_class="over-cap-repeated-logs", tool_name="get_logs"),
        _scenario("v121-g3-boundary-ls-15000", _ls_result(15_000, entries=80),
                  size_class="at-cap-boundary", tool_name="list_files"),
    ]


def write_corpus(name: str, scenarios: list[dict]) -> str:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    payload = (json.dumps(scenarios, indent=1, ensure_ascii=False) + "\n").encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(digest + "\n")
    return digest


def main() -> None:
    for name, builder in (("v121_g1_tool_heavy.json", build_g1_corpus),
                          ("v121_g3_result_heavy.json", build_g3_corpus)):
        digest = write_corpus(name, builder())
        print(f"{name}: sha256={digest}")


if __name__ == "__main__":
    main()
