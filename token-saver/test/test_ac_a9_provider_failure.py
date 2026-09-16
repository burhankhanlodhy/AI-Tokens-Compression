"""AC-A9 provider-failure tests through the real ASGI route.

The upstream is a deterministic transport fixture.  The assertions cover the
client-facing normalized error contract and the observable request metric;
there are no real provider calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import main as main_mod  # noqa: E402
from proxy.config import get_settings  # noqa: E402


class _FailureTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, status: int | None = None, body: bytes = b"", headers=None,
                 exc: Exception | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.exc = exc
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, content=self.body, headers=self.headers)


def _post_body():
    return {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "failure fixture"}],
    }


def _run_failure(monkeypatch, tmp_path, transport: _FailureTransport):
    monkeypatch.setenv("PROVIDER_ROUTING", "true")
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ac_a9.db"))
    get_settings.cache_clear()
    monkeypatch.setattr(
        main_mod,
        "_client_factory",
        lambda base_url, timeout: httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport
        ),
        raising=False,
    )
    with TestClient(main_mod.app) as client:
        main_mod.app.state.http_clients = {}
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer recorded-test-credential"},
            json=_post_body(),
        )
        metrics = client.get("/metrics?format=json")
    get_settings.cache_clear()
    return response, metrics, transport


def test_ac_a9_upstream_timeout_is_normalized_and_counted(monkeypatch, tmp_path):
    transport = _FailureTransport(exc=httpx.ReadTimeout("fixture timeout"))
    response, metrics, transport = _run_failure(monkeypatch, tmp_path, transport)

    assert len(transport.requests) == 1
    assert response.status_code == 504
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "message": "upstream timeout: fixture timeout",
            "type": "upstream_timeout",
            "code": 504,
        }
    }
    assert metrics.status_code == 200
    assert metrics.json()["requests"] == 1


def test_ac_a9_upstream_5xx_is_normalized_and_counted(monkeypatch, tmp_path):
    transport = _FailureTransport(
        status=503,
        body=json.dumps({"error": {"message": "fixture overloaded"}}).encode(),
        headers={"content-type": "application/json", "retry-after": "5"},
    )
    response, metrics, transport = _run_failure(monkeypatch, tmp_path, transport)

    assert len(transport.requests) == 1
    assert response.status_code == 503
    assert response.headers.get("retry-after") == "5"
    assert response.json() == {
        "error": {
            "message": "fixture overloaded",
            "type": "overloaded",
            "code": 503,
        }
    }
    assert metrics.status_code == 200
    assert metrics.json()["requests"] == 1
