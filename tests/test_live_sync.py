from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from replayos.live_sync import (
    LIVE_SYNC_LOCK,
    _parse_blast_matches,
    _parse_leaderboard_page,
    _parse_series_match_page,
    _parse_watch_channels,
    _viewer_count,
    sync_live_data,
)


class LiveSyncTests(unittest.TestCase):
    def test_parse_watch_channels_and_matches(self) -> None:
        html = """
        <html><body>
        <div>Where to watch</div>
        <div>ENG</div><div>rocketleague</div>
        <div>ENG</div><div>rlesports</div>
        <div>FR</div><div>rocketbaguette</div>
        <div>Additional Info</div>
        <div>Today</div>
        <div>Group A</div><div>:</div><div>Match: #1</div><div>BO5</div><div>Match: #1</div><div>Group A</div><div>Team Alpha</div><div>12:00</div><div>Team Bravo</div><div>BO5</div>
        <div>Tomorrow</div>
        <div>Playoffs</div><div>:</div><div>Grand Final</div><div>BO7</div><div>Grand Final</div><div>Playoffs</div><div>TBD</div><div>17:30</div><div>TBD</div><div>BO7</div>
        <div>Overview</div>
        </body></html>
        """
        channels = _parse_watch_channels(html, "paris-major", datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc))
        matches = _parse_blast_matches(html, "paris-major", datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(len(channels), 3)
        self.assertTrue(channels[0]["official"])
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["team_a"], "Team Alpha")
        self.assertIsNone(matches[1]["team_a"])

    def test_viewer_count_parser(self) -> None:
        self.assertEqual(_viewer_count("Watch RocketLeague live. 4,228 viewers."), 4228)

    def test_series_match_page_parser_extracts_scoreboard(self) -> None:
        html = """
        <html><body>
        <div>0000-00-00 - 00:00: Match: #6 - BO5 - LIVE</div>
        <div>Shopify Rebellion</div><div>1</div><div>TSM</div><div>0</div>
        <div>Game 1 Parc de Paris</div><div>3</div><div>0</div>
        <div>Game 2 Forbidden Temple</div><div>-</div><div>-</div>
        <div>Predict The Winner</div>
        </body></html>
        """
        updates = _parse_series_match_page(
            html,
            {"team_a": "Shopify Rebellion", "team_b": "TSM", "status": "scheduled"},
        )
        self.assertEqual(updates["status"], "live")
        self.assertEqual(updates["series_score_a"], 1)
        self.assertEqual(updates["series_score_b"], 0)
        self.assertEqual(updates["games"][0]["score_a"], 3)
        self.assertIsNone(updates["games"][1]["score_a"])

    def test_series_match_page_parser_extracts_completed_split_tokens(self) -> None:
        html = """
        <html><body>
        <div>0000-00-00 - 00:00:</div>
        <div>Match: #3</div><div>-</div><div>BO5</div>
        <div>NRG</div><div>3</div><div>MIBR</div><div>2</div>
        <div>Game</div><div>1</div><div>Parc de Paris</div><div>1</div><div>4</div>
        <div>Game</div><div>2</div><div>Forbidden Temple</div><div>2</div><div>1</div>
        <div>Game</div><div>3</div><div>Mannfield (Dusk)</div><div>3</div><div>OT: +</div><div>00:08</div><div>+00:08</div><div>4</div>
        <div>Predict The Winner</div>
        </body></html>
        """
        updates = _parse_series_match_page(
            html,
            {"team_a": "NRG", "team_b": "MIBR", "best_of": "BO5", "status": "scheduled"},
        )
        self.assertEqual(updates["status"], "completed")
        self.assertEqual(updates["series_score_a"], 3)
        self.assertEqual(updates["series_score_b"], 2)
        self.assertEqual(updates["games"][2]["score_a"], 3)
        self.assertEqual(updates["games"][2]["score_b"], 4)

    def test_series_match_page_parser_keeps_scheduled_when_no_scoreboard(self) -> None:
        html = """
        <html><body>
        <div>0000-00-00 - 00:00:</div>
        <div>Match: #1</div><div>-</div><div>BO7</div>
        <div>MIBR</div><div>-</div><div>Spacestation</div><div>-</div>
        <div>Predict The Winner</div>
        </body></html>
        """
        updates = _parse_series_match_page(
            html,
            {"team_a": "MIBR", "team_b": "Spacestation", "best_of": "BO7", "status": "scheduled"},
        )
        self.assertEqual(updates["status"], "scheduled")
        self.assertIsNone(updates["series_score_a"])
        self.assertEqual(updates["games"], [])

    def test_leaderboard_parser_extracts_top_rows(self) -> None:
        html = """
        <html><body>
        <div>Rank Team Players Points</div>
        <div>#1</div><div>NRG</div><div>BeastMode</div><div>Atomic</div><div>Daniel</div><div>38 pts</div>
        <div>#2</div><div>Spacestation</div><div>diaz</div><div>Zach</div><div>reveal</div><div>38 pts</div>
        <div>Major 2 NA Qualifying Cut</div>
        </body></html>
        """
        board = _parse_leaderboard_page(
            html,
            board_key="2026:major-2:na",
            board_name="MAJOR-2 NA",
            stage_key="major-2",
            region="NA",
            source_url="https://blast.tv/rl/leaderboard/2026/major-2/na",
            fetched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(board["items"]), 2)
        self.assertEqual(board["items"][0]["team_name"], "NRG")
        self.assertEqual(board["items"][0]["points"], 38)

    def test_sync_live_data_skips_when_another_sync_is_running(self) -> None:
        LIVE_SYNC_LOCK.acquire()
        try:
            result = sync_live_data(Path("ignored.duckdb"), force=False)
        finally:
            LIVE_SYNC_LOCK.release()
        self.assertTrue(result["skipped"])
        self.assertTrue(result["busy"])


if __name__ == "__main__":
    unittest.main()
