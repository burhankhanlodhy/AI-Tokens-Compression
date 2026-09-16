"""Database configuration shared by Postgres-backed proxy components."""
from __future__ import annotations

import os


DSN_ENV_VAR = "TOKEN_SAVER_PG_DSN"


def get_pg_dsn() -> str:
    """Return the configured Postgres DSN, refusing unsafe implicit defaults.

    A missing DSN is a configuration error rather than a reason to guess a
    local credential.  Callers that treat Postgres as optional should catch
    ``RuntimeError`` at their boundary and apply their own fallback policy.
    """
    dsn = os.environ.get(DSN_ENV_VAR)
    if not dsn:
        raise RuntimeError(f"{DSN_ENV_VAR} must be set")
    return dsn
