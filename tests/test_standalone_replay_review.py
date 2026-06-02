import unittest

from standalone_replay_review import normalize_replay_id, parse_source_inputs


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

    def test_parse_source_inputs_splits_groups_and_creators(self) -> None:
        parsed = parse_source_inputs(
            "https://ballchasing.com/group/ewc-rl-2025-d7nloy3ch5\nhttps://ballchasing.com/group/gamers8-l5u17eo7y7",
            "https://ballchasing.com/groups?creator=76561199225615730",
        )
        self.assertEqual(
            parsed["groups"],
            ["ewc-rl-2025-d7nloy3ch5", "gamers8-l5u17eo7y7"],
        )
        self.assertEqual(parsed["creators"], ["76561199225615730"])
        self.assertEqual(parsed["invalid"], [])

    def test_parse_source_inputs_reports_invalid_values(self) -> None:
        parsed = parse_source_inputs("https://example.com/not-ballchasing", "")
        self.assertEqual(parsed["groups"], [])
        self.assertEqual(parsed["creators"], [])
        self.assertEqual(parsed["invalid"], ["https://example.com/not-ballchasing"])


if __name__ == "__main__":
    unittest.main()
