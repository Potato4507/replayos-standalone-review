from __future__ import annotations

import unittest
from datetime import datetime, timezone

import duckdb

from replayos.site import team_elo_index


class SiteCacheTests(unittest.TestCase):
    def test_team_elo_cache_refreshes_when_sync_state_changes(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR PRIMARY KEY,
                    created_at TIMESTAMP,
                    match_date TIMESTAMP,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT,
                    synced_at TIMESTAMP
                )
                """
            )
            con.execute(
                """
                CREATE TABLE remote_sync_runs (
                    run_id VARCHAR PRIMARY KEY,
                    source VARCHAR,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    filters_json VARCHAR,
                    result_json VARCHAR,
                    status VARCHAR,
                    error VARCHAR
                )
                """
            )
            now = datetime.now(timezone.utc)
            con.execute(
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["r1", now, now, "Alpha", 3, "Bravo", 1, now],
            )
            con.execute(
                "INSERT INTO remote_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["run-1", "ballchasing", now, now, "{}", "{}", "completed", None],
            )

            ladder_one = team_elo_index(con, limit=10)
            self.assertEqual(ladder_one[0]["team_name"], "Alpha")
            self.assertEqual(ladder_one[0]["games"], 1)

            later = datetime.now(timezone.utc)
            con.execute(
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["r2", later, later, "Alpha", 1, "Bravo", 4, later],
            )
            con.execute(
                "INSERT INTO remote_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["run-2", "ballchasing", later, later, "{}", "{}", "completed", None],
            )

            ladder_two = team_elo_index(con, limit=10)
            self.assertEqual(len(ladder_two), 2)
            self.assertEqual(ladder_two[0]["games"], 2)
            cache_rows = con.execute("SELECT COUNT(*) FROM team_elo_cache").fetchone()[0]
            self.assertEqual(cache_rows, 2)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
