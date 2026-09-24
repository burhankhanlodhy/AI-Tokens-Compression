import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.mtcc import ConversationTurn, MTCCConfig, MTCCStore, compress_history
from proxy.mtcc import StoreCapacityExceeded, TurnNotFound


def test_recent_turns_are_verbatim_and_older_turn_is_retrievable_exactly():
    turns = [
        ConversationTurn("user", "Please change src/app.py and keep the API compatible."),
        ConversationTurn("assistant", "I will inspect src/app.py."),
        ConversationTurn("tool", "Read src/app.py: def run(): return 1"),
        ConversationTurn("assistant", "The function is simple."),
    ]
    store = MTCCStore(config=MTCCConfig(recent_turns=2))
    result = compress_history(turns, tenant_id="tenant-a", api_key_id="key-a",
                              session_id="session-a", store=store)
    assert result.messages[-2:] == [turn.to_message() for turn in turns[-2:]]
    assert result.messages[0]["content"].startswith("MTCC structured facts")
    ref = result.source_refs[0]
    assert any(message["content"] == turns[0].content for message in result.messages)
    assert store.retrieve(ref, "tenant-a", "key-a", "session-a") == turns[0]


def test_user_constraints_errors_and_referenced_tool_output_are_not_collapsed():
    turns = [
        ConversationTurn("user", "Constraint: do not change public API; keep src/api.py stable."),
        ConversationTurn("tool", "ERROR: migration failed at db/schema.sql: line 22"),
        ConversationTurn("assistant", "Tool output ref: result-123"),
        ConversationTurn("user", "Thanks, that's all."),
        ConversationTurn("assistant", "You're welcome."),
    ]
    result = compress_history(turns, tenant_id="t", api_key_id="k", session_id="s",
                              store=MTCCStore(config=MTCCConfig(recent_turns=1)))
    rendered = "\n".join(message["content"] for message in result.messages)
    for required in ("do not change public API", "migration failed", "result-123"):
        assert required in rendered
    assert result.protected_turns >= 3


def test_store_rejects_cross_tenant_key_and_session_reads():
    store = MTCCStore()
    result = compress_history([ConversationTurn("assistant", "older original")],
                              tenant_id="t1", api_key_id="k1", session_id="s1", store=store)
    ref = result.source_refs[0]
    for tenant, key, session in (("t2", "k1", "s1"), ("t1", "k2", "s1"), ("t1", "k1", "s2")):
        with pytest.raises(TurnNotFound):
            store.retrieve(ref, tenant, key, session)


def test_expired_source_turn_is_not_returned():
    now = [100.0]
    store = MTCCStore(config=MTCCConfig(ttl_seconds=5, max_turns=1), clock=lambda: now[0])
    result = compress_history([ConversationTurn("assistant", "original")],
                              tenant_id="t", api_key_id="k", session_id="s", store=store)
    with pytest.raises(StoreCapacityExceeded):
        store.save("t", "k", "s", 1, ConversationTurn("assistant", "second"))
    assert store.retrieve(result.source_refs[0], "t", "k", "s") == ConversationTurn("assistant", "original")
    now[0] = 105.0
    with pytest.raises(TurnNotFound):
        store.retrieve(result.source_refs[0], "t", "k", "s")


def test_replay_30_sessions_10_turns_keeps_quality_gate_inconclusive():
    from proxy.mtcc import replay_fixture_sessions

    report = replay_fixture_sessions(session_count=30, turns_per_session=10)
    assert report.sessions == 30
    assert report.turns == 300
    assert report.context_reduction_pct < 30  # fixture does not clear the ratified reduction gate
    assert report.task_success_regression_pp is None  # synthetic replay cannot establish quality
    assert report.wrong_diagnosis_delta is None
    assert report.summarizer_provider_cost_usd == 0
    assert report.summarizer_latency_ms >= 0
