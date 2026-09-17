"""Config-loaded pricing (PM ruling): pricing.json is the price source.

ACs (PM, 2026-09-17):
  - changing a price = one file edit, zero code change
  - estimate_cost (hence /metrics and dashboard cost_saved) derives from
    the loaded rates
  - unknown model -> hardcoded fallback + a WARNING (never silent)
  - pinned P1-1 instrument google/gemini-3.5-flash-lite is priced at
    0.30/2.50 — NOT the 0.50/1.50 fallback it silently got before
"""
from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path

import pytest

TOKEN_SAVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOKEN_SAVER))

from proxy import config  # noqa: E402


def _reset_pricing_state() -> None:
    config._pricing_cache = None
    config._pricing_warned.clear()


@pytest.fixture(autouse=True)
def _reset():
    _reset_pricing_state()
    yield
    _reset_pricing_state()


def test_repo_pricing_json_loads_with_pinned_gemini_rate():
    """The repo's shipped pricing.json is valid and prices the P1-1 pin."""
    table = config.load_pricing()
    assert table["google/gemini-3.5-flash-lite"] == (0.30, 2.50)
    # _meta is metadata, never a model row
    assert "_meta" not in table


def test_estimate_cost_uses_file_rate_for_pinned_model():
    cost = config.estimate_cost("google/gemini-3.5-flash-lite", 1_000_000, 0)
    assert abs(cost - 0.30) < 1e-9
    cost = config.estimate_cost("google/gemini-3.5-flash-lite", 0, 1_000_000)
    assert abs(cost - 2.50) < 1e-9


def test_price_edit_is_one_file_edit_zero_code_change(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"test/model-x": [1.0, 2.0]}))
    monkeypatch.setattr(
        config, "get_settings",
        lambda: types.SimpleNamespace(
            pricing_file=str(pricing),
            model_prices_per_m={},
            default_input_price_per_m=0.50,
            default_output_price_per_m=1.50,
        ),
    )
    _reset_pricing_state()
    assert abs(config.estimate_cost("test/model-x", 1_000_000, 0) - 1.0) < 1e-9

    # THE AC: the price change happens ONLY in the file...
    pricing.write_text(json.dumps({"test/model-x": [3.0, 4.0]}))
    # ...picked up on reload (startup restart or explicit force_reload)
    config.load_pricing(force_reload=True)
    assert abs(config.estimate_cost("test/model-x", 1_000_000, 0) - 3.0) < 1e-9


def test_unknown_model_falls_back_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="token-saver.pricing"):
        cost = config.estimate_cost("no/such-model", 1_000_000, 0)
    assert abs(cost - 0.50) < 1e-9  # default fallback rates
    assert any("no/such-model" in rec.getMessage() for rec in caplog.records)


def test_unknown_model_warning_fires_once_per_model(caplog):
    with caplog.at_level(logging.WARNING, logger="token-saver.pricing"):
        config.estimate_cost("no/such-model", 1_000_000, 0)
        config.estimate_cost("no/such-model", 2_000_000, 0)
        config.estimate_cost("no/such-model-2", 1_000_000, 0)
    model_warnings = [r for r in caplog.records
                      if "falling back" in r.getMessage()]
    assert len(model_warnings) == 2  # once per model, not per request


def test_missing_pricing_file_falls_back_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        config, "get_settings",
        lambda: types.SimpleNamespace(
            pricing_file=str(tmp_path / "absent.json"),
            model_prices_per_m={"fallback/model": (7.0, 9.0)},
            default_input_price_per_m=0.50,
            default_output_price_per_m=1.50,
        ),
    )
    _reset_pricing_state()
    with caplog.at_level(logging.WARNING, logger="token-saver.pricing"):
        table = config.load_pricing()
    assert table == {}
    # hardcoded fallback table still prices known models
    assert abs(config.estimate_cost("fallback/model", 1_000_000, 0) - 7.0) < 1e-9
    assert any("not found" in r.getMessage() for r in caplog.records)


def test_malformed_entries_skipped_with_warning(tmp_path, monkeypatch, caplog):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "_meta": {"source": "test"},
        "good/model": [1.0, 2.0],
        "bad/model": "three dollars",
        "worse/model": [1.0],
        "negative/model": [-1.0, 2.0],
    }))
    monkeypatch.setattr(
        config, "get_settings",
        lambda: types.SimpleNamespace(
            pricing_file=str(pricing),
            model_prices_per_m={},
            default_input_price_per_m=0.50,
            default_output_price_per_m=1.50,
        ),
    )
    _reset_pricing_state()
    with caplog.at_level(logging.WARNING, logger="token-saver.pricing"):
        table = config.load_pricing()
    assert table == {"good/model": (1.0, 2.0)}
    warnings = "\n".join(r.getMessage() for r in caplog.records)
    for bad in ("bad/model", "worse/model", "negative/model"):
        assert bad in warnings
