from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.tocp import (ContinuationStore, ContinuationNotFound, InvalidRange,
                        StoreCapacityExceeded, build_continuation_result)


def test_continuation_returns_bounded_segments_and_exact_range():
    store = ContinuationStore(ttl_seconds=30, max_entries=10, segment_chars=5)
    record = store.save("tenant-a", "session-a", "abcdefghijk", metadata={"exit_code": 0})
    assert record.available_segments == 3
    assert record.preview == "abcde"
    assert store.get_segment(record.continuation_id, "tenant-a", "session-a", 1) == "fghij"
    assert store.get_range(record.continuation_id, "tenant-a", "session-a", 2, 4) == "cd"


def test_missing_expired_and_wrong_scope_are_not_retrievable():
    clock = [100.0]
    store = ContinuationStore(ttl_seconds=2, max_entries=10, segment_chars=4, clock=lambda: clock[0])
    record = store.save("t", "s", "abcdefgh")
    for tenant, session in [("other", "s"), ("t", "other")]:
        with pytest.raises(ContinuationNotFound):
            store.get_segment(record.continuation_id, tenant, session, 0)
    clock[0] = 103.0
    with pytest.raises(ContinuationNotFound):
        store.get_segment(record.continuation_id, "t", "s", 0)
    with pytest.raises(ContinuationNotFound):
        store.get_segment("missing", "t", "s", 0)


def test_malformed_ranges_and_cleanup():
    store = ContinuationStore(ttl_seconds=1, max_entries=10, segment_chars=4)
    record = store.save("t", "s", "abcdefgh")
    with pytest.raises(InvalidRange):
        store.get_segment(record.continuation_id, "t", "s", -1)
    with pytest.raises(InvalidRange):
        store.get_range(record.continuation_id, "t", "s", 2, 1)
    assert store.cleanup() >= 0


def test_entry_limit_fails_closed_without_evicting_unexpired_entries():
    clock = [1.0]
    store = ContinuationStore(ttl_seconds=10, max_entries=1, segment_chars=4, clock=lambda: clock[0])
    first = store.save("t", "s", "first result")
    clock[0] += 1
    with pytest.raises(StoreCapacityExceeded):
        store.save("t", "s", "second result")
    assert store.get_segment(first.continuation_id, "t", "s", 0) == "firs"


def test_total_store_capacity_is_bounded():
    store = ContinuationStore(ttl_seconds=10, max_entries=10, segment_chars=2,
                              max_total_chars=5)
    first = store.save("t", "s", "abcd")
    with pytest.raises(StoreCapacityExceeded):
        store.save("t", "s", "efgh")
    assert store.size == 1
    assert store.get_segment(first.continuation_id, "t", "s", 0) == "ab"
    with pytest.raises(ValueError):
        store.save("t", "s", "123456")


def test_explicit_cleanup_removes_expired_items():
    clock = [1.0]
    store = ContinuationStore(ttl_seconds=1, max_entries=10, segment_chars=4, clock=lambda: clock[0])
    store.save("t", "s", "abcd")
    clock[0] = 3.0
    assert store.cleanup() == 1
    assert store.size == 0


def test_json_result_envelope_preserves_status_error_and_exit_code():
    store = ContinuationStore(ttl_seconds=10, max_entries=10, segment_chars=12)
    raw = json.dumps({"status": "failed", "error": "compile error", "exit_code": 2,
                      "output": "compiler output " * 8})
    rendered = build_continuation_result(raw, "tenant", "session", store=store)
    result = json.loads(rendered)
    assert result["status"] == "failed"
    assert result["error"] == "compile error"
    assert result["exit_code"] == 2
    assert result["_tocp"]["omitted_chars"] > 0
    assert result["_tocp"]["available_segments"] > 1
    assert len(result["preview"]) == 12


@pytest.mark.parametrize("sample", [
    "plain text output\n" * 20,
    json.dumps({"status": "ok", "items": list(range(100))}),
    "============================= test session starts =============================\n" + "FAILED test_x.py::test_y\n" * 30,
    "src/main.c:12: error: expected ';'\n" * 30,
])
def test_text_json_pytest_and_compiler_payloads_reconstruct_exactly(sample):
    store = ContinuationStore(ttl_seconds=20, max_entries=10, segment_chars=17)
    record = store.save("tenant", "session", sample)
    rebuilt = "".join(store.get_segment(record.continuation_id, "tenant", "session", i)
                      for i in range(record.available_segments))
    assert rebuilt == sample


def test_replay_50_over_cap_results_fetches_without_rerunning_original():
    store = ContinuationStore(ttl_seconds=60, max_entries=60, segment_chars=64)
    results = [f"pytest/compiler result {i}: " + (f"line-{i}\n" * 40) for i in range(50)]
    fetched_segments = 0
    reruns = 0
    for index, result in enumerate(results):
        record = store.save("tenant", f"session-{index}", result)
        segments = [store.get_segment(record.continuation_id, "tenant", f"session-{index}", part)
                    for part in range(record.available_segments)]
        fetched_segments += len(segments)
        assert "".join(segments) == result
    assert store.size == 50
    assert fetched_segments >= 50
    assert reruns == 0


def test_retrieval_route_requires_trusted_scope_and_returns_only_scoped_segment():
    from fastapi.testclient import TestClient
    from proxy import main
    from proxy.tocp import continuation_store

    continuation_store._entries.clear()
    record = continuation_store.save("tenant-route", "session-route", "first segment second")
    class TrustedScopeTestApp:
        """Supply trusted server state without mutating a possibly-started app."""

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope.get("headers", ()))
                if headers.get(b"x-test-trusted") == b"yes":
                    scope.setdefault("state", {}).update(
                        tenant_id="tenant-route", session_id="session-route"
                    )
            await main.app(scope, receive, send)

    client = TestClient(TrustedScopeTestApp())
    denied = client.get(f"/v1/tool-results/{record.continuation_id}?segment=0")
    assert denied.status_code == 401
    allowed = client.get(f"/v1/tool-results/{record.continuation_id}?segment=0")
    assert allowed.status_code == 401
    trusted = client.get(f"/v1/tool-results/{record.continuation_id}?segment=0",
                         headers={"x-test-trusted": "yes"})
    assert trusted.status_code == 200
    assert trusted.json()["content"] == "first segment second"
