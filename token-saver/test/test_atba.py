from proxy.atba import (
    ATBAMetrics,
    BudgetDefaults,
    OutputBudgetInput,
    TurnIntent,
    classify_turn,
    evaluate_paired_sessions,
    plan_turn_budget,
    recommend_output_budget,
)


def test_classifier_recognizes_turn_types_and_unknown_is_neutral():
    assert classify_turn("Brainstorm some alternative approaches").intent is TurnIntent.EXPLORATORY
    assert classify_turn("Implement the parser and add unit tests").intent is TurnIntent.IMPLEMENTATION
    assert classify_turn("Debug this traceback and fix the error").intent is TurnIntent.DEBUGGING
    assert classify_turn("Verify the results and run the tests").intent is TurnIntent.VERIFICATION
    assert classify_turn("hello").intent is TurnIntent.UNKNOWN


def test_neutral_turn_budget_preserves_defaults_and_session_reserve():
    defaults = BudgetDefaults(file_cap=8, tool_cap=6, session_reserve_tokens=900)
    result = plan_turn_budget("hello", defaults, remaining_session_tokens=1200)
    assert result.intent is TurnIntent.UNKNOWN
    assert result.file_cap == defaults.file_cap
    assert result.tool_cap == defaults.tool_cap
    assert result.session_reserve_tokens == 900
    assert result.available_turn_tokens == 300


def test_explicit_context_insufficiency_allows_only_one_bounded_escalation():
    defaults = BudgetDefaults(file_cap=4, tool_cap=3, session_reserve_tokens=500)
    first = plan_turn_budget("debug stack trace", defaults, remaining_session_tokens=2500,
                             context_insufficient=True)
    second = plan_turn_budget("debug stack trace", defaults, remaining_session_tokens=2500,
                              context_insufficient=True, escalation_count=1)
    assert first.escalation == "bounded"
    assert first.file_cap == 5
    assert first.tool_cap == 3
    assert second.escalation is None
    assert second.file_cap <= defaults.file_cap * 2


def test_output_policy_is_separate_fails_open_and_preserves_explicit_controls():
    user_text = "Implement a small parser"
    body = {"max_tokens": 777, "reasoning": {"effort": "high"}}
    explicit = recommend_output_budget(OutputBudgetInput(
        user_text=user_text, body=body,
        provider_supports_adaptive=False,
    ))
    assert explicit.max_output_tokens is None
    assert explicit.max_reasoning_tokens is None
    assert explicit.preserve_reason == "explicit_client_control"
    unsupported = recommend_output_budget(OutputBudgetInput(
        user_text=user_text, body={}, provider_supports_adaptive=False,
    ))
    assert unsupported.max_output_tokens is None
    assert unsupported.max_reasoning_tokens is None
    assert unsupported.preserve_reason == "provider_unsupported"
    assert body == {"max_tokens": 777, "reasoning": {"effort": "high"}}


def test_quality_sensitive_and_conciseness_off_turns_keep_output_unbounded():
    result = recommend_output_budget(OutputBudgetInput(
        user_text="Verify every cited source and explain uncertainty in this medical advice",
        body={}, provider_supports_adaptive=True, conciseness_enabled=False,
    ))
    assert result.max_output_tokens is None
    assert result.max_reasoning_tokens is None
    assert result.preserve_reason == "quality_sensitive"

    advisory = recommend_output_budget(OutputBudgetInput(
        user_text="Implement a small parser", body={},
        provider_supports_adaptive=True, conciseness_enabled=True,
    ))
    assert advisory.max_output_tokens == 2048
    assert advisory.max_reasoning_tokens == 1024
    assert advisory.preserve_reason is None


def test_paired_evidence_requires_100_sessions_and_quality_noninferiority():
    samples = [{"baseline_cost": 0.01, "treatment_cost": 0.009,
                "baseline_success": True, "treatment_success": True}
               for _ in range(100)]
    result = evaluate_paired_sessions(samples)
    assert result.session_count == 100
    assert result.net_improvement is True
    assert result.quality_regression_pp == 0
    assert result.enforcement_eligible is True
    for index in range(4):
        samples[index]["treatment_success"] = False
    assert evaluate_paired_sessions(samples).enforcement_eligible is False
    assert evaluate_paired_sessions(samples[:99]).enforcement_eligible is False


def test_metrics_include_cost_latency_retries_overhead_and_success_separately():
    metrics = ATBAMetrics(input_tokens=100, output_tokens=20, cost_usd=0.03,
                          latency_ms=125.5, retry_count=1, escalation_count=1,
                          classifier_overhead_ms=0.2, policy_overhead_ms=0.3,
                          task_success=True).to_dict()
    assert metrics == {
        "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.03,
        "latency_ms": 125.5, "retry_count": 1, "escalation_count": 1,
        "classifier_overhead_ms": 0.2, "policy_overhead_ms": 0.3,
        "task_success": True,
    }


def test_escalation_does_not_spend_session_reserve():
    result = plan_turn_budget("debug error", BudgetDefaults(4, 4, 500),
                              remaining_session_tokens=400,
                              context_insufficient=True)
    assert result.available_turn_tokens == 0
    assert result.escalation is None
