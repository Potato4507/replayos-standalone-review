from __future__ import annotations

import json
import tempfile
import unittest
import zlib
from pathlib import Path

import duckdb

from replayos.frames import load_replay_frames


def _frame_blob(cars: dict[str, dict[str, object]]) -> bytes:
    payload = {
        "ball": {"phys": {"pos": [0.0, 0.0, 0.0], "vel": [0.0, 0.0, 0.0]}},
        "cars": cars,
        "scoreboard": {"blue_score": 0, "orange_score": 0, "is_overtime": False},
    }
    return zlib.compress(json.dumps(payload).encode("utf-8"))


class FrameNamingTests(unittest.TestCase):
    def test_load_replay_frames_uses_event_names_without_players_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_db = root / "raw.duckdb"
            serving_db = root / "serving.duckdb"

            raw_con = duckdb.connect(str(raw_db))
            try:
                raw_con.execute(
                    """
                    CREATE TABLE frames_state (
                        replay_id VARCHAR,
                        bucket BIGINT,
                        t_approx DOUBLE,
                        payload_zlib BLOB
                    )
                    """
                )
                raw_con.execute(
                    "INSERT INTO frames_state VALUES (?, ?, ?, ?)",
                    [
                        "r1",
                        0,
                        0.0,
                        _frame_blob(
                            {
                                "car-1": {
                                    "team": 0,
                                    "boost": 33.0,
                                    "demo": False,
                                    "on_ground": True,
                                    "has_flip": True,
                                    "phys": {"pos": [1.0, 2.0, 17.0], "vel": [0.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                },
                                "car-2": {
                                    "team": 1,
                                    "boost": 55.0,
                                    "demo": False,
                                    "on_ground": True,
                                    "has_flip": True,
                                    "phys": {"pos": [-1.0, -2.0, 17.0], "vel": [0.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                },
                            }
                        ),
                    ],
                )
            finally:
                raw_con.close()

            serving_con = duckdb.connect(str(serving_db))
            try:
                serving_con.execute(
                    """
                    CREATE TABLE events (
                        event_id BIGINT,
                        replay_id VARCHAR,
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
                serving_con.executemany(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (1, "r1", 1.0, "touch", "blue", "r1:blue", "car-1", "Alpha", None, None, None, None, 1.0, "{}"),
                        (2, "r1", 2.0, "touch", "orange", "r1:orange", "car-2", "Beta", None, None, None, None, 1.0, "{}"),
                    ],
                )
            finally:
                serving_con.close()

            payload = load_replay_frames("r1", hz=8, max_frames=10, raw_db=raw_db, serving_db=serving_db)

            names = {row["player_id"]: row["player_name"] for row in payload["players"]}
            self.assertEqual(names["car-1"], "Alpha")
            self.assertEqual(names["car-2"], "Beta")
            self.assertEqual(payload["frames"][0]["cars"][0]["player_name"], "Alpha")
            self.assertEqual(payload["frames"][0]["cars"][1]["player_name"], "Beta")

    def test_load_replay_frames_infers_names_from_ballchasing_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_db = root / "raw.duckdb"
            serving_db = root / "serving.duckdb"

            raw_con = duckdb.connect(str(raw_db))
            try:
                raw_con.execute(
                    """
                    CREATE TABLE frames_state (
                        replay_id VARCHAR,
                        bucket BIGINT,
                        t_approx DOUBLE,
                        payload_zlib BLOB
                    )
                    """
                )
                raw_con.execute(
                    "INSERT INTO frames_state VALUES (?, ?, ?, ?)",
                    [
                        "r2",
                        0,
                        0.0,
                        _frame_blob(
                            {
                                "blue-front": {
                                    "team": 0,
                                    "boost": 60.0,
                                    "demo": False,
                                    "on_ground": True,
                                    "has_flip": True,
                                    "phys": {"pos": [0.0, -500.0, 17.0], "vel": [2000.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                },
                                "blue-back": {
                                    "team": 0,
                                    "boost": 40.0,
                                    "demo": False,
                                    "on_ground": True,
                                    "has_flip": True,
                                    "phys": {"pos": [0.0, -1000.0, 17.0], "vel": [1000.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                },
                                "orange-front": {
                                    "team": 1,
                                    "boost": 55.0,
                                    "demo": False,
                                    "on_ground": True,
                                    "has_flip": True,
                                    "phys": {"pos": [0.0, 500.0, 17.0], "vel": [1800.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                },
                                "orange-back": {
                                    "team": 1,
                                    "boost": 35.0,
                                    "demo": False,
                                    "on_ground": True,
                                    "has_flip": True,
                                    "phys": {"pos": [0.0, 1000.0, 17.0], "vel": [800.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                },
                            }
                        ),
                    ],
                )
            finally:
                raw_con.close()

            detail = {
                "blue": {
                    "players": [
                        {
                            "name": "Alpha",
                            "car_name": "Octane",
                            "stats": {
                                "movement": {"avg_speed": 2000.0, "percent_ground": 100.0},
                                "positioning": {
                                    "percent_behind_ball": 100.0,
                                    "avg_distance_to_ball": 500.0,
                                    "percent_most_back": 0.0,
                                    "percent_most_forward": 100.0,
                                    "percent_closest_to_ball": 100.0,
                                },
                            },
                        },
                        {
                            "name": "Bravo",
                            "car_name": "Fennec",
                            "stats": {
                                "movement": {"avg_speed": 1000.0, "percent_ground": 100.0},
                                "positioning": {
                                    "percent_behind_ball": 100.0,
                                    "avg_distance_to_ball": 1000.0,
                                    "percent_most_back": 100.0,
                                    "percent_most_forward": 0.0,
                                    "percent_closest_to_ball": 0.0,
                                },
                            },
                        },
                    ]
                },
                "orange": {
                    "players": [
                        {
                            "name": "Charlie",
                            "car_name": "Dominus",
                            "stats": {
                                "movement": {"avg_speed": 1800.0, "percent_ground": 100.0},
                                "positioning": {
                                    "percent_behind_ball": 100.0,
                                    "avg_distance_to_ball": 500.0,
                                    "percent_most_back": 0.0,
                                    "percent_most_forward": 100.0,
                                    "percent_closest_to_ball": 100.0,
                                },
                            },
                        },
                        {
                            "name": "Delta",
                            "car_name": "Breakout",
                            "stats": {
                                "movement": {"avg_speed": 800.0, "percent_ground": 100.0},
                                "positioning": {
                                    "percent_behind_ball": 100.0,
                                    "avg_distance_to_ball": 1000.0,
                                    "percent_most_back": 100.0,
                                    "percent_most_forward": 0.0,
                                    "percent_closest_to_ball": 0.0,
                                },
                            },
                        },
                    ]
                },
            }

            serving_con = duckdb.connect(str(serving_db))
            try:
                serving_con.execute("CREATE TABLE remote_replays (replay_id VARCHAR, raw_json VARCHAR)")
                serving_con.execute(
                    "INSERT INTO remote_replays VALUES (?, ?)",
                    ["r2", json.dumps(detail)],
                )
            finally:
                serving_con.close()

            payload = load_replay_frames("r2", hz=8, max_frames=10, raw_db=raw_db, serving_db=serving_db)

            names = {row["player_id"]: row["player_name"] for row in payload["players"]}
            cars = {row["player_id"]: row["car_name"] for row in payload["players"]}
            families = {row["player_id"]: row["car_family"] for row in payload["players"]}
            self.assertEqual(names["blue-front"], "Alpha")
            self.assertEqual(names["blue-back"], "Bravo")
            self.assertEqual(names["orange-front"], "Charlie")
            self.assertEqual(names["orange-back"], "Delta")
            self.assertEqual(cars["blue-front"], "Octane")
            self.assertEqual(cars["blue-back"], "Fennec")
            self.assertEqual(cars["orange-front"], "Dominus")
            self.assertEqual(cars["orange-back"], "Breakout")
            self.assertEqual(families["blue-front"], "octane")
            self.assertEqual(families["blue-back"], "fennec")

    def test_load_replay_frames_respects_start_frame_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_db = root / "raw.duckdb"
            serving_db = root / "serving.duckdb"

            raw_con = duckdb.connect(str(raw_db))
            try:
                raw_con.execute(
                    """
                    CREATE TABLE frames_state (
                        replay_id VARCHAR,
                        bucket BIGINT,
                        t_approx DOUBLE,
                        payload_zlib BLOB
                    )
                    """
                )
                for bucket in range(6):
                    raw_con.execute(
                        "INSERT INTO frames_state VALUES (?, ?, ?, ?)",
                        [
                            "r3",
                            bucket,
                            bucket / 60.0,
                            _frame_blob(
                                {
                                    "car-1": {
                                        "team": 0,
                                        "boost": 100.0 - bucket,
                                        "demo": False,
                                        "on_ground": True,
                                        "has_flip": True,
                                        "phys": {"pos": [bucket * 10.0, 0.0, 17.0], "vel": [0.0, 0.0, 0.0], "euler": [0.0, 0.0, 0.0]},
                                    },
                                }
                            ),
                        ],
                    )
            finally:
                raw_con.close()

            payload = load_replay_frames("r3", hz=60, max_frames=2, start_frame=3, raw_db=raw_db, serving_db=serving_db)

            self.assertEqual(payload["start_frame"], 3)
            self.assertEqual(payload["total_frame_count"], 6)
            self.assertEqual(payload["frame_count"], 2)
            self.assertEqual(payload["frames"][0]["bucket"], 3)
            self.assertEqual(payload["frames"][1]["bucket"], 4)


if __name__ == "__main__":
    unittest.main()
