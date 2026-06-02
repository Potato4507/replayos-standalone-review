from __future__ import annotations

import unittest
from datetime import datetime, timezone

import duckdb

from replayos.youtube_sync import (
    _apply_series_group_fallback,
    YouTubeClient,
    _assign_bundle_estimates,
    _assign_bundle_video_items,
    _assign_bundle_segments,
    _normalize_ytdlp_video,
    _parse_chapters,
    _score_video,
    replay_videos,
)


class YouTubeSyncTests(unittest.TestCase):
    def test_parse_chapters_reads_timestamp_lines(self) -> None:
        description = """
        0:00 Intro
        5:00 Game 1 - Alpha vs Bravo
        18:10 Game 2 - Alpha vs Bravo
        """
        chapters = _parse_chapters(description)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[1]["start_seconds"], 300)
        self.assertEqual(chapters[1]["game_number"], 1)

    def test_assign_bundle_segments_builds_trimmed_game_windows(self) -> None:
        bundle = {
            "bundle_id": "series-1",
            "series_name": "RLCS Open Qualifier",
            "blue_team_name": "Alpha",
            "orange_team_name": "Bravo",
            "team_names": ["Alpha", "Bravo"],
            "replays": [
                {
                    "replay_id": "r1",
                    "blue_team_name": "Alpha",
                    "orange_team_name": "Bravo",
                    "duration": 320.0,
                    "series_replay_index": 1,
                    "game_number": 1,
                },
                {
                    "replay_id": "r2",
                    "blue_team_name": "Alpha",
                    "orange_team_name": "Bravo",
                    "duration": 350.0,
                    "series_replay_index": 2,
                    "game_number": 2,
                },
            ],
        }
        vod = {
            "source_video_id": "vod123",
            "video_id": "vod123",
            "title": "Alpha vs Bravo RLCS VOD",
            "channel_title": "RLCS",
            "published_at": "2026-05-01T12:00:00Z",
            "description": "vod",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "duration_iso8601": "PT45M0S",
            "duration_seconds": 2700.0,
            "view_count": 1000,
            "embed_url": "https://www.youtube.com/embed/vod123?rel=0&playsinline=1",
            "watch_url": "https://www.youtube.com/watch?v=vod123",
            "query_text": "Alpha vs Bravo Rocket League VOD",
            "match_score": 8.0,
            "reasons": [],
            "chapters": [
                {"start_seconds": 0, "label": "Intro", "game_number": None},
                {"start_seconds": 300, "label": "Game 1 - Alpha vs Bravo", "game_number": 1},
                {"start_seconds": 870, "label": "Desk", "game_number": None},
                {"start_seconds": 1080, "label": "Game 2 - Alpha vs Bravo", "game_number": 2},
                {"start_seconds": 1710, "label": "Interview", "game_number": None},
            ],
        }
        assignments = _assign_bundle_segments(bundle, vod)
        self.assertEqual(sorted(assignments), ["r1", "r2"])
        clip_one = assignments["r1"][0]
        clip_two = assignments["r2"][0]
        self.assertEqual(clip_one["video_kind"], "vod_segment")
        self.assertEqual(clip_one["segment_label"], "Game 1 - Alpha vs Bravo")
        self.assertEqual(clip_one["segment_start_seconds"], 292)
        self.assertLess(clip_one["segment_end_seconds"], 870)
        self.assertEqual(clip_two["segment_start_seconds"], 1072)
        self.assertLess(clip_two["segment_end_seconds"], 1710)

    def test_assign_bundle_estimates_builds_series_windows_without_chapters(self) -> None:
        bundle = {
            "bundle_id": "series-2",
            "series_name": "RLCS Main Event",
            "blue_team_name": "Alpha",
            "orange_team_name": "Bravo",
            "team_names": ["Alpha", "Bravo"],
            "approx_total_duration": 980.0,
            "replays": [
                {
                    "replay_id": "r1",
                    "blue_team_name": "Alpha",
                    "orange_team_name": "Bravo",
                    "duration": 310.0,
                    "series_replay_index": 1,
                    "game_number": 1,
                },
                {
                    "replay_id": "r2",
                    "blue_team_name": "Alpha",
                    "orange_team_name": "Bravo",
                    "duration": 330.0,
                    "series_replay_index": 2,
                    "game_number": 2,
                },
                {
                    "replay_id": "r3",
                    "blue_team_name": "Alpha",
                    "orange_team_name": "Bravo",
                    "duration": 340.0,
                    "series_replay_index": 3,
                    "game_number": 3,
                },
            ],
        }
        vod = {
            "source_video_id": "vod456",
            "video_id": "vod456",
            "title": "Alpha vs Bravo | Full Series | RLCS Main Event",
            "channel_title": "Rocket League",
            "published_at": "2026-05-01T12:00:00Z",
            "description": "full series vod",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "duration_iso8601": "PT22M30S",
            "duration_seconds": 1350.0,
            "view_count": 1400,
            "embed_url": "https://www.youtube.com/embed/vod456?rel=0&playsinline=1",
            "watch_url": "https://www.youtube.com/watch?v=vod456",
            "query_text": "Alpha vs Bravo Rocket League VOD",
            "match_score": 8.4,
            "reasons": [],
        }
        assignments = _assign_bundle_estimates(bundle, vod)
        self.assertEqual(sorted(assignments), ["r1", "r2", "r3"])
        self.assertEqual(assignments["r1"][0]["video_kind"], "vod_estimate")
        self.assertEqual(assignments["r1"][0]["segment_label"], "Estimated Game 1")
        self.assertLess(assignments["r1"][0]["segment_start_seconds"], assignments["r2"][0]["segment_start_seconds"])
        self.assertLess(assignments["r2"][0]["segment_start_seconds"], assignments["r3"][0]["segment_start_seconds"])

    def test_assign_bundle_video_items_falls_back_to_estimates_when_no_chapters(self) -> None:
        bundle = {
            "bundle_id": "series-3",
            "series_name": "RLCS Qualifier",
            "blue_team_name": "Alpha",
            "orange_team_name": "Bravo",
            "team_names": ["Alpha", "Bravo"],
            "approx_total_duration": 640.0,
            "replays": [
                {"replay_id": "r1", "blue_team_name": "Alpha", "orange_team_name": "Bravo", "duration": 320.0, "series_replay_index": 1, "game_number": 1},
                {"replay_id": "r2", "blue_team_name": "Alpha", "orange_team_name": "Bravo", "duration": 320.0, "series_replay_index": 2, "game_number": 2},
            ],
        }
        vod = {
            "source_video_id": "vod789",
            "video_id": "vod789",
            "title": "Alpha vs Bravo Full Series RLCS Qualifier",
            "channel_title": "Rocket League",
            "published_at": "2026-05-01T12:00:00Z",
            "description": "no chapters here",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "duration_iso8601": "PT15M0S",
            "duration_seconds": 900.0,
            "view_count": 1600,
            "embed_url": "https://www.youtube.com/embed/vod789?rel=0&playsinline=1",
            "watch_url": "https://www.youtube.com/watch?v=vod789",
            "query_text": "Alpha vs Bravo Rocket League VOD",
            "match_score": 7.8,
            "reasons": [],
            "chapters": [],
        }
        assignments = _assign_bundle_video_items(bundle, vod)
        self.assertEqual(sorted(assignments), ["r1", "r2"])
        self.assertTrue(all(item[0]["video_kind"] == "vod_estimate" for item in assignments.values()))

    def test_replay_videos_reads_legacy_schema(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE replay_videos (
                    replay_id VARCHAR,
                    video_id VARCHAR,
                    title VARCHAR,
                    channel_title VARCHAR,
                    published_at TIMESTAMP,
                    description VARCHAR,
                    thumbnail_url VARCHAR,
                    duration_iso8601 VARCHAR,
                    view_count BIGINT,
                    embed_url VARCHAR,
                    watch_url VARCHAR,
                    query_text VARCHAR,
                    match_score DOUBLE,
                    reasons_json VARCHAR,
                    synced_at TIMESTAMP
                )
                """
            )
            con.execute(
                """
                INSERT INTO replay_videos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    "r1",
                    "abc123",
                    "Standalone match",
                    "RLCS",
                    datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    "desc",
                    "thumb",
                    "PT8M20S",
                    100,
                    "https://www.youtube.com/embed/abc123",
                    "https://www.youtube.com/watch?v=abc123",
                    "Alpha vs Bravo",
                    5.5,
                    "[]",
                    datetime(2026, 5, 1, 12, 5, tzinfo=timezone.utc),
                ],
            )
            rows = replay_videos(con, "r1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_video_id"], "abc123")
            self.assertEqual(rows[0]["video_kind"], "full_video")
        finally:
            con.close()

    def test_normalize_ytdlp_video_matches_internal_shape(self) -> None:
        video = _normalize_ytdlp_video(
            {
                "id": "abc123",
                "title": "Alpha vs Bravo RLCS",
                "description": "0:00 Intro",
                "channel": "Rocket League",
                "duration": 502,
                "view_count": 12345,
                "upload_date": "20260501",
                "thumbnail": "https://example.com/thumb.jpg",
                "chapters": [{"title": "Game 1 - Alpha vs Bravo", "start_time": 300}],
            }
        )
        self.assertEqual(video["id"], "abc123")
        self.assertEqual(video["snippet"]["channelTitle"], "Rocket League")
        self.assertEqual(video["contentDetails"]["duration"], "PT8M22S")
        self.assertEqual(video["statistics"]["viewCount"], 12345)
        self.assertEqual(video["chapters"][0]["game_number"], 1)

    def test_provider_status_supports_no_key_mode(self) -> None:
        status = YouTubeClient.provider_status(api_key=None)
        self.assertTrue(status["sync_enabled"])
        self.assertIn(status["provider"], {"yt_dlp_public", "youtube_data_api"})

    def test_group_json_fallback_prefers_specific_series_label(self) -> None:
        candidate = {
            "blue_team_name": "Team Vitality",
            "orange_team_name": "Team Falcons",
            "group_ids_json": '["ewc-playoffs","grand-finals"]',
            "group_names_json": '["EWC Playoffs","Grand Finals - Team Vitality vs Team Falcons"]',
        }
        _apply_series_group_fallback(candidate)
        self.assertEqual(candidate["group_id"], "grand-finals")
        self.assertIn("Grand Finals", candidate["series_name"])

    def test_score_video_prefers_full_match_over_highlights_and_mismatches(self) -> None:
        candidate = {
            "replay_id": "r1",
            "blue_team_name": "Team Vitality",
            "orange_team_name": "Geekay Esports",
            "series_name": "EWC 25 Semifinal",
            "duration": 399.0,
            "match_date": datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        }
        highlight_video = {
            "id": "high123",
            "snippet": {
                "title": "SEMI-FINALS! Team Vitality vs Geekay Esports - HIGHLIGHTS - Rocket League ft. at EWC 25",
                "description": "",
                "publishedAt": "2026-05-01T13:00:00Z",
                "channelTitle": "RL Highlights",
                "thumbnails": {},
            },
            "contentDetails": {"duration": "PT5M30S"},
            "statistics": {"viewCount": 20000},
        }
        full_match_video = {
            "id": "full123",
            "snippet": {
                "title": "Team Vitality vs. Geekay Esports | Rocket League ft. at EWC 25 - Day 4 - Semifinals",
                "description": "",
                "publishedAt": "2026-05-01T13:00:00Z",
                "channelTitle": "Rocket League",
                "thumbnails": {},
            },
            "contentDetails": {"duration": "PT24M0S"},
            "statistics": {"viewCount": 15000},
        }
        wrong_match_video = {
            "id": "wrong123",
            "snippet": {
                "title": "VITALITY vs NRG (EWC $1,000,000 LAN) | Rocket League",
                "description": "",
                "publishedAt": "2026-05-01T13:00:00Z",
                "channelTitle": "Rocket League",
                "thumbnails": {},
            },
            "contentDetails": {"duration": "PT23M0S"},
            "statistics": {"viewCount": 15000},
        }

        highlight_score = _score_video(candidate, highlight_video)["match_score"]
        full_match_score = _score_video(candidate, full_match_video)["match_score"]
        wrong_match_score = _score_video(candidate, wrong_match_video)["match_score"]

        self.assertGreater(full_match_score, highlight_score)
        self.assertGreater(full_match_score, wrong_match_score)


if __name__ == "__main__":
    unittest.main()
