from __future__ import annotations

import unittest

import duckdb

from replayos.recordbook import head_to_head, player_record_profile, recordbook_overview, team_record_profile


def seed_recordbook(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE replay_parsed_status (
            replay_id VARCHAR,
            parsed_at TIMESTAMP,
            status VARCHAR,
            blue_team_name VARCHAR,
            orange_team_name VARCHAR,
            blue_goals BIGINT,
            orange_goals BIGINT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE replay_parsed_events (
            replay_id VARCHAR,
            event_id BIGINT,
            t DOUBLE,
            event_type VARCHAR,
            team_color VARCHAR,
            team_id VARCHAR,
            player_id VARCHAR,
            player_name VARCHAR,
            other_team_color VARCHAR,
            other_team_id VARCHAR,
            other_player_id VARCHAR,
            other_player_name VARCHAR,
            value DOUBLE,
            meta VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE live_leaderboards (
            board_key VARCHAR,
            board_name VARCHAR,
            stage_key VARCHAR,
            region VARCHAR,
            rank BIGINT,
            team_name VARCHAR,
            points BIGINT,
            players_json VARCHAR,
            source_url VARCHAR,
            updated_at TIMESTAMP
        )
        """
    )
    con.executemany(
        "INSERT INTO replay_parsed_status VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("r1", "2026-01-01 10:00:00", "completed", "Alpha", "Bravo", 3, 1),
            ("r2", "2026-01-02 10:00:00", "completed", "TEAM ALPHA", "Bravo", 2, 0),
            ("r3", "2026-01-03 10:00:00", "completed", "Charlie", "Alpha", 1, 4),
        ],
    )
    events = [
        ("r1", 1, 10.0, "goal", "blue", "r1:blue", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r1", 2, 11.0, "goal", "blue", "r1:blue", "p2", "Ava", None, None, None, None, 1.0, "{}"),
        ("r1", 3, 12.0, "goal", "blue", "r1:blue", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r1", 4, 13.0, "goal", "orange", "r1:orange", "p3", "Bob", None, None, None, None, 1.0, "{}"),
        ("r1", 5, 14.0, "touch", "blue", "r1:blue", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r1", 6, 15.0, "demo", "blue", "r1:blue", "p2", "Ava", None, None, None, None, 1.0, "{}"),
        ("r1", 7, 16.0, "pressure_phase", "blue", "r1:blue", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r1", 8, 17.0, "turnover", "orange", "r1:orange", "p3", "Bob", "blue", "r1:blue", "p1", "Alice", 1.0, "{}"),
        ("r2", 9, 10.0, "goal", "blue", "r2:blue", "p1", "ALICE", None, None, None, None, 1.0, "{}"),
        ("r2", 10, 11.0, "goal", "blue", "r2:blue", "p2", "Ava", None, None, None, None, 1.0, "{}"),
        ("r2", 11, 12.0, "touch", "blue", "r2:blue", "p1", "ALICE", None, None, None, None, 1.0, "{}"),
        ("r2", 12, 13.0, "touch", "orange", "r2:orange", "p3", "Bob", None, None, None, None, 1.0, "{}"),
        ("r2", 13, 14.0, "kickoff_outcome", "blue", "r2:blue", "p2", "Ava", None, None, None, None, 1.0, "{}"),
        ("r3", 14, 10.0, "goal", "orange", "r3:orange", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r3", 15, 11.0, "goal", "orange", "r3:orange", "p2", "Ava", None, None, None, None, 1.0, "{}"),
        ("r3", 16, 12.0, "goal", "orange", "r3:orange", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r3", 17, 13.0, "goal", "orange", "r3:orange", "p2", "Ava", None, None, None, None, 1.0, "{}"),
        ("r3", 18, 14.0, "goal", "blue", "r3:blue", "p5", "Chris", None, None, None, None, 1.0, "{}"),
        ("r3", 19, 15.0, "boost_starvation_window", "orange", "r3:orange", "p1", "Alice", None, None, None, None, 1.0, "{}"),
        ("r3", 20, 16.0, "touch", "orange", "r3:orange", "p2", "Ava", None, None, None, None, 1.0, "{}"),
    ]
    con.executemany("INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", events)
    con.execute(
        "INSERT INTO live_leaderboards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["board:1", "RLCS NA", "major-2", "NA", 1, "Omelette", 18, "[]", "https://blast.tv/example", "2026-02-02 10:00:00"],
    )


class RecordbookTests(unittest.TestCase):
    def test_recordbook_overview_orders_team_leaders(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            seed_recordbook(con)
            overview = recordbook_overview(con, limit=4)
            self.assertEqual(overview["summary"]["tracked_matches"], 3)
            self.assertEqual(overview["team_leaders"]["most_wins"][0]["name"], "Alpha")
            self.assertIn("Alice", overview["player_options"])
        finally:
            con.close()

    def test_team_record_profile_includes_frequencies_and_streaks(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            seed_recordbook(con)
            profile = team_record_profile(con, "alpha")
            self.assertEqual(profile["team_name"], "Alpha")
            self.assertEqual(profile["record"]["wins"], 3)
            self.assertEqual(profile["record"]["longest_win_streak"], 3)
            frequency_labels = {item["event_type"] for item in profile["frequencies"]}
            self.assertIn("turnovers_committed", frequency_labels)
            self.assertTrue(profile["players"])
        finally:
            con.close()

    def test_player_profiles_and_head_to_head_work(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            seed_recordbook(con)
            profile = player_record_profile(con, "Alice")
            self.assertEqual(profile["record"]["wins"], 3)
            self.assertEqual(profile["teammates"][0]["teammate_name"], "Ava")
            self.assertTrue(profile["tracker_profile_url"].startswith("https://tracker.gg/rocket-league/search?query="))
            matchup = head_to_head(con, kind="player", left_name="Alice", right_name="Bob")
            self.assertEqual(matchup["summary"]["opposed_games"], 2)
            self.assertEqual(matchup["summary"]["left_wins"], 2)
        finally:
            con.close()

    def test_identity_layer_merges_team_aliases(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            seed_recordbook(con)
            con.executemany(
                "INSERT INTO replay_parsed_status VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("r4", "2026-01-04 10:00:00", "completed", "The General NRG", "Bravo", 3, 0),
                    ("r5", "2026-01-05 10:00:00", "completed", "NRG", "Charlie", 2, 1),
                ],
            )
            con.executemany(
                "INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("r4", 21, 10.0, "goal", "blue", "r4:blue", "p9", "GarrettG", None, None, None, None, 1.0, "{}"),
                    ("r5", 22, 10.0, "goal", "blue", "r5:blue", "p10", "Squishy", None, None, None, None, 1.0, "{}"),
                ],
            )
            overview = recordbook_overview(con, limit=8)
            self.assertIn("NRG", overview["team_options"])
            self.assertNotIn("The General NRG", overview["team_options"])
            profile = team_record_profile(con, "nrg")
            self.assertEqual(profile["record"]["wins"], 2)
        finally:
            con.close()

    def test_recordbook_exposes_known_entities_beyond_tracked_history(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            seed_recordbook(con)
            con.executemany(
                "INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("rx", 21, 20.0, "touch", "blue", "rx:blue", "p9", "Aerial☆", None, None, None, None, 1.0, "{}"),
                ],
            )
            overview = recordbook_overview(con, limit=4)
            self.assertIn("Omelette", overview["team_options"])
            self.assertIn("Aerial☆", overview["player_options"])
            self.assertGreaterEqual(overview["summary"]["known_teams"], overview["summary"]["tracked_teams"])
            self.assertGreaterEqual(overview["summary"]["known_players"], overview["summary"]["tracked_players"])

            team_profile = team_record_profile(con, "Omelette")
            self.assertFalse(team_profile["record"]["has_history"])
            self.assertTrue(team_profile["leaderboard_snapshot"])

            player_profile = player_record_profile(con, "Aerial☆")
            self.assertFalse(player_profile["record"]["has_history"])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
