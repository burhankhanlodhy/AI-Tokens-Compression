"""Regression guards for the standard CI lane's fresh-Postgres bootstrap."""
from __future__ import annotations

import re
from pathlib import Path


MIGRATIONS = (
    "20260918_pc1_pgvector.sql",
    "20260919_pc2_semantic_responses.sql",
    "20260920_pc5_request_versions.sql",
    "20260922_v121_tool_schema_ledger.sql",
    "20260922_t1_tool_compression_ledger.sql",
    "20260924_v21_session_stores.sql",
    "20260925_v22_runtime_settings.sql",
)

# The canonical fresh schema already has the widened cache-status constraint.
# Applying this fire-once legacy-volume migration after that schema must fail.
CANONICALIZED_MIGRATION = "20260920_ac_pcui_cache_status.sql"


def _test_job(workflow: str) -> str:
    match = re.search(r"^  test:(.*?)(?=^  \S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert match, "test job is missing from the CI workflow"
    return match.group(1)


def test_standard_ci_bootstraps_canonical_schema_and_migration_inventory_before_pytest():
    """Fresh CI Postgres must have every app table before TestClient startup."""
    repo_root = Path(__file__).resolve().parents[2]
    job = _test_job((repo_root / ".github/workflows/ci.yml").read_text())

    assert "image: pgvector/pgvector:0.8.6-pg16-bookworm" in job
    assert "- name: Install PostgreSQL client" in job
    assert "- name: Bootstrap fresh Postgres schema and migrations" in job
    assert "psql \"$TOKEN_SAVER_PG_DSN\" -v ON_ERROR_STOP=1 -f ../postgres-schema-v2.sql" in job
    for migration in MIGRATIONS:
        assert f"migrations/{migration}" in job
    assert f"migrations/{CANONICALIZED_MIGRATION}" not in job
    assert "chk_cache_status" in job
    assert "semantic_threshold_miss" in job
    assert job.count("if executed < 937 or totals['skipped']:") == 2

    bootstrap_offset = job.index("- name: Bootstrap fresh Postgres schema and migrations")
    pytest_offset = job.index("python -m pytest -q")
    assert bootstrap_offset < pytest_offset
