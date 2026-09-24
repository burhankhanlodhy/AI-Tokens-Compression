from benchmark.idcp_replay import run_replay


def test_provisional_replay_reports_50_multiedit_sessions_without_quality_claims():
    report = run_replay(session_count=50)

    assert report["evidence_kind"] == "provisional_synthetic"
    assert report["sessions"] == 50
    assert report["sessions_with_multiple_edits"] == 50
    assert report["task_success_regression_pp"] is None
    assert report["representative_corpus"] is False
    assert report["silent_stale_applications"] == 0
    assert report["baseline_repeated_file_tokens"] > 0
    assert report["idcp_repeated_file_tokens"] > 0
    assert report["repeated_file_context_reduction_pct"] > 0


def test_replay_is_deterministic_except_opaque_version_identifiers():
    first = run_replay(session_count=3)
    second = run_replay(session_count=3)

    for key in (
        "sessions", "sessions_with_multiple_edits", "baseline_repeated_file_tokens",
        "idcp_repeated_file_tokens", "repeated_file_context_reduction_pct",
        "task_success_regression_pp", "silent_stale_applications",
    ):
        assert first[key] == second[key]
