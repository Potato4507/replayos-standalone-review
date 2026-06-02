from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from replayos.maintenance import MaintenanceRunLockedError, MaintenanceWorker, run_maintenance_pass


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "serving_db": "serving.duckdb",
        "ballchasing_api_token": None,
        "ballchasing_default_groups": (),
        "ballchasing_default_creators": (),
        "ballchasing_default_creator_group_limit": 12,
        "youtube_api_key": None,
        "maintenance_enabled": True,
        "maintenance_poll_seconds": 15,
        "maintenance_live_interval_seconds": 60,
        "maintenance_ballchasing_interval_seconds": 900,
        "maintenance_index_interval_seconds": 1800,
        "maintenance_carball_interval_seconds": 300,
        "maintenance_eval_interval_seconds": 300,
        "maintenance_youtube_interval_seconds": 1200,
        "maintenance_ballchasing_count": 8,
        "maintenance_parse_limit": 8,
        "maintenance_eval_limit": 48,
        "maintenance_youtube_limit": 6,
        "sidecar_enabled": False,
        "sidecar_sleep_seconds": 90,
        "sidecar_parse_limit": 6,
        "sidecar_eval_limit": 96,
        "sidecar_ballchasing_count": 10,
        "sidecar_youtube_limit": 6,
        "sidecar_index_every": 8,
        "sidecar_ballchasing_every": 10,
        "sidecar_carball_every": 1,
        "sidecar_eval_every": 1,
        "sidecar_youtube_every": 6,
        "sidecar_live_every": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@contextmanager
def _fake_serving_connection(*, read_only: bool = True):  # noqa: ARG001
    yield object()


@contextmanager
def _fake_duck_connection(*args: object, **kwargs: object):  # noqa: ARG001
    yield object()


class MaintenanceTests(unittest.TestCase):
    def test_run_maintenance_pass_skips_missing_optional_sources(self) -> None:
        with (
            patch("replayos.maintenance.get_settings", return_value=_settings()),
            patch("replayos.maintenance.serving_connection", side_effect=_fake_serving_connection),
            patch("replayos.maintenance.ballchasing_status", return_value={"downloaded_replays": 0}),
            patch("replayos.maintenance.youtube_status", return_value={"video_links": 0}),
            patch("replayos.maintenance.replay_review_status", return_value={"cached_replays": 0}),
            patch("replayos.maintenance.YouTubeClient.provider_status", return_value={"sync_enabled": False, "provider": "none"}),
            patch("replayos.maintenance.duckdb.connect", side_effect=_fake_duck_connection),
            patch("replayos.maintenance.team_elo_index", return_value=[]),
        ):
            result = run_maintenance_pass(
                trigger="test",
                refresh_index=False,
                refresh_ballchasing=True,
                backfill_names=False,
                refresh_youtube=True,
                backfill_eval=False,
                refresh_live=False,
            )

        self.assertEqual(result["steps"]["ballchasing"]["status"], "skipped")
        self.assertEqual(result["steps"]["youtube"]["status"], "skipped")
        self.assertEqual(result["status"]["ballchasing"]["downloaded_replays"], 0)
        self.assertEqual(result["status"]["youtube"]["video_links"], 0)

    def test_worker_run_now_updates_snapshot(self) -> None:
        fake_result = {
            "trigger": "manual",
            "steps": {
                "live": {"status": "ok"},
                "ballchasing": {"status": "skipped"},
                "index": {"status": "ok"},
                "carball": {"status": "ok"},
                "youtube": {"status": "skipped"},
                "eval": {"status": "ok"},
            },
        }
        with (
            patch("replayos.maintenance.get_settings", return_value=_settings()),
            patch("replayos.maintenance.run_maintenance_pass", return_value=fake_result),
        ):
            worker = MaintenanceWorker()
            result = worker.run_now(
                trigger="manual",
                refresh_index=True,
                refresh_ballchasing=False,
                backfill_names=True,
                refresh_youtube=False,
                backfill_eval=True,
                refresh_live=True,
            )
            snapshot = worker.snapshot()

        self.assertEqual(result, fake_result)
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["last_result"], fake_result)
        self.assertEqual(snapshot["steps"]["live"]["last_result"]["status"], "ok")
        self.assertEqual(snapshot["steps"]["eval"]["last_result"]["status"], "ok")

    def test_worker_snapshot_is_detached_from_returned_result(self) -> None:
        fake_result = {
            "trigger": "manual",
            "steps": {
                "live": {"status": "ok"},
            },
            "status": {"eval": {"cached_replays": 1}},
        }
        with (
            patch("replayos.maintenance.get_settings", return_value=_settings()),
            patch("replayos.maintenance.run_maintenance_pass", return_value=fake_result),
        ):
            worker = MaintenanceWorker()
            result = worker.run_now(
                trigger="manual",
                refresh_index=False,
                refresh_ballchasing=False,
                backfill_names=False,
                refresh_youtube=False,
                backfill_eval=False,
                refresh_live=True,
            )
            result["status"]["worker"] = {"last_result": "mutated"}
            snapshot = worker.snapshot()

        self.assertNotIn("worker", snapshot["last_result"]["status"])
        self.assertEqual(snapshot["last_result"]["status"]["eval"]["cached_replays"], 1)

    def test_worker_run_now_returns_busy_when_already_running(self) -> None:
        with patch("replayos.maintenance.get_settings", return_value=_settings()):
            worker = MaintenanceWorker()
        worker._run_lock.acquire()
        try:
            result = worker.run_now()
        finally:
            worker._run_lock.release()

        self.assertEqual(result["status"], "busy")
        self.assertIn("worker", result)

    def test_worker_start_skips_background_thread_when_sidecar_enabled(self) -> None:
        with patch("replayos.maintenance.get_settings", return_value=_settings(sidecar_enabled=True)):
            worker = MaintenanceWorker()
            worker.start()
            snapshot = worker.snapshot()

        self.assertFalse(snapshot["enabled"])
        self.assertFalse(snapshot["thread_alive"])

    def test_run_maintenance_pass_returns_busy_when_writer_lock_exists(self) -> None:
        with (
            patch("replayos.maintenance.get_settings", return_value=_settings()),
            patch(
                "replayos.maintenance._MaintenanceWriterLock.acquire",
                side_effect=MaintenanceRunLockedError({"pid": os.getpid(), "path": "maintenance.lock"}),
            ),
        ):
            result = run_maintenance_pass(
                trigger="test",
                refresh_index=False,
                refresh_ballchasing=False,
                backfill_names=False,
                refresh_youtube=False,
                backfill_eval=False,
                refresh_live=False,
            )

        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["reason"], "writer_locked")
        self.assertEqual(result["lock"]["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()
