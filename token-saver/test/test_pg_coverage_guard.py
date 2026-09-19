"""Regression guard for the Postgres-sensitive acceptance-test inventory."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PG_MODULES = {
    "test_ac_a10_concurrency.py",
    "test_ac_a11_backfill.py",
    "test_ac_a12_ledger_routes.py",
    "test_ac_a13_secret_handling.py",
    "test_ac_a7_tenant_isolation.py",
    "test_benchmark_control.py",
    "test_caching.py",
    "test_kpis.py",
    "test_keys_api.py",
    "test_live_routing.py",
    "test_matrix_live.py",
    "test_streaming.py",
}

PG_EXCLUDED = {
    ("test_kpis.py", "test_endpoint_registered_in_app"),
    ("test_live_routing.py", "test_routing_off_is_legacy_passthrough"),
    ("test_live_routing.py", "test_non_routed_cache_status_defaults_miss"),
    ("test_streaming.py", "test_stream_malformed_line_does_not_crash"),
}

# These paths historically inherited TOKEN_SAVER_PG_DSN even though their
# assertions use an isolated SQLite ledger. Keep them in the floor so a
# fixture edit cannot silently remove production-ledger coverage.
PG_EXTRA = {
    ("test_l1_pipeline.py", "test_ledger_records_l1_tokens"),
    ("test_proxy.py", "test_stats_roundtrip"),
    ("test_proxy.py", "test_stats_endpoint"),
    ("test_regression.py", "test_models_request_is_logged"),
    ("test_regression.py", "test_stats_html_first_run_flip"),
    ("test_regression.py", "test_stats_html_has_tabs_and_chips"),
    ("test_regression.py", "test_stats_by_model_breakdown"),
    ("test_regression.py", "test_original_suite_still_passes"),
}


def test_postgres_sensitive_test_floor():
    """The acceptance matrix must retain at least the current 77 test items."""
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "test"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    count = 0
    for line in result.stdout.splitlines():
        match = re.match(r"(test/[^:]+)::([^\[]+)", line.strip())
        if not match:
            continue
        filename = Path(match.group(1)).name
        name = match.group(2)
        if filename in PG_MODULES and (filename, name) not in PG_EXCLUDED:
            count += 1
        elif (filename, name) in PG_EXTRA:
            count += 1
    assert count >= 77, (
        f"Postgres-sensitive test inventory dropped to {count} (minimum 77)"
    )
