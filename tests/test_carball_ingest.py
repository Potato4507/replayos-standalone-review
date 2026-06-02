from __future__ import annotations

import json
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb
import numpy as np
import pandas as pd

from replayos.carball_ingest import (
    PARSER_VERSION,
    ReplayParseError,
    _bool_series,
    _build_frame_payload,
    _build_events,
    _clean_entity_name,
    _normalized_time_axis,
    _player_map,
    _run_carball_analysis,
    _target_times,
    ensure_carball_schema,
    ensure_replay_analysis,
    load_parsed_replay_frames,
    repair_stale_running_parses,
    refresh_local_replay_index,
    replay_name_coverage,
    select_backfill_candidates,
)


class CarballIngestTests(unittest.TestCase):
    def test_clean_entity_name_repairs_mojibake(self) -> None:
        self.assertEqual(_clean_entity_name("Declan.ã"), "Declan.ツ")

    def test_target_times_include_tail_frame(self) -> None:
        target = _target_times(1.05, 60)
        self.assertGreaterEqual(target[-1], 1.05)
        self.assertAlmostEqual(target[1] - target[0], 1 / 60, places=6)

    def test_normalized_time_axis_recovers_wrapped_clock(self) -> None:
        frame = pd.DataFrame(
            {
                ("game", "time"): [19.7, 19.7333, 19.7666, 4.2, 4.2333, 4.2666],
            }
        )
        normalized = _normalized_time_axis(frame)
        self.assertGreater(normalized["duration_seconds"], 0.1)
        self.assertGreater(normalized["times"][-1], normalized["times"][0])
        self.assertAlmostEqual(normalized["times"][1] - normalized["times"][0], normalized["times"][2] - normalized["times"][1], places=4)

    def test_build_events_derives_turnover_goal_and_kickoff(self) -> None:
        players = {
            "p1": {"player_id": "p1", "player_name": "Alpha", "team": 0},
            "p2": {"player_id": "p2", "player_name": "Bravo", "team": 1},
        }
        proto = {
            "game_metadata": {"length": 30.0},
            "game_stats": {
                "hits": [
                    {"frame_number": 1, "player_id": {"id": "p1"}, "is_kickoff": True, "distance_to_goal": 5000},
                    {"frame_number": 3, "player_id": {"id": "p2"}, "distance_to_goal": 2100, "shot": True, "goal": True},
                ]
            },
        }
        events = _build_events("r1", proto, players, {0: "Blue", 1: "Orange"}, np.array([0.0, 0.1, 0.2, 0.3]))
        event_types = [event["event_type"] for event in events]
        self.assertIn("kickoff_outcome", event_types)
        self.assertIn("turnover", event_types)
        self.assertIn("goal", event_types)

    def test_build_frame_payload_reconstructs_boost_from_pad_pickups(self) -> None:
        frame = pd.DataFrame(
            {
                ("game", "time"): [0.0, 0.5, 1.0],
                ("ball", "pos_x"): [0.0, 0.0, 0.0],
                ("ball", "pos_y"): [0.0, 0.0, 0.0],
                ("ball", "pos_z"): [92.75, 92.75, 92.75],
                ("ball", "vel_x"): [0.0, 0.0, 0.0],
                ("ball", "vel_y"): [0.0, 0.0, 0.0],
                ("ball", "vel_z"): [0.0, 0.0, 0.0],
                ("Alpha", "pos_x"): [3072.0, 3072.0, 3072.0],
                ("Alpha", "pos_y"): [4096.0, 4096.0, 4096.0],
                ("Alpha", "pos_z"): [17.0, 17.0, 17.0],
                ("Alpha", "vel_x"): [0.0, 0.0, 0.0],
                ("Alpha", "vel_y"): [0.0, 0.0, 0.0],
                ("Alpha", "vel_z"): [0.0, 0.0, 0.0],
                ("Alpha", "rot_x"): [0.0, 0.0, 0.0],
                ("Alpha", "rot_y"): [0.0, 0.0, 0.0],
                ("Alpha", "rot_z"): [0.0, 0.0, 0.0],
                ("Alpha", "boost"): [0.0, 0.0, 0.0],
                ("Alpha", "boost_active"): [False, False, True],
                ("Alpha", "jump_active"): [False, False, False],
                ("Alpha", "dodge_active"): [False, False, False],
            }
        )
        players = {
            "alpha": {
                "column_key": "Alpha",
                "player_id": "alpha",
                "player_name": "Alpha",
                "team": 0,
                "team_name": "Blue",
                "car_name": "Octane",
                "car_family": "octane",
                "car_body_id": 23,
            }
        }

        payload = _build_frame_payload(
            "r-boost",
            frame,
            players,
            {0: "Blue"},
            np.array([0.0, 0.5, 1.0]),
            np.array([0.0, 0.5, 1.0]),
            2,
            2,
        )

        boosts = [row["cars"][0]["boost"] for row in payload["frames"]]
        self.assertEqual(boosts[0], 100.0)
        self.assertEqual(payload["frames"][0]["pad_states"][28], False)
        self.assertLess(boosts[-1], 100.0)

    def test_bool_series_coerces_mixed_bool_like_values(self) -> None:
        frame = pd.DataFrame(
            {
                ("Alpha", "jump_active"): [True, False, 1, 0, "true", "FALSE", "yes", "off", None, float("nan"), "2"],
            }
        )

        values = _bool_series(frame, ("Alpha", "jump_active"))

        self.assertEqual(
            values.tolist(),
            [True, False, True, False, True, False, True, False, False, False, True],
        )

    def test_load_parsed_replay_frames_uses_stale_cache_when_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            replay_path = Path(tmpdir) / "sample.replay"
            replay_path.write_bytes(b"still-good")
            serving_db = Path(tmpdir) / "serving.duckdb"
            payload = {
                "payload_version": 1,
                "frame_count": 1,
                "base_hz": 60,
                "players": [
                    {
                        "player_id": "p1",
                        "player_name": "Alpha",
                        "team": 0,
                    }
                ],
                "frames": [
                    {
                        "t": 0.0,
                        "ball": {"pos": [0.0, 0.0, 92.75]},
                        "cars": [
                            {
                                "player_id": "p1",
                                "pos": [0.0, 0.0, 17.0],
                                "euler": [0.0, 0.0, 0.0],
                                "boost": 50.0,
                            }
                        ],
                    }
                ],
            }
            con = duckdb.connect(str(serving_db))
            try:
                ensure_carball_schema(con)
                con.execute(
                    "INSERT INTO replay_parsed_frames VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        "r-stale",
                        zlib.compress(json.dumps(payload).encode("utf-8"), level=6),
                        json.dumps(payload["players"]),
                        json.dumps({}),
                        json.dumps({"event_count": 0, "duration_seconds": 0.0, "source_hz": 30, "target_hz": 60}),
                        datetime.now(timezone.utc),
                    ],
                )
                con.execute(
                    """
                    INSERT INTO replay_parsed_status (
                        replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                        frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                        file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "r-stale",
                        str(replay_path),
                        "carball",
                        "old-parser",
                        30,
                        60,
                        1,
                        1.0,
                        "Alpha",
                        "Bravo",
                        1,
                        0,
                        replay_path.stat().st_size,
                        float(replay_path.stat().st_mtime),
                        datetime.now(timezone.utc),
                        1.0,
                        "completed",
                        None,
                        datetime.now(timezone.utc),
                    ],
                )
            finally:
                con.close()

            with patch("replayos.carball_ingest.ensure_replay_analysis", side_effect=ReplayParseError("boom")):
                loaded = load_parsed_replay_frames(
                    "r-stale",
                    hz=60,
                    max_frames=10,
                    start_frame=0,
                    serving_db=serving_db,
                    local_file_path=replay_path,
                )

            self.assertEqual(loaded["frame_count"], 1)
            self.assertTrue(loaded["cache_stale"])
            self.assertEqual(loaded["cache_warning"], "boom")
            self.assertEqual(loaded["players"][0]["player_name"], "Alpha")

    def test_failed_parse_uses_retry_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            replay_path = Path(tmpdir) / "sample.replay"
            replay_path.write_bytes(b"not-a-real-replay")
            serving_db = Path(tmpdir) / "serving.duckdb"
            con = duckdb.connect(str(serving_db))
            try:
                ensure_carball_schema(con)
                con.execute(
                    """
                    INSERT INTO replay_parsed_status (
                        replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                        frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                        file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "r-cooldown",
                        str(replay_path),
                        "carball",
                        PARSER_VERSION,
                        None,
                        60,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        replay_path.stat().st_size,
                        float(replay_path.stat().st_mtime),
                        datetime.now(timezone.utc) - timedelta(minutes=5),
                        None,
                        "failed",
                        "boom",
                        datetime.now(timezone.utc) - timedelta(minutes=5),
                    ],
                )
            finally:
                con.close()

            with patch("replayos.carball_ingest.carball.analyze_replay_file") as mocked:
                with self.assertRaises(ReplayParseError):
                    ensure_replay_analysis(
                        "r-cooldown",
                        local_file_path=replay_path,
                        serving_db=serving_db,
                    )
                mocked.assert_not_called()

    def test_run_carball_analysis_falls_back_to_tolerant_mode(self) -> None:
        replay_path = Path("D:/RocketLeagueFrames/replays/sample.replay")
        fallback_result = object()
        with (
            patch("replayos.carball_ingest.carball.analyze_replay_file", side_effect=RuntimeError("boom")),
            patch("replayos.carball_ingest._run_tolerant_carball_analysis", return_value=fallback_result) as tolerant,
        ):
            result = _run_carball_analysis(replay_path)

        self.assertIs(result, fallback_result)
        tolerant.assert_called_once_with(replay_path)

    def test_refresh_local_replay_index_tracks_orphans_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            serving_db = tmp / "serving.duckdb"
            replay_root = tmp / "replays"
            replay_root.mkdir()
            warehouse_file = replay_root / "warehouse.replay"
            orphan_file = replay_root / "orphan.replay"
            warehouse_file.write_bytes(b"a" * 32)
            orphan_file.write_bytes(b"b" * 48)

            con = duckdb.connect(str(serving_db))
            try:
                con.execute("CREATE TABLE replays (replay_id VARCHAR PRIMARY KEY)")
                con.execute("INSERT INTO replays VALUES ('warehouse')")
            finally:
                con.close()

            index = refresh_local_replay_index(serving_db=serving_db, replay_roots=[replay_root])
            self.assertEqual(index["indexed_replays"], 2)
            self.assertEqual(index["warehouse_replays"], 1)
            self.assertEqual(index["orphan_local_replays"], 1)

            con = duckdb.connect(str(serving_db))
            try:
                candidates = select_backfill_candidates(con, limit=2, force=False)
                self.assertEqual([row["replay_id"] for row in candidates], ["warehouse", "orphan"])
            finally:
                con.close()

    def test_repair_stale_running_parses_marks_old_rows_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            serving_db = Path(tmpdir) / "serving.duckdb"
            con = duckdb.connect(str(serving_db))
            try:
                ensure_carball_schema(con)
                con.execute(
                    """
                    INSERT INTO replay_parsed_status (
                        replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                        frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                        file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "stale-row",
                        "D:/stale.replay",
                        "carball",
                        PARSER_VERSION,
                        None,
                        60,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        10,
                        1.0,
                        None,
                        None,
                        "running",
                        None,
                        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=8),
                    ],
                )
            finally:
                con.close()

            result = repair_stale_running_parses(serving_db=serving_db, stale_after_seconds=3600)
            self.assertEqual(result["repaired"], 1)

            con = duckdb.connect(str(serving_db), read_only=True)
            try:
                row = con.execute(
                    "SELECT status, error FROM replay_parsed_status WHERE replay_id = 'stale-row'"
                ).fetchone()
            finally:
                con.close()

            self.assertEqual(row[0], "failed")
            self.assertIn("repaired automatically", row[1])

    def test_replay_name_coverage_counts_named_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            serving_db = Path(tmpdir) / "serving.duckdb"
            con = duckdb.connect(str(serving_db))
            try:
                ensure_carball_schema(con)
                con.execute("CREATE TABLE replays (replay_id VARCHAR PRIMARY KEY)")
                con.executemany("INSERT INTO replays VALUES (?)", [("r1",), ("r2",)])
                con.executemany(
                    """
                    INSERT INTO local_replay_index VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("r1", "D:/r1.replay", 10, 1.0, "2026-05-21 10:00:00", "2026-05-21 10:00:00", True),
                        ("r2", "D:/r2.replay", 10, 1.0, "2026-05-21 10:00:00", "2026-05-21 10:00:00", True),
                        ("r3", "D:/r3.replay", 10, 1.0, "2026-05-21 10:00:00", "2026-05-21 10:00:00", False),
                    ],
                )
                con.executemany(
                    """
                    INSERT INTO replay_parsed_status (
                        replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                        frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                        file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("r1", "D:/r1.replay", "carball", PARSER_VERSION, 30, 60, 100, 300.0, "Alpha", "Bravo", 3, 1, 10, 1.0, "2026-05-21 10:05:00", 1.2, "completed", None, "2026-05-21 10:05:00"),
                        ("r2", "D:/r2.replay", "carball", PARSER_VERSION, 30, 60, 100, 300.0, "Blue Side", "Orange Side", 2, 1, 10, 1.0, "2026-05-21 10:10:00", 1.2, "completed", None, "2026-05-21 10:10:00"),
                        ("r3", "D:/r3.replay", "carball", PARSER_VERSION, None, 60, None, None, None, None, None, None, 10, 1.0, "2026-05-21 10:15:00", None, "failed", "boom", "2026-05-21 10:15:00"),
                    ],
                )
                con.executemany(
                    "INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("r1", 1, 5.0, "goal", "blue", "r1:blue", "p1", "Alpha1", None, None, None, None, 1.0, "{}"),
                        ("r2", 1, 6.0, "touch", "orange", "r2:orange", "p2", "Bravo2", None, None, None, None, 1.0, "{}"),
                    ],
                )
                coverage = replay_name_coverage(con)
                self.assertEqual(coverage["indexed_local_replays"], 3)
                self.assertEqual(coverage["named_team_replays"], 1)
                self.assertEqual(coverage["named_player_replays"], 2)
                self.assertEqual(coverage["failed_replays"], 1)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
