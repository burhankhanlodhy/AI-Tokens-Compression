"""AC-P6f cutover migration tests."""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pg_test_support import drop_database, make_database, unique_db_name  # noqa: E402
from scripts import backfill_measurement_tag as migration  # noqa: E402


def test_cutover_pins_upper_id_and_preserves_existing_tags(monkeypatch):
    name = unique_db_name("ts_ac_p6f_tag_cutover")
    dsn = make_database(name)
    monkeypatch.setenv("TOKEN_SAVER_PG_DSN", dsn)
    try:
        with psycopg.connect(dsn) as pg:
            pg.execute("ALTER TABLE requests DROP COLUMN measurement_tag")
            pg.execute(
                """
                INSERT INTO requests
                    (tenant_id, provider_id, model, route,
                     input_tokens_before, input_tokens_after, status)
                SELECT '00000000-0000-0000-0000-000000000000', id,
                       'test/model', 'passthrough', 1, 1, 200
                  FROM providers WHERE name = 'legacy'
                """
            )
            pg.execute(
                """
                INSERT INTO requests
                    (tenant_id, provider_id, model, route,
                     input_tokens_before, input_tokens_after, status)
                SELECT '00000000-0000-0000-0000-000000000000', id,
                       'test/model', 'passthrough', 1, 1, 200
                  FROM providers WHERE name = 'legacy'
                """
            )
            pg.execute(
                "ALTER TABLE requests ADD COLUMN measurement_tag TEXT"
            )
            pg.execute(
                "UPDATE requests SET measurement_tag = 'already_attributed' "
                "WHERE id = 1"
            )

        result = migration.run_backfill(dsn, tag="pre_p6_6_backfill")
        assert result == {
            "status": "ok",
            "tag": "pre_p6_6_backfill",
            "upper_id": 2,
            "updated": 1,
            "tagged_through": 1,
        }
        again = migration.run_backfill(dsn, tag="pre_p6_6_backfill")
        assert again["updated"] == 0
        assert again["upper_id"] == 2

        with psycopg.connect(dsn) as pg:
            rows = pg.execute(
                "SELECT id, measurement_tag FROM requests ORDER BY id"
            ).fetchall()
        assert rows == [
            (1, "already_attributed"),
            (2, "pre_p6_6_backfill"),
        ]
    finally:
        drop_database(name)