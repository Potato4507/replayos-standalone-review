from __future__ import annotations

import unittest

from replayos.semantics import build_possession_phases, build_replay_timeline, build_touch_chains


class SemanticsTests(unittest.TestCase):
    def test_touch_chains_split_on_player_and_gap(self) -> None:
        events = [
            {"event_type": "touch", "t": 1.0, "team_color": "blue", "player_id": "a"},
            {"event_type": "touch", "t": 2.0, "team_color": "blue", "player_id": "a"},
            {"event_type": "touch", "t": 8.0, "team_color": "blue", "player_id": "a"},
            {"event_type": "touch", "t": 8.5, "team_color": "orange", "player_id": "b"},
        ]
        chains = build_touch_chains(events, gap_seconds=2.5)
        self.assertEqual([chain["touches"] for chain in chains], [2, 1, 1])
        self.assertEqual(chains[0]["duration"], 1.0)

    def test_possession_turnover_when_new_start_arrives(self) -> None:
        events = [
            {"event_type": "possession_start", "t": 1.0, "team_color": "blue", "player_id": "a"},
            {"event_type": "possession_start", "t": 4.0, "team_color": "orange", "player_id": "b"},
            {"event_type": "goal", "t": 6.0, "team_color": "orange", "player_id": "b"},
        ]
        phases = build_possession_phases(events)
        self.assertEqual(phases[0]["ended_by"], "turnover")
        self.assertEqual(phases[1]["ended_by"], "goal")

    def test_replay_timeline_includes_turning_points(self) -> None:
        timeline = build_replay_timeline(
            [
                {"event_type": "goal", "t": 10.0, "team_color": "blue", "player_id": "a"},
                {"event_type": "demo", "t": 12.0, "team_color": "orange", "player_id": "b"},
            ]
        )
        self.assertEqual(len(timeline["turning_points"]), 2)


if __name__ == "__main__":
    unittest.main()

