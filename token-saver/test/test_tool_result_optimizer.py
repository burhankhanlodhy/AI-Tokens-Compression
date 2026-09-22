"""Regression tests for output-side tool-result optimization."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.counting import count_text
from proxy.tool_result_optimizer import (
    clear_result_cache,
    compute_result_fingerprint,
    filter_result_by_type,
    optimize_tool_result,
    truncate_large_result,
)


MODEL = "gpt-4o"


def test_truncate_large_json_preserves_top_level_structure_and_marker():
    content = json.dumps(
        {"status": "ok", "items": [{"id": index, "text": "verbose " * 20} for index in range(100)]}
    )

    optimized = truncate_large_result(content, max_tokens=100)

    parsed = json.loads(optimized)
    assert parsed["status"] == "ok"
    assert "items" in parsed
    assert "truncated" in optimized
    assert "full result available on request" in optimized
    assert count_text(optimized, MODEL) <= 100


def test_plain_text_truncation_reports_actual_removed_tokens_within_budget():
    content = "table header\n" + ("verbose result " * 1000) + "\nfinal status"
    original_tokens = count_text(content, MODEL)

    optimized = truncate_large_result(content, max_tokens=80)

    marker = re.search(r"\[\.\.\. truncated (\d+) tokens, full result available on request\]", optimized)
    assert marker is not None
    assert int(marker.group(1)) == original_tokens - count_text(optimized, MODEL)
    assert count_text(optimized, MODEL) <= 80


def test_identical_result_and_args_are_processed_once(monkeypatch):
    import proxy.tool_result_optimizer as optimizer

    clear_result_cache()
    calls = 0
    original = optimizer.truncate_large_result

    def counted(content: str, max_tokens: int) -> str:
        nonlocal calls
        calls += 1
        return original(content, max_tokens)

    monkeypatch.setattr(optimizer, "truncate_large_result", counted)
    message = {"role": "tool", "tool_call_id": "call_1", "content": "output " * 500}
    args = {"path": "/tmp/example"}

    first = optimize_tool_result(message, tool_call_args=args, max_tokens=50, filtering=False)
    second = optimize_tool_result(message, tool_call_args=args, max_tokens=50, filtering=False)

    assert first == second
    assert calls == 1
    assert message["content"] != first["content"]


def test_file_listing_filter_removes_noise_and_keeps_relevant_entries():
    listing = "\n".join(
        [
            "src/main.py",
            ".git/objects/pack/huge.pack",
            "node_modules/react/index.js",
            "tests/test_main.py",
            ".venv/lib/python3.13/site-packages/pkg.py",
        ]
    )

    filtered = filter_result_by_type(listing, "file_listing")

    assert "src/main.py" in filtered
    assert "tests/test_main.py" in filtered
    assert ".git/" not in filtered
    assert "node_modules/" not in filtered
    assert ".venv/" not in filtered
    assert "irrelevant file entries filtered" in filtered


def test_file_listing_filter_handles_ls_l_style_lines():
    listing = "\n".join(
        [
            "total 120",
            "-rw-r--r-- 1 user group 2459 Sep 21 2026 node_modules/pkg_9/lib/index.js",
            "drwxr-xr-x 4 user group 4096 Sep 21 2026 .git/objects",
            "-rw-r--r-- 1 user group  117 Sep 20 2026 src/main.py",
            "lrwxrwxrwx 1 user group   44 Sep 21 2026 latest -> .venv/lib/python3.13/site-packages/pkg",
            "-rw-r--r-- 1 user group  512 Sep 20 2026 tests/test_main.py",
        ]
    )

    filtered = filter_result_by_type(listing, "file_listing")

    assert "src/main.py" in filtered
    assert "tests/test_main.py" in filtered
    assert "node_modules" not in filtered
    assert ".git" not in filtered
    assert ".venv" not in filtered
    marker = re.search(r"\[\.\.\. (\d+) irrelevant file entries filtered \.\.\.\]", filtered)
    assert marker is not None
    assert int(marker.group(1)) == 3


def test_file_listing_filter_handles_find_ls_and_tree_style_lines():
    listing = "\n".join(
        [
            "12345   12 drwxr-xr-x   3 user     group         4096 Sep 21 10:00 ./node_modules",
            "12346    4 -rw-r--r--   1 user     group          512 Sep 21 10:01 ./.git/config",
            "12347    8 -rw-r--r--   1 user     group         1024 Sep 21 10:02 ./src/app.py",
            "src",
            "├── __pycache__",
            "│   └── app.cpython-313.pyc",
            "└── app.py",
            "dist/bundle.js",
            "build/output.txt",
            ".pytest_cache/v/cache/lastfailed",
            "README.md",
        ]
    )

    filtered = filter_result_by_type(listing, "file_listing")

    assert "./src/app.py" in filtered
    assert "app.py" in filtered
    assert "README.md" in filtered
    for noise in ("node_modules", ".git", "__pycache__", "dist/", "build/", ".pytest_cache"):
        assert noise not in filtered
    marker = re.search(r"\[\.\.\. (\d+) irrelevant file entries filtered \.\.\.\]", filtered)
    assert marker is not None
    assert int(marker.group(1)) == 6


def test_file_listing_filter_does_not_filter_filenames_containing_noise_words():
    listing = "\n".join(
        [
            "src/node_modules_compat.py",
            "docs/.github_workflow.md",
            "tools/venv_manager.sh",
            "src/build_system.md",
            "assets/dist_logo.png",
            "notes/build_notes.txt",
        ]
    )

    filtered = filter_result_by_type(listing, "file_listing")

    assert filtered == listing


def test_log_filter_keeps_errors_and_warnings_while_dropping_debug_noise():
    log = "\n".join(
        ["DEBUG connecting", "INFO request started", "WARNING retrying", "ERROR upstream failed", "INFO done"]
    )

    filtered = filter_result_by_type(log, "log")

    assert "WARNING retrying" in filtered
    assert "ERROR upstream failed" in filtered
    assert "DEBUG connecting" not in filtered
    assert "INFO request started" not in filtered
    assert "informational/debug log lines omitted" in filtered


def test_api_filter_preserves_status_and_error_when_payload_is_large():
    content = json.dumps(
        {"status": 429, "error": {"message": "rate limited"}, "data": list(range(2000))}
    )

    optimized = optimize_tool_result(
        {"role": "tool", "name": "http_request", "content": content},
        max_tokens=80,
        filtering=True,
        cache_enabled=False,
    )

    parsed = json.loads(optimized["content"])
    assert parsed["status"] == 429
    assert parsed["error"]["message"] == "rate limited"
    assert "full result available on request" in optimized["content"]


def test_fingerprint_is_stable_for_equivalent_argument_order():
    assert compute_result_fingerprint({"a": 1, "b": [2, 3]}) == compute_result_fingerprint(
        {"b": [2, 3], "a": 1}
    )


@pytest.mark.asyncio
async def test_proxy_optimizes_tool_result_without_rewriting_tool_call_schema(monkeypatch, tmp_path):
    import httpx

    from proxy import main
    from proxy.config import get_settings

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "60")
    monkeypatch.setenv("TOOL_RESULT_OPTIMIZATION", "true")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 1}},
        )

    main.app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test/v1"
    )
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": '{"path":"/tmp/a"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "src/main.py\n.git/objects/pack/noisy.pack\n" + ("line\n" * 1000),
            },
        ],
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            response = await client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200
        forwarded = captured["body"]
        assert isinstance(forwarded, dict)
        assert forwarded["messages"][0]["tool_calls"] == payload["messages"][0]["tool_calls"]
        optimized_content = forwarded["messages"][1]["content"]
        assert "full result available on request" in optimized_content
        assert "src/main.py" in optimized_content
        assert ".git/objects" not in optimized_content
        assert count_text(optimized_content, MODEL) <= 60
    finally:
        await main.app.state.http.aclose()
        get_settings.cache_clear()
