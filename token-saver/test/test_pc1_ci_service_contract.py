"""Regression guard for the pinned pgvector GitHub Actions service contract."""
from __future__ import annotations

import re
from pathlib import Path

import yaml


def _pc1_pgvector_job(workflow: str) -> str:
    match = re.search(r"^  pc1-pgvector:(.*?)(?=^  \S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert match, "pc1-pgvector job is missing from the CI workflow"
    return match.group(1)


def _dsn_password(dsn: str) -> str:
    match = re.match(r"postgresql://[^:]+:([^@]+)@", dsn)
    assert match, "pinned pgvector DSN must include a password"
    return match.group(1)


def test_ci_workflow_parses_as_yaml():
    """The workflow must remain parseable by GitHub Actions' YAML reader."""
    repo_root = Path(__file__).resolve().parents[2]
    workflow = (repo_root / ".github/workflows/ci.yml").read_text()

    document = yaml.safe_load(workflow)

    assert isinstance(document, dict)
    assert "pc1-pgvector" in document["jobs"]


def test_pinned_pgvector_service_password_matches_all_job_connection_dsns():
    """Every PC1/PC2/PC4 database connection must authenticate to its service."""
    repo_root = Path(__file__).resolve().parents[2]
    workflow = (repo_root / ".github/workflows/ci.yml").read_text()
    job = _pc1_pgvector_job(workflow)

    service_password = re.search(r"POSTGRES_PASSWORD:\s*(\S+)", job)
    assert service_password, "pinned pgvector service must define POSTGRES_PASSWORD"

    connection_variables = (
        "TOKEN_SAVER_PG_ADMIN_DSN",
        "TOKEN_SAVER_PG_DSN",
        "TOKEN_SAVER_PG_BASE",
    )
    connection_dsns = []
    for variable in connection_variables:
        match = re.search(rf"{variable}:\s*(\S+)", job)
        assert match, f"pinned pgvector job must define {variable}"
        connection_dsns.append(match.group(1))

    assert all(_dsn_password(dsn) == service_password.group(1) for dsn in connection_dsns)
