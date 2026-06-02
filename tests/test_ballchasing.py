from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from replayos.ballchasing import (
    BallchasingError,
    expand_ballchasing_group_tree,
    normalize_ballchasing_creator_id,
    normalize_ballchasing_group_id,
    resolve_ballchasing_source,
    sync_ballchasing_source_set,
)


class BallchasingTests(unittest.TestCase):
    def test_normalize_group_id_from_url(self) -> None:
        self.assertEqual(
            normalize_ballchasing_group_id("https://ballchasing.com/group/ewc-rl-2025-d7nloy3ch5"),
            "ewc-rl-2025-d7nloy3ch5",
        )

    def test_normalize_creator_id_from_groups_url(self) -> None:
        self.assertEqual(
            normalize_ballchasing_creator_id("https://ballchasing.com/groups?creator=76561199225615730"),
            "76561199225615730",
        )

    def test_resolve_ballchasing_source_prefers_creator_query(self) -> None:
        self.assertEqual(
            resolve_ballchasing_source("https://ballchasing.com/groups?creator=76561199225615730"),
            ("creator", "76561199225615730"),
        )

    def test_resolve_ballchasing_source_for_group_url(self) -> None:
        self.assertEqual(
            resolve_ballchasing_source("https://ballchasing.com/group/a-fifae-world-cup-9nby6364uq"),
            ("group", "a-fifae-world-cup-9nby6364uq"),
        )

    def test_expand_ballchasing_group_tree_walks_nested_children(self) -> None:
        class FakeClient:
            def iterate_groups(self, params=None, *, max_groups=25):
                group = (params or {}).get("group")
                mapping = {
                    "root": [{"id": "stage-a"}, {"id": "stage-b"}],
                    "stage-a": [{"id": "match-1"}],
                    "stage-b": [],
                    "match-1": [],
                }
                return mapping.get(group, [])[:max_groups]

        tree = expand_ballchasing_group_tree(FakeClient(), ["root"])
        self.assertEqual(tree["root_group_ids"], ["root"])
        self.assertEqual(tree["group_ids"], ["root", "stage-a", "stage-b", "match-1"])
        self.assertEqual(tree["errors"], [])

    def test_sync_source_set_keeps_going_when_creator_feed_fails(self) -> None:
        class FakeClient:
            def iterate_groups(self, params=None, *, max_groups=25):  # noqa: ARG002
                raise BallchasingError("429 from Ballchasing at creator feed")

        settings = SimpleNamespace(
            ballchasing_default_creator_group_limit=12,
            replay_download_dir=Path("D:/RocketLeagueFrames/replays/ballchasing"),
        )
        with (
            patch("replayos.ballchasing.get_settings", return_value=settings),
            patch("replayos.ballchasing.BallchasingClient", return_value=FakeClient()),
            patch(
                "replayos.ballchasing.expand_ballchasing_group_tree",
                return_value={"links": [], "group_ids": ["group-a"], "errors": []},
            ),
            patch(
                "replayos.ballchasing.sync_ballchasing_replays",
                return_value={"seen": 0, "inserted": 0, "updated": 0, "downloaded": 0, "parsed": 0, "parse_failed": 0, "groups_synced": 0, "players_upserted": 0, "parse_errors": []},
            ),
        ):
            result = sync_ballchasing_source_set(
                serving_db=Path("D:/RocketLeagueFrames/data/replayos_serving.duckdb"),
                group_ids=["group-a"],
                creator_ids=["76561199225615730"],
                count=1,
                download_files=False,
                fetch_details=False,
                parse_downloads=False,
            )

        self.assertEqual(result["expanded_group_ids"], ["group-a"])
        self.assertEqual(len(result["creator_errors"]), 1)
        self.assertIn("429", result["creator_errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
