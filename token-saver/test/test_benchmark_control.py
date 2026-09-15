"""P1-1 blocker fix tests: per-request conciseness control (A/B arms).

The benchmark's baseline/treatment arms depend on the
X-Token-Saver-Conciseness header actually toggling conciseness injection
per-request. Without that, both arms are identical and the benchmark is void.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_matrix_live import routed  # noqa: E402,F401
from test_live_routing import _CaptureTransport  # noqa: E402


LONG_USER = ("Here is my situation in detail. I purchased a jacket from your "
             "store two weeks ago during a summer sale event, and I would like "
             "to return it because the size does not fit me properly. The "
             "jacket has only been tried on once at home over a t-shirt, all "
             "the original tags are still attached, and I kept the paper "
             "receipt from the purchase along with the original packaging it "
             "came in. Could you walk me through whether this return is "
             "possible and what the steps would be?")


def _arms(routed):
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    captured = []
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None       # legacy client rebuilt via factory
        main_mod.app.state.http_clients = {}
        for hdr_val in ("1", "0"):
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer sk-x",
                                "X-Token-Saver-Conciseness": hdr_val},
                       json={"model": "z-ai/glm-5.3-flash",
                             "messages": [{"role": "user", "content": LONG_USER}]})
            assert r.status_code == 200, r.text[:150]
            captured.append(json.loads(cap.requests[-1].content))
    return captured, cap


def test_header_toggles_conciseness_between_arms(routed):
    """The two benchmark arms MUST differ upstream, or the benchmark is void."""
    (body_on, body_off), cap = _arms(routed)
    full_on = json.dumps(body_on["messages"])
    full_off = json.dumps(body_off["messages"])
    assert full_on != full_off, "arms identical — benchmark would measure nothing"
    assert "concisely" in full_on.lower()
    assert "concisely" not in full_off.lower()


def test_header_never_leaks_upstream(routed):
    (_on, _off), cap = _arms(routed)
    for req in cap.requests:
        assert "x-token-saver-conciseness" not in {k.lower() for k in req.headers}


def test_no_header_keeps_config_default(routed):
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None       # legacy client rebuilt via factory
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer sk-x"},
                   json={"model": "z-ai/glm-5.3-flash",
                         "messages": [{"role": "user", "content": LONG_USER}]})
        assert r.status_code == 200
    body = json.loads(cap.requests[0].content)
    # default config after P1-1: output_conciseness_enabled=false -> absent
    assert "concisely" not in json.dumps(body["messages"]).lower()
