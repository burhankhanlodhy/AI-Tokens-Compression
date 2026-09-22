"""Regression coverage for codebase-aware prompt optimization."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.codebase_optimizer import (  # noqa: E402
    deduplicate_imports,
    filter_shell_output,
    optimize_codebase_content,
    truncate_large_files,
)
from proxy.config import get_settings  # noqa: E402


def _fenced(lines: list[str], language: str = "python") -> str:
    return f"```{language}\n" + "\n".join(lines) + "\n```"


def test_truncate_large_fenced_file_preserves_head_tail_and_reports_middle(monkeypatch):
    monkeypatch.setenv("CODEBASE_MAX_FILE_LINES", "200")
    get_settings.cache_clear()
    lines = [f"line_{number}" for number in range(1, 251)]

    optimized = truncate_large_files(_fenced(lines))

    assert "line_1" in optimized
    assert "line_100" in optimized
    assert "[... middle 50 lines truncated ...]" in optimized
    assert "line_101" not in optimized
    assert "line_150" not in optimized
    assert "line_151" in optimized
    assert "line_250" in optimized


def test_truncate_large_file_reference_without_a_fence_language_hint(monkeypatch):
    monkeypatch.setenv("CODEBASE_MAX_FILE_LINES", "4")
    get_settings.cache_clear()
    content = "File: example.py\n```\n" + "\n".join(
        f"line_{number}" for number in range(1, 7)
    ) + "\n```"

    optimized = truncate_large_files(content)

    assert "line_1" in optimized
    assert "[... middle 2 lines truncated ...]" in optimized
    assert "line_6" in optimized


def test_deduplicate_import_blocks_after_first_of_four_fenced_files():
    block = ["import os", "from pathlib import Path", "", "print(Path.cwd())"]
    messages = [
        {"role": "user", "content": _fenced(block)}
        for _ in range(4)
    ]

    optimized = deduplicate_imports(messages)

    assert "import os" in optimized[0]["content"]
    assert "from pathlib import Path" in optimized[0]["content"]
    for message in optimized[1:]:
        assert "[... import block repeated 4 times ...]" in message["content"]
        assert "import os" not in message["content"]
        assert "from pathlib import Path" not in message["content"]
        assert "print(Path.cwd())" in message["content"]


def test_deduplicate_import_blocks_in_openai_text_parts_after_first_of_four():
    block = ["import os", "from pathlib import Path", "", "print(Path.cwd())"]
    messages = [
        {"role": "user", "content": [{"type": "text", "text": _fenced(block)}]}
        for _ in range(4)
    ]

    optimized = deduplicate_imports(messages)

    assert "import os" in optimized[0]["content"][0]["text"]
    for message in optimized[1:]:
        text = message["content"][0]["text"]
        assert "[... import block repeated 4 times ...]" in text
        assert "import os" not in text
        assert "from pathlib import Path" not in text
        assert "print(Path.cwd())" in text


def test_filter_shell_output_removes_noise_and_keeps_critical_lines():
    noisy_output = "\n".join(
        [
            "2026-09-22T08:01:02.123Z INFO app: started worker",
            "DEBUG connection pool acquired",
            "npm verbose cli /usr/bin/node /usr/bin/npm",
            "Collecting package metadata",
            "test/test_proxy.py::test_route PASSED",
            "ERROR failed to connect to database",
            "WARNING retrying request",
            "Final result: 42 records processed",
            "Process exited with code 1",
        ]
    )

    optimized = filter_shell_output(noisy_output)

    assert "started worker" not in optimized
    assert "DEBUG connection" not in optimized
    assert "npm verbose" not in optimized
    assert " PASSED" not in optimized
    assert "ERROR failed to connect to database" in optimized
    assert "WARNING retrying request" in optimized
    assert "Final result: 42 records processed" in optimized
    assert "Process exited with code 1" in optimized


def test_optimizer_can_disable_dedupe_without_disabling_shell_filter(monkeypatch):
    monkeypatch.setenv("CODEBASE_DEDUPE_IMPORTS", "false")
    monkeypatch.setenv("SHELL_OUTPUT_FILTERING", "true")
    get_settings.cache_clear()
    content = _fenced(["import os", "print(os.getcwd())"]) + "\nDEBUG ignore me"

    optimized = optimize_codebase_content(
        [{"role": "user", "content": content} for _ in range(4)]
    )

    for message in optimized:
        assert "import os" in message["content"]
        assert "[... import block repeated" not in message["content"]
        assert "DEBUG ignore me" not in message["content"]


def test_optimizer_respects_each_individual_feature_flag(monkeypatch):
    monkeypatch.setenv("CODEBASE_MAX_FILE_LINES", "4")
    monkeypatch.setenv("CODEBASE_DEDUPE_IMPORTS", "false")
    monkeypatch.setenv("SHELL_OUTPUT_FILTERING", "false")
    get_settings.cache_clear()
    content = _fenced([f"line_{number}" for number in range(1, 7)])
    content += "\nDEBUG keep this diagnostic"

    optimized = optimize_codebase_content([{"role": "user", "content": content}])

    assert "[... middle" in optimized[0]["content"]
    assert "DEBUG keep this diagnostic" in optimized[0]["content"]


def test_optimizer_global_disable_returns_equivalent_message_content(monkeypatch):
    monkeypatch.setenv("CODEBASE_OPTIMIZATION_ENABLED", "false")
    get_settings.cache_clear()
    messages = [{"role": "user", "content": _fenced([f"line_{n}" for n in range(1, 7)])}]

    optimized = optimize_codebase_content(messages)

    assert optimized == messages
    assert optimized is not messages


@pytest.mark.asyncio
async def test_proxy_applies_codebase_optimization_before_upstream_forward(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv("CODEBASE_OPTIMIZATION_ENABLED", "true")
    monkeypatch.setenv("CODEBASE_MAX_FILE_LINES", "4")
    monkeypatch.setenv("L1_ENABLED", "false")
    monkeypatch.setenv("COMPRESSION_ENABLED", "false")
    get_settings.cache_clear()
    from proxy import stats
    from proxy.main import app

    stats.init_db()
    captured: dict[str, object] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), base_url="http://upstream.test/v1"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": _fenced([f"line_{n}" for n in range(1, 7)])}],
                },
            )
    finally:
        await app.state.http.aclose()
        get_settings.cache_clear()

    assert response.status_code == 200
    sent = captured["body"]["messages"][0]["content"]
    assert "[... middle 2 lines truncated ...]" in sent
