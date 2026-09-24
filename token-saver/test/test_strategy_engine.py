from __future__ import annotations

import json

import pytest

from proxy.config import Settings
from proxy.strategy_engine import (
    EvidenceDimensions,
    PolicyContext,
    StrategyRegistry,
    StrategyStatus,
    record_decision,
)


def test_new_strategy_flags_are_server_side_and_default_off():
    settings = Settings(_env_file=None)
    assert settings.v21_deferred_tools_enabled is False
    assert settings.v21_tocp_enabled is False
    assert settings.v21_idcp_enabled is False
    assert settings.v21_atba_enabled is False
    assert settings.v21_atba_enforce is False
    assert settings.v21_mtcc_enabled is False


def test_registry_exposes_flag_off_decisions_and_fallbacks():
    registry = StrategyRegistry(Settings(_env_file=None))
    decisions = registry.evaluate(PolicyContext(
        tenant_id="tenant-a", api_key_id="key-a", session_id="session-a"
    ))
    by_name = {item.strategy: item for item in decisions}
    assert set(by_name) >= {"deferred_tools", "tocp", "idcp", "atba", "mtcc"}
    assert by_name["tocp"].status is StrategyStatus.SKIPPED
    assert by_name["tocp"].reason == "flag_disabled"
    assert by_name["tocp"].fallback == "full_output"
    assert by_name["tocp"].version
    assert by_name["tocp"].flag_enabled is False


def test_policy_requires_tenant_and_api_key_scope_and_session_for_session_lanes():
    settings = Settings(v21_tocp_enabled=True, _env_file=None)
    registry = StrategyRegistry(settings)
    decision = registry.evaluate(PolicyContext(tenant_id="", api_key_id=None, session_id=None))
    tocp = next(item for item in decision if item.strategy == "tocp")
    assert tocp.status is StrategyStatus.FALLBACK
    assert tocp.reason == "scope_missing"
    assert tocp.fallback == "full_output"

    no_session = registry.evaluate(PolicyContext(
        tenant_id="tenant-a", api_key_id="key-a", session_id=None
    ))
    assert next(item for item in no_session if item.strategy == "tocp").status is StrategyStatus.FALLBACK


def test_full_context_escape_hatch_skips_risky_lane_even_if_enabled():
    registry = StrategyRegistry(Settings(v21_idcp_enabled=True, _env_file=None))
    decision = registry.evaluate(PolicyContext(
        tenant_id="tenant-a", api_key_id="key-a", session_id="session-a", full_context=True
    ))
    idcp = next(item for item in decision if item.strategy == "idcp")
    assert idcp.status is StrategyStatus.FALLBACK
    assert idcp.reason == "full_context_requested"
    assert idcp.fallback == "full_file"


def test_evidence_dimensions_keep_transform_cache_and_avoided_call_separate():
    evidence = EvidenceDimensions(
        input_tokens_before=100,
        input_tokens_after=70,
        provider_cache_read_tokens=20,
        provider_cache_write_tokens=3,
        avoided_upstream_calls=1,
        policy_overhead_tokens=2,
    )
    payload = evidence.to_dict()
    assert payload == {
        "input_tokens_before": 100,
        "input_tokens_after": 70,
        "provider_cache_read_tokens": 20,
        "provider_cache_write_tokens": 3,
        "avoided_upstream_calls": 1,
        "policy_overhead_tokens": 2,
        "output_tokens_before": None,
        "output_tokens_after": None,
        "retry_count": 0,
    }
    assert not any("savings" in key for key in payload)


def test_record_decision_is_fail_open_when_telemetry_write_fails():
    class BrokenConnection:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    registry = StrategyRegistry(Settings(_env_file=None))
    decision = registry.evaluate(PolicyContext(
        tenant_id="tenant-a", api_key_id="key-a", session_id="session-a"
    ))[0]
    # Telemetry failure must not turn an already-computed policy decision into a request failure.
    assert record_decision(BrokenConnection(), decision, PolicyContext(
        tenant_id="tenant-a", api_key_id="key-a", session_id="session-a"
    )) is False


def test_telemetry_payload_is_scoped_and_does_not_add_savings_columns():
    class RecordingConnection:
        def __init__(self):
            self.sql = None
            self.params = None

        def execute(self, sql, params):
            self.sql, self.params = sql, params

    scope = PolicyContext(tenant_id="tenant-a", api_key_id="key-a", session_id="session-a")
    decision = next(item for item in StrategyRegistry(Settings(v21_tocp_enabled=True, _env_file=None)).evaluate(scope)
                    if item.strategy == "tocp")
    conn = RecordingConnection()
    assert record_decision(conn, decision, scope, evidence=EvidenceDimensions(input_tokens_before=8))
    assert "INSERT INTO strategy_telemetry" in conn.sql
    assert "tenant-a" in conn.params and "key-a" in conn.params and "session-a" in conn.params
    assert "JOIN api_keys AS k ON k.tenant_id = t.id" in conn.sql
    metadata = json.loads(conn.params[-3])
    assert metadata["evidence"]["input_tokens_before"] == 8
    assert "savings" not in conn.sql.lower()


def test_atba_stays_shadow_until_enforcement_is_explicit():
    context = PolicyContext(tenant_id="tenant-a", api_key_id="key-a", session_id="session-a")
    shadow = StrategyRegistry(Settings(v21_atba_enabled=True, _env_file=None)).evaluate(context)
    atba = next(item for item in shadow if item.strategy == "atba")
    assert atba.status is StrategyStatus.SHADOW
    assert atba.reason == "shadow_mode"

    enforced = StrategyRegistry(Settings(
        v21_atba_enabled=True, v21_atba_enforce=True, _env_file=None
    )).evaluate(context)
    assert next(item for item in enforced if item.strategy == "atba").status is StrategyStatus.ELIGIBLE


def test_telemetry_refuses_api_key_from_different_tenant():
    class NoMatchingTenantKey:
        rowcount = 0

    class Connection:
        def execute(self, *_args, **_kwargs):
            return NoMatchingTenantKey()

    scope = PolicyContext(tenant_id="tenant-a", api_key_id="key-owned-by-b", session_id="session-a")
    decision = StrategyRegistry(Settings(_env_file=None)).evaluate(scope)[0]
    assert record_decision(Connection(), decision, scope) is False


def test_strategy_status_endpoint_is_admin_only_and_reports_default_off(monkeypatch):
    from fastapi.testclient import TestClient
    from proxy import main

    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    main.app.state.admin_token = "test-admin"
    with TestClient(main.app) as client:
        denied = client.get("/api/strategies")
        assert denied.status_code == 401
        response = client.get("/api/strategies", headers={"Authorization": "Bearer test-admin"})
    assert response.status_code == 200
    states = {row["strategy"]: row for row in response.json()["strategies"]}
    assert states["tocp"]["flag_enabled"] is False
    assert states["tocp"]["fallback"] == "full_output"
    assert states["atba"]["enforcement_enabled"] is False
    assert states["semantic_cache"]["flag_enabled"] is False


def test_client_header_cannot_enable_strategy_flag(monkeypatch):
    from fastapi.testclient import TestClient
    from proxy import main

    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    main.app.state.admin_token = "test-admin"
    with TestClient(main.app) as client:
        response = client.get("/api/strategies", headers={
            "Authorization": "Bearer test-admin", "X-Token-Saver-TOCP": "true"
        })
    assert response.status_code == 200
    tocp = next(row for row in response.json()["strategies"] if row["strategy"] == "tocp")
    assert tocp["flag_enabled"] is False
