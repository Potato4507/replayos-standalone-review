from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import duckdb

from replayos.db import _connect_with_retry, database_connection, replica_db_path


class DuckDbConnectionRetryTests(TestCase):
    def test_connect_with_retry_recovers_after_io_error(self) -> None:
        marker = object()
        attempts = {"count": 0}

        def flaky_connect(path: str, *, read_only: bool):  # noqa: ARG001
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise duckdb.IOException("database busy")
            return marker

        with (
            patch("replayos.db.duckdb.connect", side_effect=flaky_connect),
            patch("replayos.db.time.sleep", return_value=None),
        ):
            result = _connect_with_retry(Path("D:/RocketLeagueFrames/data/replayos_serving.duckdb"), read_only=True)

        self.assertIs(result, marker)
        self.assertEqual(attempts["count"], 3)

    def test_connect_with_retry_raises_after_retry_budget(self) -> None:
        with (
            patch("replayos.db.duckdb.connect", side_effect=duckdb.IOException("still busy")),
            patch("replayos.db.time.sleep", return_value=None),
        ):
            with self.assertRaises(duckdb.IOException):
                _connect_with_retry(Path("D:/RocketLeagueFrames/data/replayos_serving.duckdb"), read_only=False)

    def test_connect_with_retry_falls_back_to_writable_when_configs_conflict(self) -> None:
        marker = object()
        calls: list[bool] = []

        def conflicting_connect(path: str, *, read_only: bool):  # noqa: ARG001
            calls.append(read_only)
            if read_only:
                raise duckdb.ConnectionException("Can't open a connection to same database file with a different configuration than existing connections")
            return marker

        with patch("replayos.db.duckdb.connect", side_effect=conflicting_connect):
            result = _connect_with_retry(Path("D:/RocketLeagueFrames/data/replayos_serving.duckdb"), read_only=True)

        self.assertIs(result, marker)
        self.assertEqual(calls, [True, False])

    def test_database_connection_prefers_replica_for_reads(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        with TemporaryDirectory() as temp_dir:
            fake = FakeConnection()
            primary = Path(temp_dir) / "replayos_serving.duckdb"
            primary.write_bytes(b"")
            replica = replica_db_path(primary)
            replica.write_bytes(b"")
            with patch("replayos.db._connect_with_retry", return_value=fake) as connect:
                with database_connection(primary, read_only=True) as con:
                    self.assertIs(con, fake)

            self.assertTrue(fake.closed)
            connect.assert_called_once_with(replica, read_only=True, fallback_path=primary)
