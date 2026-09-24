"""Conservative Adaptive Turn-Budget Allocator (ATBA) policy primitives.

The module is deliberately pure/local: callers may observe shadow decisions, but
must not apply them unless the separately gated enforcement lane is approved.
No prompt content is retained or logged. Unknown intent and unsupported controls
fail open to caller-supplied current defaults.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from dataclasses import dataclass
from enum import Enum
from time import perf_counter_ns
from typing import Any, Mapping, Sequence


class TurnIntent(str, Enum):
    EXPLORATORY = "exploratory"
    IMPLEMENTATION = "implementation"
    DEBUGGING = "debugging"
    VERIFICATION = "verification"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classification:
    intent: TurnIntent
    confidence: float
    policy_overhead_ms: float


# Conservative, transparent phrase cues; ambiguity always returns UNKNOWN.
_CUES = {
    TurnIntent.EXPLORATORY: re.compile(r"\b(brainstorm|explore|alternatives?|ideas?|options?|what if)\b", re.I),
    TurnIntent.IMPLEMENTATION: re.compile(r"\b(implement|build|add|write|create|refactor|feature)\b", re.I),
    TurnIntent.DEBUGGING: re.compile(r"\b(debug|traceback|stack trace|exception|error|bug|failing)\b", re.I),
    TurnIntent.VERIFICATION: re.compile(r"\b(verify|validate|test|check|audit|confirm|prove)\b", re.I),
}


def classify_turn(text: str) -> Classification:
    """Classify locally by explicit lexical cues; collisions are neutral."""
    started = perf_counter_ns()
    hits = [intent for intent, pattern in _CUES.items() if pattern.search(text or "")]
    intent = hits[0] if len(hits) == 1 else TurnIntent.UNKNOWN
    confidence = 0.72 if intent is not TurnIntent.UNKNOWN else 0.0
    return Classification(intent, confidence, (perf_counter_ns() - started) / 1_000_000)


@dataclass(frozen=True)
class BudgetDefaults:
    file_cap: int
    tool_cap: int
    session_reserve_tokens: int

    def __post_init__(self) -> None:
        if min(self.file_cap, self.tool_cap) < 0 or self.session_reserve_tokens < 0:
            raise ValueError("budget defaults must be non-negative")


@dataclass(frozen=True)
class TurnBudget:
    intent: TurnIntent
    file_cap: int
    tool_cap: int
    session_reserve_tokens: int
    available_turn_tokens: int
    escalation: str | None
    policy_overhead_ms: float


def plan_turn_budget(
    text: str,
    defaults: BudgetDefaults,
    *,
    remaining_session_tokens: int,
    context_insufficient: bool = False,
    escalation_count: int = 0,
) -> TurnBudget:
    """Recommend a bounded cap; reserve is never consumed by a turn."""
    classification = classify_turn(text)
    files, tools = defaults.file_cap, defaults.tool_cap
    if classification.intent is TurnIntent.EXPLORATORY:
        files, tools = max(1, int(files * 0.75)), max(1, int(tools * 0.75))
    elif classification.intent in (TurnIntent.DEBUGGING, TurnIntent.VERIFICATION):
        files, tools = int(files * 1.25), int(tools * 1.25)
    # Implementation and UNKNOWN retain exact current defaults.
    available = max(0, remaining_session_tokens - defaults.session_reserve_tokens)
    escalation = None
    if context_insufficient and escalation_count == 0 and available > 0:
        files = min(max(files, 1), max(defaults.file_cap, 1) * 2)
        tools = min(max(tools, 1), max(defaults.tool_cap, 1) * 2)
        escalation = "bounded"
    return TurnBudget(classification.intent, files, tools,
                      defaults.session_reserve_tokens, available,
                      escalation, classification.policy_overhead_ms)


@dataclass(frozen=True)
class OutputBudgetInput:
    user_text: str
    body: Mapping[str, Any]
    provider_supports_adaptive: bool
    conciseness_enabled: bool = False


@dataclass(frozen=True)
class OutputBudgetDecision:
    max_output_tokens: int | None
    max_reasoning_tokens: int | None
    preserve_reason: str | None
    policy_overhead_ms: float


_QUALITY_SENSITIVE = re.compile(
    r"\b(verify|verification|citation|citations|source|sources|legal|medical|safety|"
    r"security|audit|uncertainty|explain|reasoning|step[- ]by[- ]step|proof|prove)\b", re.I
)
_EXPLICIT_OUTPUT_KEYS = frozenset({
    "max_tokens", "max_output_tokens", "max_new_tokens", "max_completion_tokens",
    "reasoning", "thinking_level", "thinking", "thinking_config",
    "reasoning_effort", "budget_tokens",
})


def recommend_output_budget(request: OutputBudgetInput) -> OutputBudgetDecision:
    """Return a recommendation only; never mutates the provider request body."""
    started = perf_counter_ns()
    reason = None
    maximum = None
    reasoning_maximum = None
    if _EXPLICIT_OUTPUT_KEYS.intersection(request.body):
        reason = "explicit_client_control"
    elif not request.provider_supports_adaptive:
        reason = "provider_unsupported"
    elif _QUALITY_SENSITIVE.search(request.user_text or ""):
        reason = "quality_sensitive"
    elif not request.conciseness_enabled:
        reason = "conciseness_disabled"
    else:
        classification = classify_turn(request.user_text)
        # Separate policy dimension; recommendation is advisory and modest.
        maximum = {TurnIntent.EXPLORATORY: 1024, TurnIntent.IMPLEMENTATION: 2048,
                   TurnIntent.DEBUGGING: 3072, TurnIntent.VERIFICATION: 4096}.get(classification.intent)
        reasoning_maximum = {TurnIntent.EXPLORATORY: 512, TurnIntent.IMPLEMENTATION: 1024,
                             TurnIntent.DEBUGGING: 2048, TurnIntent.VERIFICATION: 2048}.get(classification.intent)
        if maximum is None:
            reason = "uncertain_intent"
    return OutputBudgetDecision(maximum, reasoning_maximum, reason,
                                (perf_counter_ns() - started) / 1_000_000)


@dataclass(frozen=True)
class PairedEvaluation:
    session_count: int
    baseline_cost: float
    treatment_cost: float
    net_improvement: bool
    quality_regression_pp: float
    enforcement_eligible: bool


@dataclass(frozen=True)
class ATBAMetrics:
    """Non-overlapping observations; cost includes retries and policy overhead."""
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    retry_count: int = 0
    escalation_count: int = 0
    classifier_overhead_ms: float = 0.0
    policy_overhead_ms: float = 0.0
    task_success: bool | None = None

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.retry_count,
               self.escalation_count) < 0 or min(self.cost_usd, self.latency_ms,
               self.classifier_overhead_ms, self.policy_overhead_ms) < 0:
            raise ValueError("ATBA metrics must be non-negative")
        if self.task_success is not None and not isinstance(self.task_success, bool):
            raise ValueError("task_success must be bool or None")

    def to_dict(self) -> dict[str, int | float | bool | None]:
        return asdict(self)


def evaluate_paired_sessions(samples: Sequence[Mapping[str, Any]]) -> PairedEvaluation:
    """Evaluate paired shadow/simulation evidence, including policy overhead cost.

    Costs must already include model tokens, retries/escalations, and measured
    policy overhead. Missing/malformed records fail closed (ineligible).
    """
    n = len(samples)
    valid = all(
        isinstance(row.get("baseline_cost"), (int, float))
        and isinstance(row.get("treatment_cost"), (int, float))
        and row["baseline_cost"] >= 0 and row["treatment_cost"] >= 0
        and isinstance(row.get("baseline_success"), bool)
        and isinstance(row.get("treatment_success"), bool)
        for row in samples
    )
    baseline_cost = sum(float(r["baseline_cost"]) for r in samples) if valid else 0.0
    treatment_cost = sum(float(r["treatment_cost"]) for r in samples) if valid else 0.0
    if n:
        base_success = sum(r["baseline_success"] for r in samples) / n if valid else 0.0
        treatment_success = sum(r["treatment_success"] for r in samples) / n if valid else 0.0
        regression = max(0.0, (base_success - treatment_success) * 100)
    else:
        regression = 0.0
    improvement = valid and treatment_cost < baseline_cost
    eligible = bool(valid and n >= 100 and improvement and regression <= 3.0)
    return PairedEvaluation(n, baseline_cost, treatment_cost, improvement,
                            regression, eligible)
