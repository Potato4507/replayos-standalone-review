from __future__ import annotations

import unittest

from replayos.native_viewer import build_native_viewer_payload


class NativeViewerPayloadTests(unittest.TestCase):
    def test_build_native_viewer_payload_reorders_rotation_and_tracks_boost(self) -> None:
        replay = {
            "replay_id": "r1",
            "title": "Alpha vs Bravo",
            "blue_team_name": "Alpha",
            "orange_team_name": "Bravo",
            "blue_goals": 1,
            "orange_goals": 0,
            "map_code": "TrainStation_Night_P",
            "players": [
                {"side": "blue", "player_name": "Alpha", "score": 420, "goals": 1, "shots": 2, "car_name": "Fennec"},
                {"side": "orange", "player_name": "Bravo", "score": 280, "saves": 1, "car_name": "Octane"},
            ],
        }
        parsed_payload = {
            "replay_id": "r1",
            "base_hz": 60,
            "boost_pad_layout": [{"pad_id": "small-0", "x": 0.0, "y": 0.0, "full_boost": False}],
            "players": [
                {
                    "player_id": "blue-1",
                    "player_name": "Alpha",
                    "team": 0,
                    "car_name": "Fennec",
                    "car_body_id": 4284,
                    "loadout": {"car": 4284, "wheels": 777, "boost": 32},
                    "camera_settings": {"fieldOfView": 108.0, "distance": 280.0, "height": 120.0},
                },
                {
                    "player_id": "orange-1",
                    "player_name": "Bravo",
                    "team": 1,
                    "car_name": "Octane",
                    "car_body_id": 23,
                    "loadout": {"car": 23, "wheels": 12, "boost": 66},
                    "camera_settings": {"fieldOfView": 110.0, "distance": 260.0, "height": 90.0},
                },
            ],
            "frames": [
                {
                    "t": 0.0,
                    "ball": {"pos": [0.0, 0.0, 92.75]},
                    "pad_states": [True],
                    "cars": [
                        {"player_id": "blue-1", "player_name": "Alpha", "team": 0, "boost": 50.0, "pos": [10.0, 20.0, 30.0], "euler": [0.1, 0.2, 0.3]},
                        {"player_id": "orange-1", "player_name": "Bravo", "team": 1, "boost": 80.0, "pos": [-10.0, -20.0, 30.0], "euler": [0.4, 0.5, 0.6]},
                    ],
                },
                {
                    "t": 1.0 / 60.0,
                    "ball": {"pos": [1.0, 2.0, 93.0]},
                    "pad_states": [False],
                    "cars": [
                        {"player_id": "blue-1", "player_name": "Alpha", "team": 0, "boost": 45.0, "pos": [12.0, 22.0, 32.0], "euler": [0.11, 0.21, 0.31]},
                        {"player_id": "orange-1", "player_name": "Bravo", "team": 1, "boost": 80.0, "pos": [-12.0, -22.0, 32.0], "euler": [0.41, 0.51, 0.61]},
                    ],
                },
            ],
        }
        events = [
            {"event_type": "goal", "t": 0.5, "player_id": "blue-1"},
        ]

        payload = build_native_viewer_payload(replay, parsed_payload, events)

        self.assertEqual(payload["replayData"]["names"], ["Alpha", "Bravo"])
        self.assertEqual(payload["replayData"]["colors"], [False, True])
        self.assertEqual(payload["replayData"]["ball"][0], [0.0, 0.0, 92.75, 0.0, 0.0, 0.0])
        self.assertEqual(payload["replayData"]["players"][0][0], [10.0, 20.0, 30.0, 0.1, 0.2, 0.3, False, 50.0])
        self.assertEqual(payload["replayData"]["players"][0][1], [12.0, 22.0, 32.0, 0.11, 0.21, 0.31, True, 45.0])
        self.assertEqual(payload["hud"]["boost_by_player"]["blue-1"], [50.0, 45.0])
        self.assertEqual(payload["hud"]["pad_state_masks"], [1, 0])
        self.assertEqual(payload["replayMetadata"]["gameMetadata"]["goals"], [{"frameNumber": 1, "playerId": {"id": "blue-1"}}])
        self.assertEqual(payload["replayMetadata"]["players"][0]["loadout"]["car"], 4284)
        self.assertEqual(payload["replayMetadata"]["players"][0]["loadout"]["wheels"], 777)
        self.assertEqual(payload["replayMetadata"]["players"][0]["cameraSettings"]["distance"], 280.0)


if __name__ == "__main__":
    unittest.main()
