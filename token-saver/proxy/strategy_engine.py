"""V2.1 strategy registry, fail-open policy decisions, and non-overlapping telemetry.

The registry is descriptive/policy infrastructure: existing V2.0 request stages
remain untouched. Risky V2.1 lanes are deployment-controlled and default off.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

_LOG = logging.getLogger("token-saver.strategy")


class StrategyStatus(str, Enum):
    ELIGIBLE = "eligible"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FALLBACK = "fallback"
    SHADOW = "shadow"


@dataclass(frozen=True)
class PolicyContext:
    """Trusted request scope; caller must derive these from authenticated server state."""

    tenant_id: str | None
    api_key_id: str | None
    session_id: str | None = None
    full_context: bool = False
    full_output: bool = False

    @property
    def scope_valid(self) -> bool:
        return bool(self.tenant_id and self.tenant_id.strip() and self.api_key_id and self.api_key_id.strip())


@dataclass(frozen=True)
class EvidenceDimensions:
    """Independent observations only; intentionally contains no savings aggregate."""

    input_tokens_before: int | None = None
    input_tokens_after: int | None = None
    output_tokens_before: int | None = None
    output_tokens_after: int | None = None
    provider_cache_read_tokens: int | None = None
    provider_cache_write_tokens: int | None = None
    avoided_upstream_calls: int | None = None
    policy_overhead_tokens: int | None = None
    retry_count: int = 0

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if value is not None and value < 0:
                raise ValueError(f"{key} must be non-negative")

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyDecision:
    strategy: str
    status: StrategyStatus
    reason: str
    version: str
    fallback: str
    flag_enabled: bool
    evidence_dimensions: tuple[str, ...] = ()
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")


@dataclass(frozen=True)
class _StrategySpec:
    name: str
    flag: str
    fallback: str
    requires_session: bool = True
    full_context_escape: bool = True
    version: str = "v2.2.0"
    dimensions: tuple[str, ...] = ()


_SPECS = (
    _StrategySpec("deferred_tools", "v21_deferred_tools_enabled", "eager_tool_schemas", dimensions=("catalog_tokens", "search_overhead_tokens", "miss_count", "retry_count")),
    _StrategySpec("tocp", "v21_tocp_enabled", "full_output", dimensions=("input_tokens_before", "input_tokens_after", "retrieval_count", "policy_overhead_tokens")),
    _StrategySpec("idcp", "v21_idcp_enabled", "full_file", dimensions=("input_tokens_before", "input_tokens_after", "fallback_count", "policy_overhead_tokens")),
    _StrategySpec("atba", "v21_atba_enabled", "static_budget", dimensions=("input_tokens_before", "input_tokens_after", "output_tokens_before", "output_tokens_after", "policy_overhead_tokens", "retry_count")),
    _StrategySpec("mtcc", "v21_mtcc_enabled", "verbatim_history", dimensions=("input_tokens_before", "input_tokens_after", "retrieval_count", "policy_overhead_tokens")),
)

# Existing lane flags are exposed for audit only. The V2.1 registry never applies
# or overrides them, preserving their established V2.0 request semantics.
_EXISTING_FLAGS: Mapping[str, str] = {
    "l1": "l1_enabled",
    "conciseness": "output_conciseness_enabled",
    "provider_cache": "cache_enabled",
    "semantic_cache": "semantic_cache_enabled",
    "routing_discovery": "provider_routing",
}


class StrategyRegistry:
    def __init__(self, settings: Any):
        self._settings = settings

    def status(self) -> list[dict[str, str | bool]]:
        """Return deployment flag state without exposing client controls."""
        result = [
            {"strategy": spec.name, "version": spec.version,
             "flag_enabled": bool(getattr(self._settings, spec.flag, False)),
             "enforcement_enabled": bool(getattr(self._settings, "v21_atba_enforce", False)) if spec.name == "atba" else False,
             "default_off": True, "fallback": spec.fallback}
            for spec in _SPECS
        ]
        result.extend(
            {"strategy": name, "version": "v2.0", "flag_enabled": bool(getattr(self._settings, flag, False)),
             "default_off": False, "fallback": "existing_behavior"}
            for name, flag in _EXISTING_FLAGS.items()
        )
        return result

    def evaluate(self, context: PolicyContext) -> list[StrategyDecision]:
        decisions: list[StrategyDecision] = []
        for spec in _SPECS:
            enabled = bool(getattr(self._settings, spec.flag, False))
            if not enabled:
                status, reason = StrategyStatus.SKIPPED, "flag_disabled"
            elif not context.scope_valid or (spec.requires_session and not context.session_id):
                status, reason = StrategyStatus.FALLBACK, "scope_missing"
            elif spec.full_context_escape and (context.full_context or context.full_output):
                status, reason = StrategyStatus.FALLBACK, "full_context_requested"
            elif spec.name == "atba" and not bool(getattr(self._settings, "v21_atba_enforce", False)):
                status, reason = StrategyStatus.SHADOW, "shadow_mode"
            else:
                status, reason = StrategyStatus.ELIGIBLE, "policy_eligible"
            decisions.append(StrategyDecision(
                strategy=spec.name, status=status, reason=reason,
                version=spec.version, fallback=spec.fallback, flag_enabled=enabled,
                evidence_dimensions=spec.dimensions,
            ))
        return decisions


def record_decision(
    connection: Any,
    decision: StrategyDecision,
    context: PolicyContext,
    *,
    evidence: EvidenceDimensions | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort write to DBA's strategy_telemetry table; never breaks proxy flow.

    Tenant and API-key identifiers are trusted authenticated scope values, not
    caller-supplied headers. The SQL and metadata deliberately contain no savings
    total; billable attribution remains with the existing request ledger.
    """
    if not context.scope_valid:
        _LOG.warning("strategy telemetry skipped: authenticated scope missing")
        return False
    payload: dict[str, Any] = {"fallback": decision.fallback,
                               "evidence_dimensions": list(decision.evidence_dimensions)}
    if evidence is not None:
        payload["evidence"] = evidence.to_dict()
    if metadata:
        payload["lane"] = dict(metadata)
    try:
        latency = decision.latency_ms
        cursor = connection.execute(
            """INSERT INTO strategy_telemetry
               (tenant_id, api_key_id, session_id, strategy, decision, reason,
                strategy_version, flag_enabled, latency_ms, metadata)
               SELECT %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
               WHERE EXISTS (
                   SELECT 1 FROM tenants AS t
                   JOIN api_keys AS k ON k.tenant_id = t.id
                   WHERE t.id = %s::uuid AND k.id = %s::uuid
               )""",
            (context.tenant_id, context.api_key_id, context.session_id,
             decision.strategy, decision.status.value, decision.reason,
             decision.version, decision.flag_enabled, latency,
             json.dumps(payload, separators=(",", ":")),
             context.tenant_id, context.api_key_id),
        )
        return getattr(cursor, "rowcount", 1) != 0
    except Exception:  # telemetry is explicitly best-effort / fail-open
        _LOG.warning("strategy telemetry write failed for %s", decision.strategy, exc_info=True)
        return False
