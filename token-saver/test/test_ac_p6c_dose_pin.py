"""AC-P6c tier-pin QA contract (PM ratification, spec 25173d1).

Two assertions QA owns on AC-P6c, tested here ahead of the paid
calibration run:
1. with `allow_dose_pin` OFF, a request carrying `x-token-saver-dose-pin`
   still resolves to the discriminator's selection — the pre-calibration
   cap is not bypassable by any client;
2. with the flag ON (benchmark deployment only), the pin is honored as
   the calibration instrument, the header never leaks upstream, and an
   invalid pin value falls back to the discriminator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import get_settings  # noqa: E402
from test_matrix_live import routed  # noqa: E402,F401
from test_live_routing import _CaptureTransport  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "benchmark" / "prompts.json"
PROMPTS = {p["id"]: p for p in json.loads(FIXTURES.read_text())["prompts"]}

RAG_051 = PROMPTS["rag-051"]["messages"]

UNGROUNDED_LONG = [{
    "role": "user",
    "content": (
        "Here is my situation in detail. I purchased a jacket from your "
        "store two weeks ago during a summer sale event, and I would like "
        "to return it because the size does not fit me properly. The "
        "jacket has only been tried on once at home over a t-shirt, all "
        "the original tags are still attached, and I kept the paper "
        "receipt from the purchase along with the original packaging it "
        "came in. Could you walk me through whether this return is "
        "possible and what the steps would be?"
    ),
}]


@pytest.fixture
def dose_pin_env(monkeypatch):
    """Toggle allow_dose_pin via env + settings cache reset, restoring
    the cached settings afterwards so other tests see deployment config."""
    def _set(flag: bool) -> None:
        if flag:
            monkeypatch.setenv("ALLOW_DOSE_PIN", "true")
        else:
            monkeypatch.delenv("ALLOW_DOSE_PIN", raising=False)
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


def _upstream(routed, messages, pin=None):
    main_mod = routed
    cap = _CaptureTransport()
    main_mod._client_factory = lambda b, t: httpx.AsyncClient(
        base_url=b, timeout=t, transport=cap)
    from fastapi.testclient import TestClient
    headers = {"Authorization": "Bearer sk-x",
               "X-Token-Saver-Conciseness": "1"}
    if pin is not None:
        headers["X-Token-Saver-Dose-Pin"] = pin
    with TestClient(main_mod.app) as c:
        main_mod.app.state.http = None
        main_mod.app.state.http_clients = {}
        r = c.post("/v1/chat/completions", headers=headers,
                   json={"model": "openrouter/z-ai/glm-5.3-flash",
                         "messages": messages})
        assert r.status_code == 200, r.text[:200]
    sent = cap.requests[-1]
    return json.loads(sent.content)["messages"], sent.headers


# --- QA assertion 1: the cap is not bypassable with the flag off -------


def test_pin_ignored_when_flag_off_ground_fidelity_critical(routed, dose_pin_env):
    dose_pin_env(False)
    upstream, _hdrs = _upstream(routed, RAG_051, pin="bounded")
    # pre-calibration discriminator selection for rag-051 is tier "none":
    # the pin must NOT lift the injection into the body.
    assert "concisely" not in json.dumps(upstream).lower()
    assert "source-attributed" not in json.dumps(upstream).lower()


def test_pin_ignored_when_flag_off_ungrounded_full_still_applies(routed, dose_pin_env):
    """With the flag off the pin can neither raise NOR lower the tier —
    the discriminator's selection is the only authority."""
    dose_pin_env(False)
    upstream, _hdrs = _upstream(routed, UNGROUNDED_LONG, pin="none")
    assert "concisely" in json.dumps(upstream).lower()


# --- QA assertion 2 (instrument side): pin honored behind the flag ------


def test_pin_honored_bounded_on_grounded_when_flag_on(routed, dose_pin_env):
    dose_pin_env(True)
    upstream, sent_headers = _upstream(routed, RAG_051, pin="bounded")
    body = json.dumps(upstream).lower()
    # the BOUNDED fidelity-guarded instruction, not the full one
    assert "source-attributed" in body
    assert "shorten the delivery, never the content" in body
    assert "postamble" not in body  # full-tier marker absent
    # control header never leaks upstream (C2)
    assert "x-token-saver-dose-pin" not in {k.lower() for k in sent_headers}


def test_pin_honored_none_on_ungrounded_when_flag_on(routed, dose_pin_env):
    """The pin can also force DOWN (tier none) for control arms."""
    dose_pin_env(True)
    upstream, _hdrs = _upstream(routed, UNGROUNDED_LONG, pin="none")
    assert "concisely" not in json.dumps(upstream).lower()


def test_pin_honored_full_on_grounded_when_flag_on(routed, dose_pin_env):
    """Instrument capability: behind the flag the harness may also pin
    `full` on grounded fixtures — needed to measure what the cap forbids."""
    dose_pin_env(True)
    upstream, _hdrs = _upstream(routed, RAG_051, pin="full")
    assert "postamble" in json.dumps(upstream).lower()


def test_invalid_pin_value_falls_back_to_discriminator(routed, dose_pin_env):
    dose_pin_env(True)
    upstream, _hdrs = _upstream(routed, RAG_051, pin="aggressive")
    # discriminator selection for rag-051 pre-calibration is none
    assert "concisely" not in json.dumps(upstream).lower()
    assert "source-attributed" not in json.dumps(upstream).lower()
