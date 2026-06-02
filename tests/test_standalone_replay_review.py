import unittest

from standalone_replay_review import normalize_replay_id


class StandaloneReplayReviewTests(unittest.TestCase):
    def test_normalize_replay_id_from_url(self) -> None:
        self.assertEqual(
            normalize_replay_id("https://ballchasing.com/replay/92aa7211-d35d-4b3c-b93f-8a9faf21ac24"),
            "92aa7211-d35d-4b3c-b93f-8a9faf21ac24",
        )

    def test_normalize_replay_id_from_raw_id(self) -> None:
        self.assertEqual(
            normalize_replay_id("92AA7211-D35D-4B3C-B93F-8A9FAF21AC24"),
            "92aa7211-d35d-4b3c-b93f-8a9faf21ac24",
        )

    def test_normalize_replay_id_rejects_noise(self) -> None:
        with self.assertRaises(ValueError):
            normalize_replay_id("not a replay")


if __name__ == "__main__":
    unittest.main()
