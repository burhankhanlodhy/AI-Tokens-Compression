"""One-time AC-P6f ledger cutover for pre-tag-aware rows.

The benchmark/calibration deployment must not be allowed to manufacture its
own live population.  This migration adds the nullable attribution column to
older Postgres deployments and stamps every row present at one pinned cutover
point.  The table lock makes the upper-bound read and UPDATE atomic with
respect to concurrent ledger INSERTs; rows written after the commit remain
untagged and therefore represent the post-cutover organic population.

The operation is deliberately write-once: existing non-empty tags are never
overwritten, and rerunning the command updates zero rows.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.db import get_pg_dsn  # noqa: E402

BACKFILL_TAG = "pre_p6_6_backfill"


def run_backfill(dsn: str | None = None, *, tag: str = BACKFILL_TAG) -> dict[str, int | str]:
    """Add the column and tag the ledger through one pinned id upper bound."""
    if not tag or not tag.strip():
        raise ValueError("tag must be non-empty")
    resolved_dsn = dsn or get_pg_dsn()
    with psycopg.connect(resolved_dsn) as pg:
        # SHARE ROW EXCLUSIVE conflicts with INSERT's ROW EXCLUSIVE lock.  It
        # therefore freezes the cutover while requests already in flight drain
        # or wait, instead of allowing rows to slip across the pinned bound.
        pg.execute("LOCK TABLE requests IN SHARE ROW EXCLUSIVE MODE")
        pg.execute(
            "ALTER TABLE requests ADD COLUMN IF NOT EXISTS measurement_tag TEXT"
        )
        upper_id = int(
            pg.execute("SELECT COALESCE(MAX(id), 0) FROM requests").fetchone()[0]
        )
        updated = int(
            pg.execute(
                """
                UPDATE requests
                   SET measurement_tag = %s
                 WHERE id <= %s
                   AND (measurement_tag IS NULL OR measurement_tag = '')
                """,
                (tag, upper_id),
            ).rowcount
        )
        tagged_through = int(
            pg.execute(
                """
                SELECT COUNT(*) FROM requests
                 WHERE id <= %s AND measurement_tag = %s
                """,
                (upper_id, tag),
            ).fetchone()[0]
        )
        pg.commit()
    return {
        "status": "ok",
        "tag": tag,
        "upper_id": upper_id,
        "updated": updated,
        "tagged_through": tagged_through,
    }


if __name__ == "__main__":
    try:
        result = run_backfill(os.environ.get("TOKEN_SAVER_PG_DSN"))
    except Exception as exc:  # pragma: no cover - CLI failure surface
        print(f"measurement-tag backfill failed: {exc}", file=sys.stderr)
        raise
    print(result)