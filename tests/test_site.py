from __future__ import annotations

import unittest

import duckdb

from replayos.site import (
    build_replay_eval,
    build_win_edge,
    get_library_replay,
    library_replay_page,
    list_series,
    list_library_replays,
    refresh_replay_review_cache,
    replay_review_status,
    team_elo_index,
)


class SiteTests(unittest.TestCase):
    def test_build_win_edge_swings_on_goal(self) -> None:
        edge = build_win_edge(
            [
                {"t": 10.0, "event_type": "touch", "team_color": "blue", "value": 1.0},
                {"t": 20.0, "event_type": "goal", "team_color": "blue", "value": 1.0},
                {"t": 40.0, "event_type": "goal", "team_color": "orange", "value": 1.0},
            ],
            60.0,
            [{"prediction_type": "blue_win_probability", "probability": 0.5}],
            segments=6,
        )
        probabilities = [bucket["blue_probability"] for bucket in edge["segments"]]
        self.assertGreater(probabilities[1], probabilities[0])
        self.assertGreater(probabilities[2], 0.5)
        self.assertLess(probabilities[-1], probabilities[2])

    def test_team_elo_index_orders_by_elo(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    match_date TIMESTAMP,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT
                )
                """
            )
            con.executemany(
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("r1", "2026-01-01 10:00:00", "Alpha", 3, "Bravo", 1),
                    ("r2", "2026-01-02 10:00:00", "Alpha", 2, "Charlie", 1),
                    ("r3", "2026-01-03 10:00:00", "Bravo", 4, "Charlie", 0),
                ],
            )
            ladder = team_elo_index(con, limit=3)
            self.assertEqual(ladder[0]["team_name"], "Alpha")
            self.assertEqual(ladder[0]["wins"], 2)
            self.assertGreater(ladder[0]["dominance_score"], 0.0)
            self.assertGreaterEqual(ladder[0]["power_score"], 1500.0)
        finally:
            con.close()

    def test_team_elo_index_uses_standings_without_match_rows(self) -> None:
        con = duckdb.connect(":memory:")
        try:
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
                "INSERT INTO live_leaderboards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("2026:major-2:na", "MAJOR-2 NA", "major-2", "NA", 1, "NRG", 38, "[]", "https://blast.tv/x", "2026-05-21 10:00:00"),
                    ("2026:major-2:eu", "MAJOR-2 EU", "major-2", "EU", 1, "Vitality", 40, "[]", "https://blast.tv/y", "2026-05-21 10:00:00"),
                ],
            )
            ladder = team_elo_index(con, limit=4)
            self.assertEqual(ladder[0]["team_name"], "Vitality")
            self.assertEqual(ladder[0]["standings_points"], 40)
            self.assertEqual(ladder[0]["games"], 0)
            self.assertGreater(ladder[0]["standings_score"], 0.0)
        finally:
            con.close()

    def test_team_elo_index_merges_aliases_into_canonical_team(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    match_date TIMESTAMP,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT
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
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("r1", "2026-01-01 10:00:00", "The General NRG", 3, "Bravo", 1),
                    ("r2", "2026-01-02 10:00:00", "Bravo", 0, "NRG", 2),
                ],
            )
            con.execute(
                "INSERT INTO live_leaderboards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["2026:major-2:na", "MAJOR-2 NA", "major-2", "NA", 1, "NRG", 38, "[]", "https://blast.tv/x", "2026-05-21 10:00:00"],
            )
            ladder = team_elo_index(con, limit=4)
            self.assertEqual(ladder[0]["team_name"], "NRG")
            self.assertEqual(ladder[0]["games"], 2)
            self.assertEqual(ladder[0]["standings_points"], 38)
            self.assertGreater(ladder[0]["standings_score"], 0.0)
        finally:
            con.close()

    def test_build_replay_eval_flags_turnover_as_blunder(self) -> None:
        replay_eval = build_replay_eval(
            [
                {"event_id": 1, "t": 12.0, "event_type": "turnover", "team_color": "orange", "other_team_color": "blue", "other_player_name": "CJCJ", "player_name": "Zez0nix", "value": 1.0},
                {"event_id": 2, "t": 18.0, "event_type": "goal", "team_color": "orange", "player_name": "Zez0nix", "value": 1.0},
            ],
            [{"prediction_type": "blue_win_probability", "probability": 0.5}],
        )
        self.assertTrue(replay_eval["blunders"])
        self.assertEqual(replay_eval["blunders"][0]["player_name"], "CJCJ")
        self.assertLess(replay_eval["blunders"][0]["impact"], 0.0)
        self.assertLess(replay_eval["blunders"][0]["probability_swing"], 0.0)
        self.assertLess(replay_eval["blunders"][0]["swing_points"], 0.0)
        self.assertTrue(replay_eval["plays"])
        self.assertGreater(replay_eval["volatility_points"], 0.0)
        self.assertEqual(replay_eval["swing_count"], 2)
        player_rows = {row["player_name"]: row for row in replay_eval["player_net"]}
        self.assertGreater(player_rows["Zez0nix"]["created_advantage_points"], 0.0)
        self.assertGreater(player_rows["CJCJ"]["lost_advantage_points"], 0.0)
        self.assertGreater(player_rows["Zez0nix"]["involvement_points"], 0.0)

    def test_replay_review_cache_precomputes_and_attaches_to_library_rows(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE replay_parsed_status (
                    replay_id VARCHAR PRIMARY KEY,
                    local_file_path VARCHAR,
                    parser_name VARCHAR,
                    parser_version VARCHAR,
                    source_hz BIGINT,
                    target_hz BIGINT,
                    frame_count BIGINT,
                    duration_seconds DOUBLE,
                    blue_team_name VARCHAR,
                    orange_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    file_size BIGINT,
                    file_mtime DOUBLE,
                    parsed_at TIMESTAMP,
                    parse_seconds DOUBLE,
                    status VARCHAR,
                    error VARCHAR,
                    last_accessed_at TIMESTAMP
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
                CREATE TABLE replays (
                    replay_id VARCHAR PRIMARY KEY,
                    ingested_at TIMESTAMP,
                    game_duration DOUBLE,
                    has_semantic_features BOOLEAN
                )
                """
            )
            con.execute(
                """
                CREATE TABLE matches (
                    replay_id VARCHAR PRIMARY KEY,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    winner_team_id VARCHAR
                )
                """
            )
            con.execute(
                """
                INSERT INTO replay_parsed_status VALUES (
                    'r1', NULL, 'carball', 'v1', 30, 60, 1200, 300.0,
                    'Alpha', 'Bravo', 2, 1, 10, 11.0, '2026-05-21 10:00:00', 1.2, 'completed', NULL, '2026-05-21 10:00:00'
                )
                """
            )
            con.execute("INSERT INTO replays VALUES ('r1', '2026-05-21 10:00:00', 300.0, true)")
            con.execute("INSERT INTO matches VALUES ('r1', 2, 1, 'r1:blue')")
            con.executemany(
                "INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("r1", 1, 12.0, "turnover", "orange", "r1:orange", "o1", "Beta", "blue", "r1:blue", "b1", "Alpha", 1.0, "{}"),
                    ("r1", 2, 18.0, "goal", "blue", "r1:blue", "b1", "Alpha", "orange", "r1:orange", None, None, 1.0, "{}"),
                    ("r1", 3, 240.0, "pressure_phase", "blue", "r1:blue", "b2", "Aero", "orange", "r1:orange", None, None, 1.0, "{}"),
                ],
            )

            result = refresh_replay_review_cache(con, limit=10)
            self.assertEqual(result["computed"], 1)
            status = replay_review_status(con)
            self.assertEqual(status["cached_replays"], 1)

            rows = list_library_replays(con, limit=5)
            self.assertEqual(rows[0]["review"]["largest_blunder"]["player_name"], "Alpha")
            self.assertIn("impact_score_points", rows[0]["review"]["impact_leader"])
            self.assertGreater(rows[0]["review"]["volatility"], 0.0)
            self.assertGreaterEqual(rows[0]["review"]["swing_count"], 2)
            filtered = list_library_replays(con, limit=5, search="Alpha", parsed_only=True, review_ready=True)
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["replay_id"], "r1")
            self.assertFalse(list_library_replays(con, limit=5, search="Nope", parsed_only=True, review_ready=True))
        finally:
            con.close()

    def test_replay_review_cache_prunes_against_full_eligible_set(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE replay_parsed_status (
                    replay_id VARCHAR PRIMARY KEY,
                    local_file_path VARCHAR,
                    parser_name VARCHAR,
                    parser_version VARCHAR,
                    source_hz BIGINT,
                    target_hz BIGINT,
                    frame_count BIGINT,
                    duration_seconds DOUBLE,
                    blue_team_name VARCHAR,
                    orange_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    file_size BIGINT,
                    file_mtime DOUBLE,
                    parsed_at TIMESTAMP,
                    parse_seconds DOUBLE,
                    status VARCHAR,
                    error VARCHAR,
                    last_accessed_at TIMESTAMP
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
                CREATE TABLE replays (
                    replay_id VARCHAR PRIMARY KEY,
                    ingested_at TIMESTAMP,
                    game_duration DOUBLE,
                    has_semantic_features BOOLEAN
                )
                """
            )
            con.execute(
                """
                CREATE TABLE matches (
                    replay_id VARCHAR PRIMARY KEY,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    winner_team_id VARCHAR
                )
                """
            )
            con.executemany(
                """
                INSERT INTO replay_parsed_status VALUES (
                    ?, NULL, 'carball', 'v1', 30, 60, 1200, 300.0,
                    'Alpha', 'Bravo', 2, 1, 10, 11.0, ?, 1.2, 'completed', NULL, ?
                )
                """,
                [
                    ("r1", "2026-05-21 10:00:00", "2026-05-21 10:00:00"),
                    ("r2", "2026-05-21 10:05:00", "2026-05-21 10:05:00"),
                ],
            )
            con.executemany(
                "INSERT INTO replays VALUES (?, ?, 300.0, true)",
                [
                    ("r1", "2026-05-21 10:00:00"),
                    ("r2", "2026-05-21 10:05:00"),
                ],
            )
            con.executemany(
                "INSERT INTO matches VALUES (?, 2, 1, 'blue')",
                [("r1",), ("r2",)],
            )
            con.executemany(
                "INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("r1", 1, 18.0, "goal", "blue", "r1:blue", "b1", "Alpha", "orange", "r1:orange", None, None, 1.0, "{}"),
                    ("r2", 1, 22.0, "goal", "orange", "r2:orange", "o1", "Bravo", "blue", "r2:blue", None, None, 1.0, "{}"),
                ],
            )

            first = refresh_replay_review_cache(con, limit=10)
            self.assertEqual(first["computed"], 2)
            self.assertEqual(replay_review_status(con)["cached_replays"], 2)

            second = refresh_replay_review_cache(con, limit=1)
            self.assertEqual(second["processed"], 1)
            self.assertEqual(replay_review_status(con)["cached_replays"], 2)
        finally:
            con.close()

    def test_get_library_replay_returns_none_without_local_replays_table(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    title VARCHAR,
                    created_at TIMESTAMP,
                    match_date TIMESTAMP,
                    playlist_id VARCHAR,
                    duration DOUBLE,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT,
                    local_file_path VARCHAR,
                    downloaded_at TIMESTAMP,
                    group_ids_json VARCHAR,
                    group_names_json VARCHAR,
                    raw_json VARCHAR
                )
                """
            )
            self.assertIsNone(get_library_replay(con, "missing-replay"))
        finally:
            con.close()

    def test_library_replays_merges_remote_and_local_corpus(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    title VARCHAR,
                    match_date TIMESTAMP,
                    created_at TIMESTAMP,
                    duration DOUBLE,
                    playlist_id VARCHAR,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT,
                    local_file_path VARCHAR,
                    downloaded_at TIMESTAMP
                )
                """
            )
            con.execute(
                """
                CREATE TABLE replays (
                    replay_id VARCHAR PRIMARY KEY,
                    ingested_at TIMESTAMP,
                    game_duration DOUBLE,
                    has_semantic_features BOOLEAN
                )
                """
            )
            con.execute(
                """
                CREATE TABLE matches (
                    replay_id VARCHAR PRIMARY KEY,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    winner_team_id VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE replay_parsed_status (
                    replay_id VARCHAR PRIMARY KEY,
                    local_file_path VARCHAR,
                    parser_name VARCHAR,
                    parser_version VARCHAR,
                    source_hz BIGINT,
                    target_hz BIGINT,
                    frame_count BIGINT,
                    duration_seconds DOUBLE,
                    blue_team_name VARCHAR,
                    orange_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    file_size BIGINT,
                    file_mtime DOUBLE,
                    parsed_at TIMESTAMP,
                    parse_seconds DOUBLE,
                    status VARCHAR,
                    error VARCHAR,
                    last_accessed_at TIMESTAMP
                )
                """
            )
            con.execute(
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "remote-1",
                    "Remote Match",
                    "2026-05-21 12:00:00",
                    "2026-05-21 12:00:00",
                    315.0,
                    "ballchasing",
                    "Alpha",
                    3,
                    "Bravo",
                    2,
                    None,
                    "2026-05-21 12:30:00",
                ],
            )
            con.executemany(
                "INSERT INTO replays VALUES (?, ?, ?, ?)",
                [
                    ("remote-1", "2026-05-21 12:00:00", 315.0, True),
                    ("local-1", "2026-05-20 12:00:00", 298.0, True),
                ],
            )
            con.executemany(
                "INSERT INTO matches VALUES (?, ?, ?, ?)",
                [
                    ("remote-1", 3, 2, "remote-1:blue"),
                    ("local-1", 2, 1, "local-1:orange"),
                ],
            )
            con.execute(
                """
                INSERT INTO replay_parsed_status VALUES (
                    'local-1', NULL, 'carball', 'v2', 30, 60, 1800, 298.0,
                    'Local Blue', 'Local Orange', 2, 1, 10, 11.0, '2026-05-20 12:00:00', 1.2, 'completed', NULL, '2026-05-20 12:10:00'
                )
                """
            )

            rows = list_library_replays(con, limit=10)
            replay_ids = [row["replay_id"] for row in rows]
            self.assertIn("remote-1", replay_ids)
            self.assertIn("local-1", replay_ids)
            local_row = next(row for row in rows if row["replay_id"] == "local-1")
            self.assertEqual(local_row["blue_team_name"], "Local Blue")
            self.assertEqual(local_row["orange_team_name"], "Local Orange")
        finally:
            con.close()

    def test_library_replays_and_detail_fill_remote_scores_from_parse_or_raw(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    title VARCHAR,
                    match_date TIMESTAMP,
                    created_at TIMESTAMP,
                    duration DOUBLE,
                    playlist_id VARCHAR,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT,
                    local_file_path VARCHAR,
                    downloaded_at TIMESTAMP,
                    raw_json VARCHAR,
                    group_ids_json VARCHAR,
                    group_names_json VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE replay_parsed_status (
                    replay_id VARCHAR PRIMARY KEY,
                    local_file_path VARCHAR,
                    parser_name VARCHAR,
                    parser_version VARCHAR,
                    source_hz BIGINT,
                    target_hz BIGINT,
                    frame_count BIGINT,
                    duration_seconds DOUBLE,
                    blue_team_name VARCHAR,
                    orange_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_goals BIGINT,
                    file_size BIGINT,
                    file_mtime DOUBLE,
                    parsed_at TIMESTAMP,
                    parse_seconds DOUBLE,
                    status VARCHAR,
                    error VARCHAR,
                    last_accessed_at TIMESTAMP
                )
                """
            )
            con.execute(
                """
                INSERT INTO remote_replays VALUES (
                    'remote-parse', 'Remote Parse Match', '2026-05-21 12:00:00', '2026-05-21 12:00:00',
                    315.0, 'ballchasing', 'Alpha', NULL, 'Bravo', NULL, NULL, '2026-05-21 12:30:00',
                    '{"blue":{"stats":{"core":{"goals":3}}},"orange":{"stats":{"core":{"goals":1}}}}',
                    '[]', '[]'
                )
                """
            )
            con.execute(
                """
                INSERT INTO replay_parsed_status VALUES (
                    'remote-parse', 'D:\\replays\\remote-parse.replay', 'carball', 'v2', 30, 60, 1800, 315.0,
                    'Alpha', 'Bravo', 4, 2, 10, 11.0, '2026-05-21 12:00:00', 1.2, 'completed', NULL, '2026-05-21 12:10:00'
                )
                """
            )
            con.execute(
                """
                INSERT INTO remote_replays VALUES (
                    'remote-raw', 'Remote Raw Match', '2026-05-21 13:00:00', '2026-05-21 13:00:00',
                    301.0, 'ballchasing', 'Gamma', NULL, 'Delta', NULL, NULL, '2026-05-21 13:30:00',
                    '{"blue":{"stats":{"core":{"goals":2}}},"orange":{"stats":{"core":{"goals":0}}}}',
                    '[]', '[]'
                )
                """
            )

            rows = list_library_replays(con, limit=10)
            parsed_row = next(row for row in rows if row["replay_id"] == "remote-parse")
            raw_row = next(row for row in rows if row["replay_id"] == "remote-raw")
            self.assertEqual((parsed_row["blue_goals"], parsed_row["orange_goals"]), (4, 2))
            self.assertEqual((raw_row["blue_goals"], raw_row["orange_goals"]), (2, 0))

            detail = get_library_replay(con, "remote-raw")
            assert detail is not None
            self.assertEqual((detail["blue_goals"], detail["orange_goals"]), (2, 0))
        finally:
            con.close()

    def test_library_replay_page_can_sort_by_series_order(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    title VARCHAR,
                    match_date TIMESTAMP,
                    created_at TIMESTAMP,
                    duration DOUBLE,
                    playlist_id VARCHAR,
                    blue_team_name VARCHAR,
                    blue_goals BIGINT,
                    orange_team_name VARCHAR,
                    orange_goals BIGINT,
                    local_file_path VARCHAR,
                    downloaded_at TIMESTAMP
                )
                """
            )
            con.execute(
                """
                CREATE TABLE remote_replay_groups (
                    replay_id VARCHAR,
                    group_id VARCHAR,
                    group_name VARCHAR
                )
                """
            )
            con.executemany(
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("r2", "Series A Game 2", "2026-05-21 12:10:00", "2026-05-21 12:10:00", 310.0, "ballchasing", "Alpha", 3, "Bravo", 1, None, "2026-05-21 12:30:00"),
                    ("r1", "Series A Game 1", "2026-05-21 12:00:00", "2026-05-21 12:00:00", 300.0, "ballchasing", "Alpha", 2, "Bravo", 1, None, "2026-05-21 12:20:00"),
                    ("r3", "Series B Game 1", "2026-05-22 12:00:00", "2026-05-22 12:00:00", 305.0, "ballchasing", "Charlie", 4, "Delta", 2, None, "2026-05-22 12:20:00"),
                ],
            )
            con.executemany(
                "INSERT INTO remote_replay_groups VALUES (?, ?, ?)",
                [
                    ("r1", "series-a", "Series A"),
                    ("r2", "series-a", "Series A"),
                    ("r3", "series-b", "Series B"),
                ],
            )

            page = library_replay_page(con, limit=10, sort_mode="series")
            self.assertEqual([row["replay_id"] for row in page["items"]], ["r3", "r1", "r2"])
            self.assertEqual(page["sort_mode"], "series")
            self.assertEqual(page["items"][1]["series_name"], "Series A")
            self.assertEqual(page["items"][1]["series_replay_index"], 1)
            self.assertEqual(page["items"][2]["series_replay_index"], 2)
        finally:
            con.close()

    def test_list_series_prefers_rounds_and_real_series_over_single_game_leaves(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE remote_groups (
                    group_id VARCHAR PRIMARY KEY,
                    name VARCHAR,
                    created_at TIMESTAMP,
                    status VARCHAR,
                    direct_replays BIGINT,
                    indirect_replays BIGINT
                )
                """
            )
            con.execute(
                """
                CREATE TABLE remote_replay_groups (
                    replay_id VARCHAR,
                    group_id VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE remote_replays (
                    replay_id VARCHAR,
                    match_date TIMESTAMP,
                    blue_team_name VARCHAR,
                    orange_team_name VARCHAR
                )
                """
            )
            con.executemany(
                "INSERT INTO remote_groups VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("day-4", "Day 4", "2026-05-21 10:00:00", "ok", None, None),
                    ("alpha-series", "Alpha vs Bravo", "2026-05-21 10:05:00", "ok", None, None),
                    ("leaf-one", "VIT vs GK", "2026-05-21 10:10:00", "ok", None, None),
                    ("noise", "nghjghkgm", "2026-05-21 10:15:00", "ok", None, None),
                ],
            )
            con.executemany(
                "INSERT INTO remote_replay_groups VALUES (?, ?)",
                [
                    ("r1", "day-4"),
                    ("r2", "day-4"),
                    ("r3", "alpha-series"),
                    ("r4", "alpha-series"),
                    ("r5", "leaf-one"),
                ],
            )
            con.executemany(
                "INSERT INTO remote_replays VALUES (?, ?, ?, ?)",
                [
                    ("r1", "2026-05-21 12:00:00", "Alpha", "Bravo"),
                    ("r2", "2026-05-21 12:10:00", "Charlie", "Delta"),
                    ("r3", "2026-05-22 12:00:00", "Alpha", "Bravo"),
                    ("r4", "2026-05-22 12:10:00", "Bravo", "Alpha"),
                    ("r5", "2026-05-23 12:00:00", "Vitality", "GEEKAY ESPORTS"),
                ],
            )

            items = list_series(con, limit=10)

            self.assertEqual([item["group_id"] for item in items], ["alpha-series", "day-4"])
            self.assertEqual(items[0]["kind"], "series")
            self.assertEqual(items[0]["replay_count"], 2)
            self.assertEqual(items[1]["kind"], "round")
            self.assertEqual(items[1]["matchup_count"], 2)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
