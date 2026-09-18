"""AC-P6f live tripwire loop (product-spec-v2.md AC-P6f).

The honesty guarantee is a loop, not a one-time certificate. Covered here:
1. the observation channel — ``grounded.envelope_shape_present`` is the
   AC-P6j scanner exposed as a pure request-content flag (positives for the
   ratified wrapper shapes, negatives pinning the false-positive controls);
2. the pure rules — dose drift (grounded ``bounded`` rows cutting outside
   the calibrated band) and missed grounding (envelope-shaped rows
   classified tier ``full`` or never classified, realizing a deep cut);
3. the ledger channel — dose_tier / grounded_risk / envelope_shape persist
   on both a fresh and a MIGRATED legacy SQLite ledger, and the live request
   path records them;
4. the endpoint — /api/tripwire reads the ledger and composes the report,
   pending (no calibration artifact), green, and red.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy.config import get_settings  # noqa: E402
from proxy.grounded import (  # noqa: E402
    envelope_shape_present,
    grounded_answer_risk,
)
from proxy import tripwire  # noqa: E402

UPSTREAM_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}

# --- AC-P6j ratified wrapper shapes (positives for the observation channel) --

ENVELOPE_POSITIVES = {
    "fenced_json": (
        '```json\n{"retrieved_documents": [{"chunk_id": "c1", '
        '"source": "policy.pdf", "document": "Returns accepted in 30 days."}]}\n```'
    ),
    "embedded_in_prose": (
        "Here is the context I found:\n"
        '{"documents": [{"chunk_id": "d2", "source": "a.md", '
        '"document": "Widget specs"}]}\n'
        "Now answer my trailing question, please?"
    ),
    "langchain": (
        '{"context": [{"page_content": "Refunds take 5 days", '
        '"metadata": {"source": "faq.md"}}]}'
    ),
    "xml_documents": (
        "<documents><document source='kb.txt'>Refunds take 5 days</document>"
        "</documents>"
    ),
    "id_document_hits": (
        '{"retrieved_documents": [{"id": "7", "document": "SLA is 99.9%"}]}'
    ),
    "nested_envelope": (
        '{"wrapper": {"search_results": [{"chunk_id": "x", '
        '"source": "s.txt", "document": "content"}]}}'
    ),
}

# False-positive controls (the same negatives AC-P6j pinned).
ENVELOPE_NEGATIVES = {
    "bare_string_list": '{"retrieved_documents": ["policy text", "more text"]}',
    "prose_mentioning_keys": (
        "Our pipeline stores retrieved_documents and page_content fields, "
        "but this message contains no data. What is your return policy?"
    ),
    "no_envelope": "Just a plain question about the weather?",
    "hits_lacking_source_keys": (
        '{"retrieved_documents": [{"text": "orphan text"}]}'
    ),
    "malformed_xml": "<documents><document>no source attr</document></documents>",
}


@pytest.mark.parametrize("name", sorted(ENVELOPE_POSITIVES))
def test_envelope_shape_present_positives(name):
    messages = [{"role": "user", "content": ENVELOPE_POSITIVES[name]}]
    assert envelope_shape_present(messages) is True


@pytest.mark.parametrize("name", sorted(ENVELOPE_NEGATIVES))
def test_envelope_shape_present_negatives(name):
    messages = [{"role": "user", "content": ENVELOPE_NEGATIVES[name]}]
    assert envelope_shape_present(messages) is False


def test_envelope_shape_present_scans_system_and_user():
    assert envelope_shape_present([
        {"role": "system", "content": ENVELOPE_POSITIVES["langchain"]},
        {"role": "user", "content": "summarize"},
    ]) is True


# --- Pure rules ---------------------------------------------------------------


def _row(**kw):
    base = {
        "id": 1, "ts": 0.0, "model": "m", "route": "compress",
        "dose_tier": None, "grounded_risk": None, "envelope_shape": 0,
        "input_tokens_before": 1000, "input_tokens_after": 1000,
        "output_tokens": 0,
    }
    base.update(kw)
    return base


class TestDoseDrift:
    def test_pending_when_no_band(self):
        rep = tripwire.evaluate_dose_drift([_row()], band=None)
        assert rep == {"status": "pending_calibration", "flagged": [],
                       "checked": 0}

    def test_alert_when_bounded_grounded_cut_exceeds_band(self):
        rows = [_row(dose_tier="bounded", grounded_risk="fidelity_critical",
                     input_tokens_before=1000, input_tokens_after=800)]
        rep = tripwire.evaluate_dose_drift(rows, band=(5.0, 12.0))
        assert rep["status"] == "alert"
        assert rep["flagged"][0]["reason"] == "bounded_cut_above_band"
        assert rep["flagged"][0]["realized_cut_pct"] == 20.0

    def test_clear_within_band(self):
        rows = [_row(dose_tier="bounded", grounded_risk="bounded",
                     input_tokens_before=1000, input_tokens_after=920)]
        assert tripwire.evaluate_dose_drift(rows, band=(5.0, 12.0))[
            "status"] == "clear"

    def test_ungrounded_bounded_rows_never_flag(self):
        rows = [_row(dose_tier="bounded", grounded_risk=None,
                     input_tokens_before=1000, input_tokens_after=500)]
        assert tripwire.evaluate_dose_drift(rows, band=(5.0, 12.0))[
            "status"] == "clear"

    def test_full_tier_rows_are_not_drift_scope(self):
        rows = [_row(dose_tier="full", grounded_risk="none",
                     input_tokens_before=1000, input_tokens_after=100)]
        assert tripwire.evaluate_dose_drift(rows, band=(5.0, 12.0))[
            "status"] == "clear"


class TestMissedGrounding:
    DEEP = 25.0

    def test_flags_unclassified_envelope_row_with_deep_cut(self):
        rows = [_row(dose_tier=None, envelope_shape=1,
                     input_tokens_before=1000, input_tokens_after=400)]
        rep = tripwire.evaluate_missed_grounding(rows, self.DEEP)
        assert rep["status"] == "alert"
        assert rep["flagged"][0]["reason"] == \
            "envelope_shape_unprotected_deep_cut"

    def test_flags_full_tier_envelope_row_with_deep_cut(self):
        rows = [_row(dose_tier="full", envelope_shape=1,
                     input_tokens_before=1000, input_tokens_after=600)]
        assert tripwire.evaluate_missed_grounding(rows, self.DEEP)[
            "status"] == "alert"

    def test_bounded_classified_envelope_row_does_not_flag(self):
        # The discriminator protected it — not the missed-grounding class.
        rows = [_row(dose_tier="bounded", grounded_risk="fidelity_critical",
                     envelope_shape=1, input_tokens_before=1000,
                     input_tokens_after=400)]
        assert tripwire.evaluate_missed_grounding(rows, self.DEEP)[
            "status"] == "clear"

    def test_shallow_cut_does_not_flag(self):
        rows = [_row(dose_tier=None, envelope_shape=1,
                     input_tokens_before=1000, input_tokens_after=900)]
        assert tripwire.evaluate_missed_grounding(rows, self.DEEP)[
            "status"] == "clear"

    def test_no_envelope_never_flags(self):
        rows = [_row(dose_tier="full", envelope_shape=0,
                     input_tokens_before=1000, input_tokens_after=100)]
        assert tripwire.evaluate_missed_grounding(rows, self.DEEP)[
            "status"] == "clear"


class TestReport:
    def test_report_pending_without_calibration_artifact(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(tripwire, "RESULTS_DIR", tmp_path)
        rep = tripwire.tripwire_report([])
        assert rep["status"] == "pending"
        assert rep["dose_drift"]["status"] == "pending_calibration"
        assert rep["calibration_band_pct"] is None

    def test_report_red_on_missed_grounding(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tripwire, "RESULTS_DIR", tmp_path)
        rows = [_row(dose_tier=None, envelope_shape=1,
                     input_tokens_before=1000, input_tokens_after=400)]
        assert tripwire.tripwire_report(rows)["status"] == "red"

    def test_band_loader_accepts_artifact_shapes(self, tmp_path):
        for payload in (
            {"cut_pct_band": [4.0, 11.0]},
            {"band": {"cut_pct": [4.0, 11.0]}},
            {"cut_pct": [4.0, 11.0]},
        ):
            (tmp_path / "calibration-rag-v2.json").write_text(json.dumps(payload))
            assert tripwire.load_calibration_band(tmp_path) == (4.0, 11.0)

    def test_band_loader_none_on_absent_or_malformed(self, tmp_path):
        assert tripwire.load_calibration_band(tmp_path) is None
        (tmp_path / "calibration-x.json").write_text("{not json")
        assert tripwire.load_calibration_band(tmp_path) is None
        (tmp_path / "calibration-y.json").write_text('{"cut_pct_band": [9, 2]}')
        assert tripwire.load_calibration_band(tmp_path) is None


# --- Ledger channel -----------------------------------------------------------


_LEGACY_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    route TEXT NOT NULL,
    input_tokens_before INTEGER NOT NULL,
    input_tokens_after INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_before REAL NOT NULL DEFAULT 0,
    est_cost_after REAL NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    compressed INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    l1_tokens_stripped INTEGER NOT NULL DEFAULT 0,
    l1_savings REAL NOT NULL DEFAULT 0
);
"""


def _sqlite_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    get_settings.cache_clear()
    from proxy import stats
    stats.init_db()
    return stats


def test_log_request_persists_tripwire_columns(tmp_path, monkeypatch):
    stats = _sqlite_env(tmp_path, monkeypatch)
    stats.log_request(
        model="gpt-4o-mini", route="compress", input_tokens_before=100,
        input_tokens_after=80, output_tokens=5, est_cost_before=0.0,
        est_cost_after=0.0, latency_ms=1.0, compressed=True, status=200,
        dose_tier="bounded", grounded_risk="fidelity_critical",
        envelope_shape=1,
    )
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT dose_tier, grounded_risk, envelope_shape FROM requests"
        ).fetchone()
    assert row["dose_tier"] == "bounded"
    assert row["grounded_risk"] == "fidelity_critical"
    assert row["envelope_shape"] == 1


def test_legacy_ledger_migrates_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    get_settings.cache_clear()
    db = tmp_path / "stats.db"
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    from proxy import stats
    stats.init_db()  # idempotent migration must add the AC-P6f columns
    stats.log_request(
        model="m", route="compress", input_tokens_before=10,
        input_tokens_after=10, output_tokens=0, est_cost_before=0.0,
        est_cost_after=0.0, latency_ms=0.0, compressed=False, status=200,
        dose_tier="full", grounded_risk="none", envelope_shape=0,
    )
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT dose_tier, grounded_risk, envelope_shape FROM requests"
        ).fetchone()
    assert row["dose_tier"] == "full"
    assert row["grounded_risk"] == "none"
    assert row["envelope_shape"] == 0


# --- Live request path ---------------------------------------------------------

_POLICY = (
    "Use only the provided policy text when answering. POLICY: Returns are "
    "accepted within 30 days of purchase with the original receipt; sale "
    "items are final; refunds process in five business days to the original "
    "payment method."
)
_GROUNDED_USER = (
    "Per the policy above, can I return a sale jacket I bought two weeks "
    "ago, and when would my refund arrive if it is accepted? Please quote "
    "the exact conditions."
)


def _pinned_app(monkeypatch, tmp_path):
    """App on an isolated SQLite ledger, legacy upstream via MockTransport."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv("L1_ENABLED", "false")
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")
    monkeypatch.setenv("MIN_CHARS_TO_CLASSIFY", "10")
    monkeypatch.setenv("OUTPUT_CONCISENESS_ENABLED", "true")
    monkeypatch.delenv("TOKEN_SAVER_PG_DSN", raising=False)
    get_settings.cache_clear()
    from proxy import stats
    stats.init_db()
    from proxy.main import app
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=UPSTREAM_RESPONSE)),
        base_url="http://upstream.test/v1")
    return app, stats


def _post(app, messages):
    async def _run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            return await c.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key-123",
                    "X-Token-Saver-Conciseness": "1",
                },
                json={"model": "gpt-4o-mini",
                      "messages": messages},
            )

    try:
        return asyncio.run(_run())
    finally:
        asyncio.run(app.state.http.aclose())


def test_request_path_records_tier_risk_and_envelope(tmp_path, monkeypatch):
    app, stats = _pinned_app(monkeypatch, tmp_path)
    messages = [
        {"role": "system", "content": _POLICY},
        {"role": "user", "content": _GROUNDED_USER},
    ]
    r = _post(app, messages)
    assert r.status_code == 200, r.text[:200]
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT dose_tier, grounded_risk, envelope_shape FROM requests"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    # rag-051 class: source block grounds the request; pre-calibration the
    # fidelity-critical tier caps at "none" — the RESOLVED tier is recorded.
    assert row["dose_tier"] == "none"
    assert row["grounded_risk"] == "fidelity_critical"
    assert row["envelope_shape"] == 0  # policy text, no retrieval envelope
    # parity: the pure discriminator agrees with what was recorded
    assert grounded_answer_risk(messages)["risk"] == "fidelity_critical"


def test_request_path_records_envelope_flag(tmp_path, monkeypatch):
    app, stats = _pinned_app(monkeypatch, tmp_path)
    messages = [{
        "role": "user",
        "content": "Answer from this context:\n"
                   + ENVELOPE_POSITIVES["fenced_json"]
                   + "\nWhat is the return window?",
    }]
    r = _post(app, messages)
    assert r.status_code == 200, r.text[:200]
    with stats.get_conn() as conn:
        row = conn.execute(
            "SELECT envelope_shape, grounded_risk, dose_tier FROM requests"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["envelope_shape"] == 1
    # The structured-JSON classifier protects RAG prompts via passthrough,
    # so the discriminator never ran on this request: grounded_risk/dose_tier
    # stay NULL while the envelope flag still records — precisely the
    # unclassified class the missed-grounding tripwire watches live.
    assert row["grounded_risk"] is None
    assert row["dose_tier"] is None


# --- Endpoint ------------------------------------------------------------------


def test_tripwire_endpoint_red_on_seeded_missed_grounding(tmp_path,
                                                          monkeypatch):
    app, stats = _pinned_app(monkeypatch, tmp_path)
    monkeypatch.setattr(tripwire, "RESULTS_DIR", tmp_path)
    stats.log_request(
        model="m", route="passthrough", input_tokens_before=1000,
        input_tokens_after=400, output_tokens=5, est_cost_before=0.0,
        est_cost_after=0.0, latency_ms=1.0, compressed=False, status=200,
        dose_tier=None, grounded_risk=None, envelope_shape=1,
    )
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.get("/api/tripwire")
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    assert data["status"] == "red"
    assert data["missed_grounding"]["status"] == "alert"
    assert data["dose_drift"]["status"] == "pending_calibration"
    assert data["calibration_band_pct"] is None


def test_tripwire_endpoint_green_on_benign_ledger(tmp_path, monkeypatch):
    app, stats = _pinned_app(monkeypatch, tmp_path)
    monkeypatch.setattr(tripwire, "RESULTS_DIR", tmp_path)
    # green requires a committed calibration band; without the artifact the
    # dose-drift rule honestly reports pending_calibration instead.
    (tmp_path / "calibration-rag-v2.json").write_text(
        json.dumps({"cut_pct_band": [2.0, 45.0]}))
    stats.log_request(
        model="m", route="compress", input_tokens_before=1000,
        input_tokens_after=700, output_tokens=5, est_cost_before=0.0,
        est_cost_after=0.0, latency_ms=1.0, compressed=True, status=200,
        dose_tier="bounded", grounded_risk="none", envelope_shape=0,
    )
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        data = c.get("/api/tripwire").json()
    assert data["status"] == "green"
    assert data["dose_drift"]["status"] == "clear"
    assert data["missed_grounding"]["status"] == "clear"
    assert data["rows_scanned"] == 1


def test_tripwire_endpoint_rejects_bad_window(tmp_path, monkeypatch):
    app, _ = _pinned_app(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.get("/api/tripwire", params={"days": 0})
    assert r.status_code == 400
